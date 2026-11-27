import asyncio
import datetime
import json
import random
import secrets
from typing import Any, Dict, List, Optional, Set, Tuple
from bson import ObjectId

from app.infrastructure.cache import get_cached_value, set_cached_value
from app.services.auth_service import Role, is_pro_role
from app.services.prediction.calendar_service import parse_date_range_end, parse_date_range_start
from app.services.scrapers.scraper import fetch_fis_calendar

ALLOWED_DRAFT_CATEGORIES = {
    "WC",
    "TCS",
    "RAW_AIR",
    "PLANICA_7",
    "GP",
    "OWG",
    "WSC",
    "SFWC",
    "COC",
    "FC",
}

TCS_VENUES = {"oberstdorf", "garmisch-partenkirchen", "garmisch partenkirchen", "innsbruck", "bischofshofen"}
RAW_AIR_VENUES = {"oslo", "lillehammer", "trondheim", "vikersund"}
PLANICA_7_VENUES = {"planica"}

DEFAULT_PICKS_PER_PLAYER = 6
MIN_PICKS_PER_PLAYER = 3
MAX_PICKS_PER_PLAYER = 8
DEFAULT_DRAFT_SEASON = "2027"


class DraftEventBroker:
    """Prosty broker pub/sub w pamięci procesu do natychmiastowego dostarczania zdarzeń SSE."""

    def __init__(self, max_per_user: int = 6) -> None:
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._max_per_user = max_per_user

    def subscribe(self, user_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        subs = self._subscribers.setdefault(str(user_id), [])
        while len(subs) >= self._max_per_user:
            subs.pop(0)
        subs.append(q)
        return q

    def unsubscribe(self, user_id: str, q: asyncio.Queue) -> None:
        uid = str(user_id)
        subs = self._subscribers.get(uid)
        if not subs:
            return
        if q in subs:
            subs.remove(q)
        if not subs:
            self._subscribers.pop(uid, None)

    def publish(self, user_id: str, event_payload: Dict[str, Any]) -> None:
        uid = str(user_id)
        subs = list(self._subscribers.get(uid) or [])
        for q in subs:
            try:
                if q.full():
                    q.get_nowait()
                q.put_nowait(json.dumps(event_payload, ensure_ascii=False))
            except Exception:
                pass


draft_broker = DraftEventBroker()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _season_query_values(season: str) -> List[Any]:
    vals: List[Any] = [str(season)]
    try:
        vals.append(int(str(season)))
    except ValueError:
        pass
    return vals


def get_db_category_for_tournament(category_code: str) -> str:
    cat = str(category_code or "WC").strip().upper()
    if cat in {"TCS", "RAW_AIR", "PLANICA_7"}:
        return "WC"
    return cat


def _tournament_default_bounds(season: str, category_code: str) -> Tuple[str, str]:
    """Zwraca domyślne okno dat (from, to) dla turniejów specjalnych takich jak TCS / Raw Air / Planica 7."""
    try:
        yr = int(str(season))
    except ValueError:
        yr = datetime.date.today().year
    cat = str(category_code or "WC").strip().upper()
    if cat == "TCS":
        return f"{yr - 1}-12-27", f"{yr}-01-08"
    if cat == "RAW_AIR":
        return f"{yr}-03-01", f"{yr}-03-26"
    if cat == "PLANICA_7":
        return f"{yr}-03-15", f"{yr}-04-05"
    return "", ""


def matches_draft_tournament(session_doc: Dict[str, Any], season: str, category_code: str) -> bool:
    """Weryfikuje czy sesja należy do wybranego turnieju (z automatycznym wykrywaniem TCS / Raw Air / Planica 7)."""
    cat = str(category_code or "WC").strip().upper()
    s_cat = str(session_doc.get("category_code") or "").strip().upper()
    expected_db_cat = get_db_category_for_tournament(cat)
    if s_cat != expected_db_cat:
        return False

    place_norm = str(session_doc.get("event_place") or "").strip().lower()
    s_date = str(session_doc.get("date") or "")[:10]

    if cat == "TCS":
        if place_norm not in TCS_VENUES:
            return False
        w_from, w_to = _tournament_default_bounds(season, "TCS")
        return bool(s_date and w_from <= s_date <= w_to)

    if cat == "RAW_AIR":
        if place_norm not in RAW_AIR_VENUES:
            return False
        w_from, w_to = _tournament_default_bounds(season, "RAW_AIR")
        return bool(s_date and w_from <= s_date <= w_to)

    if cat == "PLANICA_7":
        if place_norm not in PLANICA_7_VENUES:
            return False
        w_from, w_to = _tournament_default_bounds(season, "PLANICA_7")
        return bool(s_date and w_from <= s_date <= w_to)

    return True


def get_snake_turn_user_id(order_ids: List[str], pick_index: int) -> Optional[str]:
    """
    Kolejność Snake Draft (wężyk):
    - Rundy nieparzyste (1, 3, 5...): 0 -> N-1
    - Rundy parzyste (2, 4, 6...): N-1 -> 0
    """
    n = len(order_ids)
    if n == 0 or pick_index < 0:
        return None
    round_zero = pick_index // n
    pos_in_round = pick_index % n
    if round_zero % 2 == 0:
        return order_ids[pos_in_round]
    return order_ids[n - 1 - pos_in_round]


def build_upcoming_snake_turns(
    order_ids: List[str],
    participants_by_id: Dict[str, Dict[str, Any]],
    current_pick_index: int,
    picks_per_player: int,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    n = len(order_ids)
    total_picks = n * picks_per_player
    if n == 0 or current_pick_index >= total_picks:
        return []
    upcoming = []
    for idx in range(current_pick_index, min(total_picks, current_pick_index + limit)):
        uid = get_snake_turn_user_id(order_ids, idx)
        if not uid:
            continue
        p = participants_by_id.get(uid, {})
        upcoming.append({
            "pick_number": idx + 1,
            "round": (idx // n) + 1,
            "user_id": uid,
            "name": p.get("name") or "Gracz",
            "is_current": idx == current_pick_index,
        })
    return upcoming


async def count_user_active_draft_leagues(db, user_id: str) -> int:
    return await db["draft_leagues"].count_documents({
        "participants": {
            "$elemMatch": {
                "user_id": str(user_id),
                "status": "ACCEPTED",
            }
        }
    })


async def generate_unique_invite_code(db, length: int = 8) -> str:
    """Generuje unikalny 8-znakowy kod alfanumeryczny dołączenia do ligi drafta."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(25):
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        exists = await db["draft_leagues"].find_one({"invite_code": code}, {"_id": 1})
        if not exists:
            return code
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def ensure_league_invite_code(db, league: Dict[str, Any]) -> Dict[str, Any]:
    """Uzupełnia kod dołączenia w starszych ligach, które go jeszcze nie posiadają."""
    if league and not league.get("invite_code"):
        code = await generate_unique_invite_code(db)
        await db["draft_leagues"].update_one({"_id": league["_id"]}, {"$set": {"invite_code": code}})
        league["invite_code"] = code
    return league


async def check_user_draft_eligibility(db, user_doc: Dict[str, Any]) -> Tuple[bool, str, int]:
    role = str(user_doc.get("role") or Role.USER).strip().upper()
    uid = str(user_doc.get("_id") or user_doc.get("id"))
    current_count = await count_user_active_draft_leagues(db, uid)

    if is_pro_role(role):
        return True, "", current_count
    if current_count >= 1:
        return (
            False,
            "Darmowe konto może uczestniczyć maksymalnie w 1 lidze drafta (utworzyć lub dołączyć). Przejdź na pakiet PRO, aby grać w wielu ligach.",
            current_count,
        )
    return True, "", current_count


async def _fetch_raw_fis_calendar_cached(season: str, db_cat: str) -> List[Dict[str, Any]]:
    cache_key = f"draft_fis_cal_{season}_{db_cat}"
    cached = get_cached_value("medium", cache_key)
    if cached is not None:
        return cached
    raw = await asyncio.to_thread(
        fetch_fis_calendar,
        seasoncode=season,
        categorycode=db_cat,
        gendercode="M",
        seasonmonth="",
        eventselection="actual",
    )
    if not raw:
        raw = await asyncio.to_thread(
            fetch_fis_calendar,
            seasoncode=season,
            categorycode=db_cat,
            gendercode="M",
            seasonmonth="",
            eventselection="results",
        )
    result = raw or []
    set_cached_value("medium", cache_key, result)
    return result


async def build_draft_tournament_calendar(
    db,
    season: str,
    category_code: str,
    scoring_from_date: str = "",
    scoring_to_date: str = "",
) -> Dict[str, Any]:
    """
    Buduje pełny kalendarz wybranego turnieju (rozegrane + nadchodzące przystanki FIS)
    wraz z informacją, które przystanki/konkursy biorą udział w punktacji ligi Draft.
    """
    clean_season = str(season or DEFAULT_DRAFT_SEASON).strip()
    cat = str(category_code or "WC").strip().upper()
    db_cat = get_db_category_for_tournament(cat)
    from_dt = str(scoring_from_date or "").strip()
    to_dt = str(scoring_to_date or "").strip()

    try:
        year_int = int(clean_season)
    except ValueError:
        year_int = datetime.date.today().year

    raw_events = await _fetch_raw_fis_calendar_cached(clean_season, db_cat)

    comp_docs = await db["sessions"].find(
        {
            "season": {"$in": _season_query_values(clean_season)},
            "category_code": db_cat,
            "session_type": "COMPETITION",
            "results_data.is_cancelled": {"$ne": True},
            "results_data.results.0": {"$exists": True},
        },
        {"_id": 1, "event_place": 1, "event_country": 1, "date": 1, "category_code": 1, "title": 1},
    ).sort("date", 1).to_list(length=300)

    sessions_by_place: Dict[str, List[Dict[str, Any]]] = {}
    all_matching_db_comps: List[Dict[str, Any]] = []
    for d in comp_docs:
        if not matches_draft_tournament(d, clean_season, cat):
            continue
        dt_str = str(d.get("date") or "")[:10]
        comp_item = {
            "id": str(d["_id"]),
            "event_place": d.get("event_place") or "Konkurs",
            "country": d.get("event_country") or "",
            "date": dt_str,
            "title": d.get("title") or "Konkurs",
        }
        all_matching_db_comps.append(comp_item)
        p_norm = str(comp_item["event_place"]).strip().lower()
        sessions_by_place.setdefault(p_norm, []).append(comp_item)

    today = datetime.date.today()
    tcs_from, tcs_to = _tournament_default_bounds(clean_season, "TCS")
    raw_from, raw_to = _tournament_default_bounds(clean_season, "RAW_AIR")
    pla_from, pla_to = _tournament_default_bounds(clean_season, "PLANICA_7")

    used_session_ids: Set[str] = set()
    calendar_events: List[Dict[str, Any]] = []

    for ev in raw_events:
        place = str(ev.get("place") or "").strip()
        place_low = place.lower()
        country = str(ev.get("country") or "").strip().upper()
        date_label = str(ev.get("date") or "").strip()

        if "four hills tournament" in place_low or country == "FIS":
            continue

        lbl_up = date_label.upper()
        def_yr = year_int - 1 if any(m in lbl_up for m in ("OCT", "NOV", "DEC")) else year_int
        start_dt = parse_date_range_start(date_label, def_yr)
        end_dt = parse_date_range_end(date_label, def_yr)
        if end_dt < start_dt:
            end_dt = start_dt
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()

        # Filtr turniejów specjalnych
        tournament_tag = None
        if db_cat == "WC":
            if place_low in TCS_VENUES and tcs_from <= end_iso <= tcs_to:
                tournament_tag = "TCS"
            elif place_low in RAW_AIR_VENUES and raw_from <= end_iso <= raw_to:
                tournament_tag = "RAW_AIR"
            elif place_low in PLANICA_7_VENUES and pla_from <= end_iso <= pla_to:
                tournament_tag = "PLANICA_7"

        if cat in {"TCS", "RAW_AIR", "PLANICA_7"} and tournament_tag != cat:
            continue

        matched = [
            ms for ms in sessions_by_place.get(place_low, [])
            if ms.get("date") and abs((datetime.date.fromisoformat(ms["date"]) - end_dt).days) <= 7
        ]
        for ms in matched:
            used_session_ids.add(ms["id"])

        fis_cancelled = bool(ev.get("is_cancelled")) and not bool(ev.get("has_results_on_fis"))
        if matched:
            status_str = "COMPLETED"
        elif fis_cancelled:
            status_str = "CANCELLED"
        elif end_dt < today:
            status_str = "AWAITING_RESULTS"
        else:
            status_str = "UPCOMING"

        # Czy przystanek bierze udział w punktacji?
        if matched:
            in_range_sessions = [
                ms for ms in matched
                if (not from_dt or ms["date"] >= from_dt) and (not to_dt or ms["date"] <= to_dt)
            ]
            participates = len(in_range_sessions) > 0
            counted_count = len(in_range_sessions)
        else:
            after_from = (not from_dt) or (end_iso >= from_dt)
            before_to = (not to_dt) or (start_iso <= to_dt)
            participates = after_from and before_to
            counted_count = 0

        calendar_events.append({
            "event_id": ev.get("id") or f"{place_low}_{start_iso}",
            "date_label": date_label,
            "start_date": start_iso,
            "end_date": end_iso,
            "place": place,
            "country": country,
            "url": ev.get("url") or "",
            "tournament_tag": tournament_tag,
            "status": status_str,
            "participates": participates,
            "counted_sessions_count": counted_count,
            "sessions": matched,
        })

    # Dopisz ewentualne konkursy z DB nieobecne w kalendarzu (np. starsze sezony lub niestandardowe miejscowości)
    for ms in all_matching_db_comps:
        if ms["id"] in used_session_ids:
            continue
        dt_iso = ms["date"]
        participates = ((not from_dt) or dt_iso >= from_dt) and ((not to_dt) or dt_iso <= to_dt)
        calendar_events.append({
            "event_id": ms["id"],
            "date_label": dt_iso,
            "start_date": dt_iso,
            "end_date": dt_iso,
            "place": ms["event_place"],
            "country": ms.get("country") or "",
            "url": "",
            "tournament_tag": cat if cat in {"TCS", "RAW_AIR", "PLANICA_7"} else None,
            "status": "COMPLETED",
            "participates": participates,
            "counted_sessions_count": 1 if participates else 0,
            "sessions": [ms],
        })

    calendar_events.sort(key=lambda x: (x["start_date"], x["place"]))
    return {
        "season": clean_season,
        "category_code": cat,
        "scoring_from_date": from_dt,
        "scoring_to_date": to_dt,
        "events": calendar_events,
    }


async def get_finished_competitions_for_cutoff(
    db,
    season: str,
    category_code: str,
) -> Dict[str, Any]:
    """
    Zwraca już rozegrane konkursy, kalendarz przystanków oraz sugerowane daty
    (scoring_from_date, scoring_to_date) dla konfiguratora Drafta.
    """
    cat = str(category_code or "WC").strip().upper()
    db_cat = get_db_category_for_tournament(cat)
    query = {
        "season": {"$in": _season_query_values(season)},
        "category_code": db_cat,
        "session_type": "COMPETITION",
        "results_data.is_cancelled": {"$ne": True},
        "results_data.results.0": {"$exists": True},
    }
    docs = await db["sessions"].find(
        query,
        {"_id": 1, "event_place": 1, "event_country": 1, "date": 1, "category_code": 1},
    ).sort("date", 1).to_list(length=250)

    finished_list = []
    max_date_str = ""
    for d in docs:
        if not matches_draft_tournament(d, season, cat):
            continue
        dt = str(d.get("date") or "")[:10]
        if dt > max_date_str:
            max_date_str = dt
        finished_list.append({
            "id": str(d["_id"]),
            "event_place": d.get("event_place") or "Konkurs",
            "country": d.get("event_country") or "",
            "date": dt,
        })

    cal_data = await build_draft_tournament_calendar(db, season=season, category_code=cat)
    cal_events = cal_data.get("events", [])
    upcoming_stops = [ev for ev in cal_events if ev.get("status") == "UPCOMING"]

    win_from, win_to = _tournament_default_bounds(season, cat)
    if win_from:
        recommended_from = win_from
        recommended_to = win_to
        if max_date_str and max_date_str >= win_from and max_date_str < win_to:
            next_stop = next((ev for ev in upcoming_stops if ev["start_date"] > max_date_str), None)
            if next_stop:
                recommended_from = next_stop["start_date"]
            else:
                try:
                    recommended_from = (datetime.date.fromisoformat(max_date_str) + datetime.timedelta(days=1)).isoformat()
                except ValueError:
                    pass
    else:
        recommended_to = ""
        if upcoming_stops:
            recommended_from = upcoming_stops[0]["start_date"]
        elif max_date_str:
            try:
                dt_obj = datetime.date.fromisoformat(max_date_str)
                recommended_from = (dt_obj + datetime.timedelta(days=1)).isoformat()
            except ValueError:
                recommended_from = datetime.date.today().isoformat()
        else:
            recommended_from = datetime.date.today().isoformat()

    # Buduj przejrzystą listę konkursów/przystanków do wyboru w kreatorze ("Od którego konkursu")
    competition_options: List[Dict[str, Any]] = []
    for fc in finished_list:
        competition_options.append({
            "key": f"done_{fc['id']}",
            "date": fc["date"],
            "place": fc["event_place"],
            "country": fc.get("country") or "",
            "date_label": fc["date"],
            "status": "COMPLETED",
            "label": f"✅ {fc['event_place']} ({fc['date']}) — rozegrany",
        })
    for up in upcoming_stops:
        competition_options.append({
            "key": f"up_{up['event_id']}",
            "date": up["start_date"],
            "place": up["place"],
            "country": up.get("country") or "",
            "date_label": up["date_label"],
            "status": "UPCOMING",
            "label": f"⏳ {up['place']} ({up['date_label']}) — od {up['start_date']}",
        })

    return {
        "finished_competitions": finished_list,
        "upcoming_competitions": upcoming_stops,
        "competition_options": competition_options,
        "recommended_scoring_from_date": recommended_from,
        "recommended_scoring_to_date": recommended_to,
    }


async def create_notification(
    db,
    user_id: str,
    notif_type: str,
    title: str,
    message: str,
    league_id: Optional[str] = None,
    link: Optional[str] = None,
) -> None:
    created_at = _now_iso()
    resolved_link = link if link else (f"/draft/league/{league_id}" if league_id else "/draft")
    res = await db["notifications"].insert_one({
        "user_id": str(user_id),
        "type": notif_type,
        "title": title,
        "message": message,
        "league_id": str(league_id) if league_id else None,
        "link": resolved_link,
        "is_read": False,
        "created_at": created_at,
    })
    draft_broker.publish(str(user_id), {
        "event": "notification",
        "id": str(res.inserted_id),
        "type": notif_type,
        "title": title,
        "message": message,
        "league_id": str(league_id) if league_id else None,
        "link": resolved_link,
        "created_at": created_at,
    })


async def notify_season_started_for_session(db, session_doc: Optional[Dict[str, Any]]) -> None:
    """
    Wysyła jednorazowe powiadomienie SEASON_STARTED, gdy pojawi się pierwszy ukończony
    konkurs bieżącego/przyszłego sezonu (globalnie dla PŚ oraz per aktywna liga drafta).
    """
    if not session_doc:
        return
    try:
        if str(session_doc.get("session_type") or "").upper() != "COMPETITION":
            return
        res_data = session_doc.get("results_data") or {}
        if session_doc.get("is_cancelled") is True or res_data.get("is_cancelled") is True:
            return
        results = res_data.get("results") or []
        if not results:
            return

        season_str = str(session_doc.get("season") or "").strip()
        if not season_str:
            return
        try:
            season_int = int(season_str)
        except ValueError:
            return

        # Chroni przed spamem podczas dociągania historycznych sezonów archiwalnych
        if season_int < datetime.date.today().year:
            return

        s_cat = str(session_doc.get("category_code") or "").strip().upper()
        s_date = str(session_doc.get("date") or "")[:10]
        place = str(session_doc.get("event_place") or "Konkurs").strip()

        # 1. Globalne otwarcie sezonu zimowego (pierwsze zawody WC w danym sezonie)
        if s_cat == "WC":
            existing_wc_count = await db["sessions"].count_documents({
                "season": {"$in": _season_query_values(season_str)},
                "category_code": "WC",
                "session_type": "COMPETITION",
                "results_data.is_cancelled": {"$ne": True},
                "results_data.results.0": {"$exists": True},
            })
            if existing_wc_count <= 3:
                marker_res = await db["system_events"].update_one(
                    {"_id": f"season_start_{season_str}_WC"},
                    {"$setOnInsert": {"created_at": _now_iso(), "season": season_str, "category_code": "WC", "place": place, "date": s_date}},
                    upsert=True,
                )
                if marker_res.upserted_id is not None:
                    users_cursor = db["users"].find({"is_active": {"$ne": False}}, {"_id": 1})
                    async for u in users_cursor:
                        await create_notification(
                            db=db,
                            user_id=str(u["_id"]),
                            notif_type="SEASON_STARTED",
                            title=f"Start sezonu {season_str}!",
                            message=f"Pierwszy konkurs ({place}, {s_date}) zainaugurował nowy sezon Pucharu Świata!",
                            league_id=None,
                            link="/draft",
                        )

        # 2. Powiadomienie dla aktywnych lig drafta czekających na pierwszy punktowany konkurs
        active_leagues = await db["draft_leagues"].find({
            "season": {"$in": _season_query_values(season_str)},
            "status": {"$in": ["LEAGUE_ACTIVE", "COMPLETED"]},
            "season_started_notified": {"$ne": True},
        }).to_list(length=500)

        for lg in active_leagues:
            if lg.get("status") == "COMPLETED" and not lg.get("league_started_at"):
                continue
            lg_cat = str(lg.get("category_code") or "WC").strip().upper()
            if get_db_category_for_tournament(lg_cat) != s_cat:
                continue
            if not matches_draft_tournament(session_doc, season_str, lg_cat):
                continue
            from_dt = str(lg.get("scoring_from_date") or "").strip()
            to_dt = str(lg.get("scoring_to_date") or "").strip()
            if from_dt and s_date and s_date < from_dt:
                continue
            if to_dt and s_date and s_date > to_dt:
                continue

            claimed = await db["draft_leagues"].find_one_and_update(
                {"_id": lg["_id"], "season_started_notified": {"$ne": True}},
                {"$set": {"season_started_notified": True}},
            )
            if not claimed:
                continue

            lid_str = str(lg["_id"])
            for p in lg.get("participants") or []:
                if p.get("status") == "ACCEPTED" and p.get("user_id"):
                    await create_notification(
                        db=db,
                        user_id=str(p["user_id"]),
                        notif_type="SEASON_STARTED",
                        title="Start punktowania sezonu!",
                        message=f"Sezon w lidze '{lg.get('name')}' wystartował — rozegrano pierwszy punktowany konkurs: {place} ({s_date}).",
                        league_id=lid_str,
                        link=f"/draft/league/{lid_str}",
                    )
    except Exception:
        pass


def publish_league_refresh_to_participants(league: Dict[str, Any], event_kind: str = "league_update") -> None:
    lid = str(league.get("_id") or league.get("id") or "")
    for p in league.get("participants") or []:
        uid = p.get("user_id")
        if uid:
            draft_broker.publish(str(uid), {
                "event": event_kind,
                "league_id": lid,
                "status": league.get("status"),
            })


async def mark_turn_notifications_read(db, user_id: str, league_id: str) -> None:
    await db["notifications"].update_many(
        {
            "user_id": str(user_id),
            "league_id": str(league_id),
            "type": {"$in": ["DRAFT_TURN", "DRAFT_INVITE"]},
            "is_read": False,
        },
        {"$set": {"is_read": True}},
    )


async def sync_leagues_user_profiles(db, leagues: List[Dict[str, Any]]) -> None:
    """
    Synchronizuje aktualne nazwy (name) i avatary (picture) uczestników oraz twórcy
    z kolekcji 'users', aby zmiany nazwy profilu były widoczne we wszystkich ligach drafta.
    """
    if not leagues:
        return

    raw_ids: Set[str] = set()
    for lg in leagues:
        cid = lg.get("creator_id")
        if cid:
            raw_ids.add(str(cid))
        for p in lg.get("participants") or []:
            uid = p.get("user_id")
            if uid:
                raw_ids.add(str(uid))

    oids: List[ObjectId] = []
    for raw in raw_ids:
        try:
            if ObjectId.is_valid(raw):
                oids.append(ObjectId(raw))
        except Exception:
            pass
    if not oids:
        return

    user_docs = await db["users"].find(
        {"_id": {"$in": oids}},
        {"name": 1, "picture": 1},
    ).to_list(length=len(oids))

    users_by_id: Dict[str, Dict[str, Any]] = {str(u["_id"]): u for u in user_docs}
    for lg in leagues:
        cid = str(lg.get("creator_id") or "")
        if cid in users_by_id and users_by_id[cid].get("name"):
            lg["creator_name"] = users_by_id[cid]["name"]
        for p in lg.get("participants") or []:
            uid = str(p.get("user_id") or "")
            u = users_by_id.get(uid)
            if not u:
                continue
            if u.get("name"):
                p["name"] = u["name"]
            if "picture" in u and u.get("picture") is not None:
                p["picture"] = u.get("picture") or ""


async def calculate_league_standings(db, league: Dict[str, Any]) -> Dict[str, Any]:
    """
    Zlicza punkty zawodników wybranych przez uczestników ligi wyłącznie na podstawie
    sezonu, turnieju (w tym TCS / Raw Air / Planica 7 / OWG / WSC / SFWC / GP / WC / COC / FC)
    oraz zakresu dat [scoring_from_date, scoring_to_date] (bez blokowania po ID sesji!).
    """
    await sync_leagues_user_profiles(db, [league])
    season = str(league.get("season") or DEFAULT_DRAFT_SEASON)
    category_code = str(league.get("category_code") or "WC").strip().upper()
    db_cat = get_db_category_for_tournament(category_code)
    scoring_from_date = str(league.get("scoring_from_date") or "").strip()
    scoring_to_date = str(league.get("scoring_to_date") or "").strip()

    all_comp_docs = await db["sessions"].find(
        {
            "season": {"$in": _season_query_values(season)},
            "category_code": db_cat,
            "session_type": "COMPETITION",
            "results_data.is_cancelled": {"$ne": True},
            "results_data.results.0": {"$exists": True},
        },
        {"_id": 1, "event_place": 1, "date": 1, "category_code": 1, "results_data.results": 1},
    ).sort("date", 1).to_list(length=300)

    points_by_fis: Dict[str, float] = {}
    events_by_fis: Dict[str, List[Dict[str, Any]]] = {}
    counted_competitions: List[Dict[str, Any]] = []
    excluded_competitions: List[Dict[str, Any]] = []

    for s in all_comp_docs:
        if not matches_draft_tournament(s, season, category_code):
            continue

        sid = str(s["_id"])
        s_date = str(s.get("date") or "")[:10]
        s_place = s.get("event_place") or "Konkurs"

        if scoring_from_date and s_date and s_date < scoring_from_date:
            excluded_competitions.append({
                "id": sid,
                "event_place": s_place,
                "date": s_date,
                "reason": f"Przed datą startu ({scoring_from_date})",
            })
            continue

        if scoring_to_date and s_date and s_date > scoring_to_date:
            excluded_competitions.append({
                "id": sid,
                "event_place": s_place,
                "date": s_date,
                "reason": f"Po dacie końcowej ({scoring_to_date})",
            })
            continue

        counted_competitions.append({
            "id": sid,
            "event_place": s_place,
            "date": s_date,
        })

        results = (s.get("results_data") or {}).get("results") or []
        for r in results:
            fis = r.get("fis_code")
            if fis is None:
                continue
            fis_key = str(fis).strip()
            cp = r.get("competition_points")
            pts = float(cp) if isinstance(cp, (int, float)) else 0.0
            if pts > 0:
                points_by_fis[fis_key] = points_by_fis.get(fis_key, 0.0) + pts
            events_by_fis.setdefault(fis_key, []).append({
                "session_id": sid,
                "event_place": s_place,
                "date": s_date,
                "rank": r.get("rank"),
                "points": int(pts) if pts.is_integer() else round(pts, 1),
            })

    # Hurtowe dociągnięcie zdjęć (competitor_id_from_url) dla wszystkich skoczków w składach (również w starszych ligach)
    pick_fis_ints: Set[int] = set()
    for p in league.get("participants") or []:
        for pk in p.get("picks") or []:
            try:
                if pk.get("fis_code") is not None:
                    pick_fis_ints.add(int(pk["fis_code"]))
            except (TypeError, ValueError):
                pass
    avatar_by_fis: Dict[str, Any] = {}
    if pick_fis_ints:
        ath_docs = await db["athletes"].find(
            {"fis_code": {"$in": list(pick_fis_ints)}},
            {"fis_code": 1, "competitor_id_from_url": 1},
        ).to_list(length=len(pick_fis_ints))
        for ad in ath_docs:
            if ad.get("competitor_id_from_url"):
                avatar_by_fis[str(ad["fis_code"]).strip()] = ad["competitor_id_from_url"]

    standings = []
    for p in league.get("participants") or []:
        if p.get("status") != "ACCEPTED":
            continue
        enriched_picks = []
        total_pts = 0.0
        for pk in p.get("picks") or []:
            fk = str(pk.get("fis_code"))
            comp_url_id = pk.get("competitor_id_from_url") or avatar_by_fis.get(fk)
            if comp_url_id and not pk.get("competitor_id_from_url"):
                pk["competitor_id_from_url"] = comp_url_id
            ath_pts = points_by_fis.get(fk, 0.0)
            total_pts += ath_pts
            enriched_picks.append({
                **pk,
                **({"competitor_id_from_url": comp_url_id} if comp_url_id else {}),
                "points": int(ath_pts) if ath_pts.is_integer() else round(ath_pts, 1),
                "events": events_by_fis.get(fk, []),
            })
        standings.append({
            "user_id": p["user_id"],
            "name": p["name"],
            "picture": p.get("picture", ""),
            "draft_order": p.get("draft_order", 0),
            "picks": enriched_picks,
            "picks_count": len(enriched_picks),
            "total_points": int(total_pts) if total_pts.is_integer() else round(total_pts, 1),
        })

    standings.sort(key=lambda x: (-x["total_points"], x["draft_order"], x["name"].lower()))
    for idx, row in enumerate(standings):
        # Kolejność zawsze 1..N (przy remisie punktowym decyduje kolejność wyboru w drafcie)
        row["rank"] = idx + 1

    return {
        "standings": standings,
        "counted_competitions": counted_competitions,
        "excluded_competitions": excluded_competitions,
    }


def serialize_league(
    league: Dict[str, Any],
    standings_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    participants = league.get("participants") or []
    accepted = [p for p in participants if p.get("status") == "ACCEPTED"]
    participants_by_id = {p["user_id"]: p for p in accepted}
    picks_per_player = int(league.get("picks_per_player") or DEFAULT_PICKS_PER_PLAYER)
    order_ids = league.get("draft_order_user_ids") or [p["user_id"] for p in accepted]
    total_picks = len(accepted) * picks_per_player
    curr_idx = int(league.get("current_pick_index") or 0)

    raw_status = league.get("status") or "LOBBY"
    if raw_status == "COMPLETED":
        norm_status = "LEAGUE_ACTIVE" if league.get("league_started_at") else "DRAFT_FINISHED"
    else:
        norm_status = raw_status

    current_round = (curr_idx // len(accepted) + 1) if accepted and norm_status == "ACTIVE_DRAFT" else None

    picked_map: Dict[str, str] = {}
    for p in accepted:
        for pk in p.get("picks") or []:
            picked_map[str(pk.get("fis_code"))] = p["name"]

    upcoming_turns = (
        build_upcoming_snake_turns(order_ids, participants_by_id, curr_idx, picks_per_player)
        if norm_status == "ACTIVE_DRAFT"
        else []
    )

    sanitized_participants = [
        {k: v for k, v in p.items() if k != "role"}
        for p in participants
    ]

    return {
        "id": str(league["_id"]),
        "name": league.get("name"),
        "invite_code": league.get("invite_code") or "",
        "creator_id": league.get("creator_id"),
        "creator_name": league.get("creator_name"),
        "status": norm_status,
        "category_code": league.get("category_code"),
        "season": str(league.get("season") or DEFAULT_DRAFT_SEASON),
        "picks_per_player": picks_per_player,
        "order_mode": league.get("order_mode") or "CUSTOM",
        "draft_format": "SNAKE",
        "scoring_from_date": league.get("scoring_from_date") or "",
        "scoring_to_date": league.get("scoring_to_date") or "",
        "participants": sanitized_participants,
        "draft_order_user_ids": order_ids,
        "current_pick_index": curr_idx,
        "total_picks": total_picks,
        "current_round": current_round,
        "current_turn_user_id": league.get("current_turn_user_id"),
        "upcoming_turns": upcoming_turns,
        "picked_athletes_map": picked_map,
        "started_at": league.get("started_at"),
        "completed_at": league.get("completed_at"),
        "league_started_at": league.get("league_started_at"),
        "created_at": league.get("created_at"),
        "updated_at": league.get("updated_at"),
        "standings": standings_data.get("standings", []) if standings_data else [],
        "counted_competitions": standings_data.get("counted_competitions", []) if standings_data else [],
        "excluded_competitions": standings_data.get("excluded_competitions", []) if standings_data else [],
    }


def make_random_order(user_ids: List[str]) -> List[str]:
    shuffled = list(user_ids)
    random.shuffle(shuffled)
    return shuffled


def get_current_fis_season_code() -> str:
    """Zwraca kod bieżącego sezonu PŚ FIS (lipiec–czerwiec -> rok zakończenia sezonu)."""
    today = datetime.date.today()
    return str(today.year + (1 if today.month >= 7 else 0))


async def get_current_wc_season_points_map(db) -> Tuple[str, Dict[int, float]]:
    """
    Zwraca mapę {fis_code: suma_punktów_PŚ} z bieżącego sezonu Pucharu Świata
    na podstawie sklasyfikowanych wyników oficjalnych konkursów WC w bazie 'sessions'.
    """
    current_season = get_current_fis_season_code()
    cache_key = f"draft_wc_points_map_{current_season}"
    cached = get_cached_value("short", cache_key)
    if cached is not None:
        return cached

    sessions = await db["sessions"].find(
        {
            "category_code": "WC",
            "season": {"$in": _season_query_values(current_season)},
            "session_type": {"$regex": "COMPETITION$", "$options": "i"},
            "is_cancelled": {"$ne": True},
            "results_data.is_cancelled": {"$ne": True},
            "results_data.results.0": {"$exists": True},
        },
        {"_id": 1, "race_id": 1, "raceId": 1, "results_data.results": 1},
    ).to_list(length=200)

    seen_races: Set[str] = set()
    points_by_fis: Dict[int, float] = {}
    for s in sessions:
        race_key = str(s.get("raceId") or s.get("race_id") or s.get("_id"))
        if race_key in seen_races:
            continue
        seen_races.add(race_key)
        for r in (s.get("results_data") or {}).get("results") or []:
            st = str(r.get("status") or "").strip()
            if st and st != "Classified":
                continue
            raw_fis = r.get("fis_code")
            if raw_fis is None:
                continue
            try:
                fis_int = int(raw_fis)
            except (ValueError, TypeError):
                continue
            raw_pts = r.get("competition_points")
            if raw_pts is None:
                raw_pts = r.get("world_cup_points")
            try:
                pts = float(raw_pts or 0)
            except (ValueError, TypeError):
                pts = 0.0
            if pts > 0:
                points_by_fis[fis_int] = points_by_fis.get(fis_int, 0.0) + pts

    result = (current_season, points_by_fis)
    set_cached_value("short", cache_key, result)
    return result


async def get_active_athlete_countries(db) -> List[Dict[str, Any]]:
    """Zwraca wszystkie dostępne kody narodowości aktywnych skoczków wraz z licznością."""
    cache_key = "draft_active_athlete_countries"
    cached = get_cached_value("medium", cache_key)
    if cached is not None:
        return cached

    rows = await db["athletes"].aggregate([
        {"$match": {"status": "Active", "country_code": {"$exists": True, "$nin": [None, ""]}}},
        {"$group": {"_id": "$country_code", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]).to_list(length=300)

    merged: Dict[str, int] = {}
    for row in rows:
        code = str(row.get("_id") or "").strip().upper()
        if not code:
            continue
        merged[code] = merged.get(code, 0) + int(row.get("count") or 0)

    out = [{"code": c, "count": merged[c]} for c in sorted(merged.keys())]
    set_cached_value("medium", cache_key, out)
    return out
