import datetime
import logging
from typing import Dict, Any, List, Optional, Tuple
import jwt
import httpx
from bson import ObjectId

from app import config
from app.services.athletes_esm_prices import (
    normalize_text,
    extract_significant_tokens,
    get_esm_token as get_technical_esm_token,
)

logger = logging.getLogger(__name__)

ESM_API_BASE = "https://esmdomainserviceproduction.azurewebsites.net"
DEFAULT_ESM_LEAGUE_ID = "6ae06420a36131d6eb48db15"


def _resolve_ioc_from_esm(player: Dict[str, Any], countries_dict: Dict[str, Any]) -> str:
    """Dopasowuje kod IOC kraju z pól teamKey / team (zarówno EN jak i PL)."""
    candidates = [
        str(player.get("teamKey") or "").strip().lower(),
        str(player.get("team") or "").strip().lower(),
    ]
    for cand in candidates:
        if not cand:
            continue
        for code, names in countries_dict.items():
            if (
                str(names.get("eng", "")).strip().lower() == cand
                or str(names.get("pl", "")).strip().lower() == cand
                or str(code).strip().lower() == cand
            ):
                return code
    return ""


async def _build_athletes_lookup(db) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Buduje indeks skoczków z MongoDB pogrupowany po country_code."""
    cursor = db["athletes"].find(
        {},
        {"fis_code": 1, "full_name": 1, "country_code": 1, "status": 1, "price": 1},
    )
    all_docs = await cursor.to_list(length=None)
    by_ioc: Dict[str, List[Dict[str, Any]]] = {}
    flat: List[Dict[str, Any]] = []
    for a in all_docs:
        item = {
            "fis_code": a.get("fis_code"),
            "full_name": a.get("full_name", ""),
            "country_code": a.get("country_code", ""),
            "status": a.get("status"),
            "price": a.get("price"),
            "norm": normalize_text(a.get("full_name", "")),
        }
        flat.append(item)
        ioc = item["country_code"]
        if ioc:
            by_ioc.setdefault(ioc, []).append(item)
    return by_ioc, flat


def match_esm_player_to_db(
    player: Dict[str, Any],
    countries_dict: Dict[str, Any],
    athletes_by_ioc: Dict[str, List[Dict[str, Any]]],
    all_athletes: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Dopasowuje skoczka z obiektu userteam ESM (np. 'Kristoffer Sundal', 'Norway')
    do rekordu w bazie FantaSKI ('SUNDAL Kristoffer Eriksen', fis_code: 8021).
    """
    fullname = str(player.get("fullname") or "").strip()
    surname = str(player.get("surname") or "").strip()
    if not fullname:
        fullname = f"{player.get('name', '')} {surname}".strip()

    full_tokens = extract_significant_tokens(fullname)
    surname_tokens = extract_significant_tokens(surname)
    first_tokens = [t for t in full_tokens if t not in surname_tokens]

    ioc_code = _resolve_ioc_from_esm(player, countries_dict)
    pool = athletes_by_ioc.get(ioc_code, []) if ioc_code else all_athletes

    def _search(search_pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        exact = []
        partial = []
        for cand in search_pool:
            norm = cand["norm"]
            if full_tokens and all(t in norm for t in full_tokens):
                exact.append(cand)
            elif surname_tokens and all(st in norm for st in surname_tokens):
                if not first_tokens or any(ft in norm for ft in first_tokens):
                    partial.append(cand)
        return exact if exact else partial

    cands = _search(pool)
    if not cands and ioc_code:
        # Fallback globalny gdyby kraj w słowniku różnił się nazwą
        cands = _search(all_athletes)

    if not cands:
        return None
    if len(cands) > 1:
        active = [c for c in cands if c.get("status") == "Active"]
        if active:
            cands = active
    return cands[0]


def parse_esm_team_payload(
    raw_data: Any,
    countries_dict: Dict[str, Any],
    athletes_by_ioc: Dict[str, List[Dict[str, Any]]],
    all_athletes: List[Dict[str, Any]],
    league_id: str = DEFAULT_ESM_LEAGUE_ID,
) -> Optional[Dict[str, Any]]:
    """Parsuje odpowiedź /leagues/fantasy/userteams/... do ustrukturyzowanego składu FantaSKI."""
    team_obj: Optional[Dict[str, Any]] = None
    if isinstance(raw_data, list):
        if not raw_data:
            return None
        matching = [t for t in raw_data if isinstance(t, dict) and str(t.get("league")) == str(league_id)]
        team_obj = matching[0] if matching else raw_data[0]
    elif isinstance(raw_data, dict):
        team_obj = raw_data

    if not team_obj or not isinstance(team_obj, dict):
        return None

    squad_groups = team_obj.get("squad") or []
    starters: List[Dict[str, Any]] = []
    bench: List[Dict[str, Any]] = []

    for g_idx, grp in enumerate(squad_groups):
        if not isinstance(grp, list):
            continue
        for p in grp:
            if not isinstance(p, dict):
                continue
            matched = match_esm_player_to_db(p, countries_dict, athletes_by_ioc, all_athletes)
            ioc = (matched.get("country_code") if matched else None) or _resolve_ioc_from_esm(p, countries_dict)
            entry = {
                "esm_id": p.get("_id"),
                "esm_fullname": p.get("fullname") or f"{p.get('name', '')} {p.get('surname', '')}".strip(),
                "fis_code": matched.get("fis_code") if matched else None,
                "fis_name": matched.get("full_name") if matched else None,
                "country": ioc,
                "price": p.get("price") if p.get("price") is not None else (matched.get("price") if matched else None),
                "is_captain": bool(p.get("isPlayerCaptain")),
                "group": g_idx,
            }
            if g_idx == 0:
                starters.append(entry)
            else:
                bench.append(entry)

    fixture_pts = team_obj.get("fixturePoints") or {}
    total_pts = sum(float(v) for v in fixture_pts.values() if isinstance(v, (int, float))) if isinstance(fixture_pts, dict) else 0.0

    return {
        "team_id": str(team_obj.get("_id") or ""),
        "team_name": str(team_obj.get("name") or "Drużyna ESM"),
        "league_id": str(team_obj.get("league") or league_id),
        "value": team_obj.get("Value"),
        "income": team_obj.get("Income"),
        "total_points": round(total_pts, 1),
        "starters": starters,
        "bench": bench,
        "all_players": starters + bench,
        "synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def decode_esm_token_email(token: str) -> Optional[str]:
    """Odczytuje bezpiecznie pole email z tokena ESM bez weryfikacji podpisu serwera ESM."""
    try:
        payload = jwt.decode(token.strip(), options={"verify_signature": False})
        if isinstance(payload, dict) and payload.get("email"):
            return str(payload["email"])
    except Exception:
        pass
    return None


async def login_esm_with_credentials(email: str, password: str) -> str:
    """Oficjalne logowanie do API esmManager przez email + hasło (POST /users/auth)."""
    url = f"{ESM_API_BASE}/users/auth"
    payload = {"email": email.strip(), "password": password, "from": "/joinTeam"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code in (400, 401, 403):
            raise ValueError("Nieprawidłowy e-mail lub hasło konta esmManager.")
        resp.raise_for_status()
        tok = resp.text.strip().strip('"')
        if not tok:
            raise ValueError("Serwer esmManager zwrócił pusty token.")
        return tok


async def fetch_esm_my_team(token: str, league_id: str = DEFAULT_ESM_LEAGUE_ID) -> Any:
    url = f"{ESM_API_BASE}/leagues/fantasy/userteams/league/{league_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={"x-auth-token": token.strip()})
        if resp.status_code in (401, 403):
            raise PermissionError("Token x-auth-token esmManager jest nieprawidłowy lub wygasł.")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def fetch_esm_team_by_id(team_id: str, token: Optional[str] = None) -> Any:
    clean_id = str(team_id or "").strip()
    if not clean_id:
        return None
    use_tok = token.strip() if token else get_technical_esm_token()
    url = f"{ESM_API_BASE}/leagues/fantasy/userteams/{clean_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={"x-auth-token": use_tok})
        if resp.status_code in (401, 403) and token:
            # Spróbuj technicznym tokenem aplikacji, bo endpoint /userteams/{id} jest współdzielony w lidze
            tech_tok = get_technical_esm_token()
            resp = await client.get(url, headers={"x-auth-token": tech_tok})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def fetch_esm_friends(token: str) -> List[Dict[str, Any]]:
    url = f"{ESM_API_BASE}/users/me/friends"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={"x-auth-token": token.strip()})
        if resp.status_code in (401, 403):
            raise PermissionError("Token x-auth-token esmManager wygasł lub nie ma dostępu do listy znajomych.")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


def _extract_friend_candidate_ids(friend_obj: Dict[str, Any], league_id: str) -> Tuple[str, List[str]]:
    """Wyciąga nazwę znajomego oraz możliwe identyfikatory drużyny/użytkownika z obiektu /users/me/friends."""
    display_name = (
        friend_obj.get("name")
        or friend_obj.get("username")
        or friend_obj.get("fullname")
        or friend_obj.get("email")
        or str(friend_obj.get("_id") or "Znajomy ESM")
    )
    cand_team_ids: List[str] = []
    for key in ("userTeamId", "teamId", "fantasyTeamId"):
        if friend_obj.get(key):
            cand_team_ids.append(str(friend_obj[key]))

    for nested_key in ("userTeam", "team"):
        nested = friend_obj.get(nested_key)
        if isinstance(nested, dict) and nested.get("_id"):
            cand_team_ids.append(str(nested["_id"]))

    if isinstance(friend_obj.get("teams"), list):
        for t in friend_obj["teams"]:
            if isinstance(t, dict) and t.get("_id"):
                if not t.get("league") or str(t.get("league")) == str(league_id):
                    cand_team_ids.insert(0, str(t["_id"]))
                else:
                    cand_team_ids.append(str(t["_id"]))
            elif isinstance(t, str):
                cand_team_ids.append(t)

    if friend_obj.get("_id"):
        cand_team_ids.append(str(friend_obj["_id"]))

    seen = set()
    uniq = []
    for cid in cand_team_ids:
        if cid not in seen:
            seen.add(cid)
            uniq.append(cid)
    return str(display_name), uniq


async def sync_team_for_user_doc(
    db,
    user_doc: Dict[str, Any],
    countries_dict: Dict[str, Any],
    team_id: Optional[str] = None,
    league_id: Optional[str] = None,
    assigned_by: Optional[str] = None,
    strict_league_check: bool = True,
) -> Dict[str, Any]:
    """
    Pobiera skład drużyny ESM po jej team_id (technicznym tokenem aplikacji),
    mapuje skoczków do fis_code i zapisuje wynik w MongoDB (users.esm_integration).
    """
    existing = dict(user_doc.get("esm_integration") or {})
    active_league = str(league_id or existing.get("league_id") or DEFAULT_ESM_LEAGUE_ID).strip()
    active_team_id = str(team_id if team_id is not None else (existing.get("team_id") or "")).strip()
    if not active_team_id:
        raise ValueError("Użytkownik nie ma przypisanego ESM Team ID.")

    raw_by_id = await fetch_esm_team_by_id(active_team_id)
    if not raw_by_id:
        raise ValueError(f"Nie znaleziono drużyny ESM o ID: {active_team_id}.")

    athletes_by_ioc, all_athletes = await _build_athletes_lookup(db)
    my_team = parse_esm_team_payload(
        raw_by_id,
        countries_dict,
        athletes_by_ioc,
        all_athletes,
        league_id=active_league,
    )
    if not my_team or not my_team.get("team_id"):
        raise ValueError("Nie udało się sparsować składu drużyny z esmManager.")

    if strict_league_check and active_league and str(my_team.get("league_id")) != active_league:
        raise ValueError(
            f"Drużyna '{my_team.get('team_name')}' należy do innej ligi ESM ({my_team.get('league_id')}) "
            f"niż bieżący sezon ({active_league})."
        )

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    integration_doc = {
        "connected": True,
        "league_id": active_league,
        "team_id": my_team["team_id"],
        "assigned_by": assigned_by if assigned_by is not None else existing.get("assigned_by"),
        "assigned_at": now_iso if assigned_by is not None else (existing.get("assigned_at") or now_iso),
        "my_team": my_team,
        "last_synced_at": now_iso,
    }

    await db["users"].update_one(
        {"_id": ObjectId(str(user_doc["_id"]))},
        {"$set": {"esm_integration": integration_doc}},
    )
    return integration_doc


def _public_friend_summary(u_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Zwraca bezpieczny publiczny profil znajomego (tylko nazwa + drużyna ESM, bez adresu e-mail)."""
    esm = u_doc.get("esm_integration") or {}
    team = esm.get("my_team") or {}
    return {
        "user_id": str(u_doc["_id"]),
        "name": u_doc.get("name") or "Gracz FantaSKI",
        "picture": u_doc.get("picture") or "",
        "team_id": esm.get("team_id"),
        "team_name": team.get("team_name") or "Brak przypisanej drużyny",
        "value": team.get("value"),
        "total_points": team.get("total_points"),
    }


async def build_user_esm_status_payload(
    db,
    user_doc: Dict[str, Any],
    countries_dict: Optional[Dict[str, Any]] = None,
    refresh_friends_live: bool = False,
) -> Dict[str, Any]:
    """
    Buduje pełny stan integracji ESM zalogowanego użytkownika wraz z listą znajomych
    z systemu FantaSKI oraz oczekującymi zaproszeniami.
    """
    esm = dict(user_doc.get("esm_integration") or {})
    friend_ids = [str(fid) for fid in (user_doc.get("fantaski_friends") or []) if fid]
    in_ids = [str(fid) for fid in (user_doc.get("friend_requests_in") or []) if fid]
    out_ids = [str(fid) for fid in (user_doc.get("friend_requests_out") or []) if fid]

    all_rel_ids = list(dict.fromkeys(friend_ids + in_ids + out_ids))
    users_map: Dict[str, Dict[str, Any]] = {}
    if all_rel_ids:
        oids = []
        for uid in all_rel_ids:
            try:
                oids.append(ObjectId(uid))
            except Exception:
                continue
        if oids:
            cursor = db["users"].find(
                {"_id": {"$in": oids}, "is_active": {"$ne": False}},
                {"name": 1, "picture": 1, "esm_integration": 1},
            )
            async for doc in cursor:
                users_map[str(doc["_id"])] = doc

    friends_payload: List[Dict[str, Any]] = []
    for fid in friend_ids:
        f_doc = users_map.get(fid)
        if not f_doc:
            continue
        f_esm = dict(f_doc.get("esm_integration") or {})
        f_team = f_esm.get("my_team")
        if refresh_friends_live and countries_dict is not None and f_esm.get("team_id"):
            try:
                updated_esm = await sync_team_for_user_doc(
                    db=db,
                    user_doc=f_doc,
                    countries_dict=countries_dict,
                    team_id=f_esm.get("team_id"),
                    league_id=f_esm.get("league_id"),
                    strict_league_check=False,
                )
                f_team = updated_esm.get("my_team")
            except Exception as ex:
                logger.warning("Nie udało się odświeżyć składu znajomego %s: %s", fid, ex)

        if f_team and isinstance(f_team, dict):
            friends_payload.append({
                "id": fid,
                "user_id": fid,
                "display_name": f_doc.get("name") or f_team.get("team_name") or "Znajomy",
                "picture": f_doc.get("picture") or "",
                "source": "fantaski_friend",
                **f_team,
            })
        else:
            friends_payload.append({
                "id": fid,
                "user_id": fid,
                "display_name": f_doc.get("name") or "Znajomy",
                "picture": f_doc.get("picture") or "",
                "source": "fantaski_friend",
                "team_id": f_esm.get("team_id"),
                "team_name": "Brak przypisanej drużyny ESM",
                "starters": [],
                "bench": [],
                "all_players": [],
            })

    incoming_requests = [_public_friend_summary(users_map[uid]) for uid in in_ids if uid in users_map]
    outgoing_requests = [_public_friend_summary(users_map[uid]) for uid in out_ids if uid in users_map]

    is_connected = bool(esm.get("connected") and esm.get("team_id"))
    return {
        "connected": is_connected,
        "has_token": False,
        "token_email": None,
        "league_id": esm.get("league_id") or DEFAULT_ESM_LEAGUE_ID,
        "team_id": esm.get("team_id") if is_connected else None,
        "assigned_at": esm.get("assigned_at") if is_connected else None,
        "my_team": esm.get("my_team") if is_connected else None,
        "friends": friends_payload,
        "incoming_requests": incoming_requests,
        "outgoing_requests": outgoing_requests,
        "last_synced_at": esm.get("last_synced_at"),
    }


def sanitize_esm_integration_for_client(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Nigdy nie zwraca wrażliwych danych tokenów do frontendu."""
    if not doc:
        return {
            "connected": False,
            "has_token": False,
            "token_email": None,
            "league_id": DEFAULT_ESM_LEAGUE_ID,
            "team_id": None,
            "my_team": None,
            "friends": [],
            "last_synced_at": None,
        }
    is_connected = bool(doc.get("connected") and doc.get("team_id"))
    return {
        "connected": is_connected,
        "has_token": False,
        "token_email": None,
        "league_id": doc.get("league_id") or DEFAULT_ESM_LEAGUE_ID,
        "team_id": doc.get("team_id") if is_connected else None,
        "my_team": doc.get("my_team") if is_connected else None,
        "friends": doc.get("friends") or [],
        "last_synced_at": doc.get("last_synced_at"),
    }
