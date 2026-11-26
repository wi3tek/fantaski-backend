import datetime
import logging
from typing import Optional, Tuple, Dict, Any
import requests

logger = logging.getLogger(__name__)

# Predefiniowane współrzędne dla najpopularniejszych skoczni narciarskich (dla szybkości i bezbłędności)
KNOWN_VENUES: Dict[str, Tuple[float, float]] = {
    "zakopane": (49.2989, 19.9489),
    "wisla": (49.6563, 18.8596),
    "wisła": (49.6563, 18.8596),
    "szczyrk": (49.7153, 19.0287),
    "planica": (46.4764, 13.7225),
    "oberstdorf": (47.4083, 10.2794),
    "garmisch-partenkirchen": (47.4921, 11.0958),
    "innsbruck": (47.2692, 11.4041),
    "bischofshofen": (47.4144, 13.2189),
    "vikersund": (59.9678, 9.9981),
    "kulm": (47.5381, 13.9983),
    "tauplitz": (47.5381, 13.9983),
    "bad mitterndorf": (47.5550, 13.9317),
    "lahti": (60.9827, 25.6612),
    "ruka": (66.1667, 29.1500),
    "kuusamo": (66.1667, 29.1500),
    "engelberg": (46.8200, 8.4070),
    "lillehammer": (61.1153, 10.4662),
    "oslo": (59.9139, 10.7522),
    "trondheim": (63.4305, 10.3951),
    "willingen": (51.2936, 8.6111),
    "titisee-neustadt": (47.9042, 8.2167),
    "klingenthal": (50.3583, 12.4681),
    "sapporo": (43.0618, 141.3545),
    "hinzenbach": (48.3075, 14.0042),
    "rasnov": (45.5900, 25.4667),
    "courchevel": (45.4147, 6.6342),
    "lake placid": (44.2795, -73.9799),
    "iron mountain": (45.8202, -88.0626),
    "almaty": (43.2389, 76.8897),
    "chaykovsky": (56.7667, 54.1167),
    "chaikovski": (56.7667, 54.1167),
    "chaikovsky": (56.7667, 54.1167),
    "predazzo": (46.3131, 11.6022),
    "val di fiemme": (46.3131, 11.6022),
    "klingenthal": (50.3583, 12.4681),
}


