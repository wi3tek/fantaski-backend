# app/services/dictionary_service.py
from fastapi import Request


class DictionaryService:
    def __init__(self, dicts: dict):
        self._countries = dicts.get("countries", {})
        self._sessions = dicts.get("sessions", {})
        self._categories = dicts.get("categories", {})

    def get_flag_url(self, ioc_code: str) -> str | None:
        if not ioc_code:
            return None
        ioc_cleaned = ioc_code.strip().upper()
        country_data = self._countries.get(ioc_cleaned)

        if country_data and country_data.get("iso2"):
            iso2 = country_data["iso2"].lower()
            return f"https://flagpedia.net/data/flags/h80/{iso2}.png"
        return None

    def get_country_names(self, ioc_code: str) -> dict:
        """Zwraca słownik z nazwą PL, ENG i oryginalnym kodem IOC"""
        if not ioc_code:
            return {"code": "", "pl": "", "eng": ""}

        ioc_cleaned = ioc_code.strip().upper()
        country_data = self._countries.get(ioc_cleaned)

        if country_data:
            return {
                "code": ioc_cleaned,
                "pl": country_data.get("pl", ioc_cleaned),
                "eng": country_data.get("eng", ioc_cleaned)
            }
        return {"code": ioc_cleaned, "pl": ioc_cleaned, "eng": ioc_cleaned}

    def get_session_names(self, session_type: str) -> dict:
        """Mapuje kod sesji (np. QUA) na nazwę PL i ENG oraz zwraca surowy kod"""
        if not session_type:
            return {"code": "", "pl": "", "eng": ""}

        st_upper = session_type.strip().upper()
        if st_upper in self._sessions:
            return {
                "code": st_upper,
                "pl": self._sessions[st_upper]["pl"],
                "eng": self._sessions[st_upper]["eng"]
            }

        # Zabezpieczenie fallback (zawiera słowa kluczowe)
        if "QUA" in st_upper:
            return {"code": st_upper, "pl": "Kwalifikacje", "eng": "Qualifications"}
        if "COMP" in st_upper:
            return {"code": st_upper, "pl": "Konkurs", "eng": "Competition"}

        return {"code": session_type, "pl": session_type, "eng": session_type}

    def get_category_names(self, category_code: str) -> dict:
        """Mapuje kod kategorii (np. WC) na nazwę PL i ENG oraz zwraca surowy kod"""
        if not category_code:
            return {"code": "", "pl": "", "eng": ""}

        cat_upper = category_code.strip().upper()
        if cat_upper in self._categories:
            return {
                "code": cat_upper,
                "pl": self._categories[cat_upper]["pl"],
                "eng": self._categories[cat_upper]["eng"]
            }
        return {"code": category_code, "pl": category_code, "eng": category_code}


def get_dictionary_service(request: Request) -> DictionaryService:
    return DictionaryService(request.app.state.dicts)