class WeatherService:
    def __init__(self, db=None):
        self.db = db
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "FantaSKI-DataSKI-Prediction/1.0"
        })

    def get_coordinates(self, place: str, country: Optional[str] = None) -> Optional[Tuple[float, float]]:
        """Zwraca (szerokość_geogr, długość_geogr) dla danej miejscowości """
        if not place:
            return None

        clean_place = place.strip().lower()

        # 1. Sprawdź predefiniowane znane skocznie
        for k, coords in KNOWN_VENUES.items():
            if k in clean_place or clean_place in k:
                return coords

        # 2. Sprawdź cache bazy danych (jeśli dostępna)
        # Note: Wywołanie synchroniczne na potrzeby predykcji/modelu
        try:
            # 3. Odpytaj darmowe Open-Meteo Geocoding API
            url = f"https://geocoding-api.open-meteo.com/v1/search?name={clean_place}&count=1&language=en&format=json"
            res = self.session.get(url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                results = data.get("results")
                if results and len(results) > 0:
                    r0 = results[0]
                    return float(r0["latitude"]), float(r0["longitude"])
        except Exception as e:
            logger.warning(f"Błąd geokodowania dla {place}: {e}")

        # Domyślne współrzędne europejskich Alp/Tatr (główne centrum skoków)
        return (47.5, 13.5)

    def get_weather(self, place: str, date_str: str, time_str: Optional[str] = "15:00") -> Dict[str, Any]:
        """
        Pobiera pogodę (historyczną z archiwum lub prognozę) dla danej lokalizacji i daty.
        Zwraca ustandaryzowany słownik cech pogodowych.
        """
        coords = self.get_coordinates(place)
        if not coords:
            coords = (47.5, 13.5)

        lat, lon = coords

        try:
            target_date = datetime.date.fromisoformat(date_str)
        except Exception:
            target_date = datetime.date.today()

        today = datetime.date.today()
        # Wybór godziny (domyślnie 15:00 lub wyciągnięta z parametru time_str)
        target_hour = 15
        if time_str and ":" in time_str:
            try:
                target_hour = int(time_str.split(":")[0])
            except Exception:
                target_hour = 15

        target_hour = max(0, min(23, target_hour))

        # Jeśli data jest w przeszłości (starsza niż dzisiaj), używamy Archive API
        # Jeśli dzisiaj lub w przyszłości (do 16 dni), używamy Forecast API
        is_historical = target_date < today

        try:
            if is_historical:
                url = (
                    f"https://archive-api.open-meteo.com/v1/archive?"
                    f"latitude={lat}&longitude={lon}&start_date={target_date.isoformat()}&end_date={target_date.isoformat()}&"
                    f"hourly=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation&"
                    f"wind_speed_unit=ms&timezone=auto"
                )
            else:
                url = (
                    f"https://api.open-meteo.com/v1/forecast?"
                    f"latitude={lat}&longitude={lon}&"
                    f"hourly=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation&"
                    f"wind_speed_unit=ms&timezone=auto"
                )

            res = self.session.get(url, timeout=6)
            if res.status_code == 200:
                data = res.json()
                hourly = data.get("hourly", {})
                times = hourly.get("time", [])

                # Znajdź indeks dla szukanej daty i godziny
                idx = -1
                target_prefix = f"{target_date.isoformat()}T{target_hour:02d}:00"
                for i, t in enumerate(times):
                    if t.startswith(target_prefix):
                        idx = i
                        break

                if idx == -1 and len(times) > 0:
                    # Wybierz pierwszą lub środkową godzinę
                    idx = min(target_hour, len(times) - 1)

                if idx != -1:
                    wind_speed = float(hourly.get("wind_speed_10m", [1.5])[idx] or 1.5)
                    wind_direction = float(hourly.get("wind_direction_10m", [0])[idx] or 0)
                    temp = float(hourly.get("temperature_2m", [-2.0])[idx] or -2.0)
                    pressure = float(hourly.get("surface_pressure", [960.0])[idx] or 960.0)
                    humidity = float(hourly.get("relative_humidity_2m", [75])[idx] or 75)
                    gusts = float(hourly.get("wind_gusts_10m", [wind_speed * 1.5])[idx] or (wind_speed * 1.5))
                    precip = float(hourly.get("precipitation", [0.0])[idx] or 0.0)

                    # Obliczenie wiatru czołowego/tylnego (estymacja wektorowa z kierunkiem wiatru)
                    # Większość skoczni narciarskich ma orientację lotu ~N-S (oś zeskoku ~160-200°).
                    # Wiatr pod narty (headwind) to wiatr wiający OD kierunku lądowania ku profilowi (pod kątem),
                    # domyślna oś zeskoku ~180° (lot na południe). Pozytywna wartość = wiatr w twarz = pod narty.
                    import math
                    jump_axis_deg = 180.0  # domyślna oś lotu (na południe)
                    angle_diff_rad = math.radians(wind_direction - jump_axis_deg)
                    headwind = round(wind_speed * math.cos(angle_diff_rad), 2)

                    # --- KORYTARZ STABILNOŚCI POGODOWEJ (okno konkursu: +2 godziny) ---
                    idx_end = min(len(times) - 1, idx + 2) if idx != -1 else -1
                    if idx_end != -1 and idx_end != idx:
                        wind_speed_end = float(hourly.get("wind_speed_10m", [wind_speed])[idx_end] or wind_speed)
                        wind_dir_end = float(hourly.get("wind_direction_10m", [wind_direction])[idx_end] or wind_direction)
                        temp_end = float(hourly.get("temperature_2m", [temp])[idx_end] or temp)
                        headwind_end = round(wind_speed_end * math.cos(math.radians(wind_dir_end - jump_axis_deg)), 2)

                        wind_trend_slope = round(headwind_end - headwind, 2)
                        dir_diff = abs(wind_dir_end - wind_direction)
                        direction_shift = round(min(dir_diff, 360.0 - dir_diff), 1)
                        temp_drift = round(temp_end - temp, 2)
                    else:
                        wind_trend_slope = 0.0
                        direction_shift = 0.0
                        temp_drift = 0.0

                    volatility_idx = round(max(0.0, (gusts - wind_speed) / max(0.5, wind_speed)), 2)
                    lottery_level = "HIGH" if volatility_idx >= 0.85 else ("MODERATE" if volatility_idx >= 0.35 else "LOW")

                    return {
                        "source": "Open-Meteo",
                        "latitude": lat,
                        "longitude": lon,
                        "place": place,
                        "date": target_date.isoformat(),
                        "time": f"{target_hour:02d}:00",
                        "temperature": temp,
                        "wind_speed": wind_speed,
                        "wind_direction": wind_direction,
                        "wind_gusts": gusts,
                        "surface_pressure": pressure,
                        "relative_humidity": humidity,
                        "precipitation": precip,
                        "headwind_component": headwind,
                        "wind_volatility_index": volatility_idx,
                        "wind_trend_slope": wind_trend_slope,
                        "direction_shift": direction_shift,
                        "temperature_drift": temp_drift,
                        "lottery_risk_level": lottery_level,
                        "is_forecast": not is_historical
                    }
        except Exception as e:
            logger.error(f"Błąd pobierania pogody dla {place} ({date_str}): {e}")

        # Fallback z typowymi warunkami zimowymi dla skoków narciarskich
        return {
            "source": "Default-Fallback",
            "latitude": lat,
            "longitude": lon,
            "place": place,
            "date": target_date.isoformat(),
            "time": f"{target_hour:02d}:00",
            "temperature": -2.0,
            "wind_speed": 1.5,
            "wind_direction": 180.0,
            "wind_gusts": 2.5,
            "surface_pressure": 960.0,
            "relative_humidity": 75.0,
            "precipitation": 0.0,
            "headwind_component": 1.0,
            "wind_volatility_index": 0.67,
            "wind_trend_slope": 0.0,
            "direction_shift": 0.0,
            "temperature_drift": -0.5,
            "lottery_risk_level": "MODERATE",
            "is_forecast": not is_historical
        }


_weather_service_instance: Optional[WeatherService] = None

def get_weather_service() -> WeatherService:
    global _weather_service_instance
    if _weather_service_instance is None:
        _weather_service_instance = WeatherService()
    return _weather_service_instance
