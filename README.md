# FantaSKI - backend

FastAPI backend dla platformy **FantaSKI** — bazy wyników FIS, klasyfikacji, narzędzi analitycznych Fantasy oraz predykcji sztucznej inteligencji (PyTorch).

## Funkcjonalności
- Autoryzacja Google OAuth2 + JWT
- Role & Uprawnienia: Gość (wyniki), Użytkownik zalogowany (klasyfikacje), FantaSKI PRO (porównywarka, predykcje, wyceny Fantasy), Administrator (zarządzanie i scrapowanie)
- Pamięć podręczna (in-memory multi-tier TTLCache)
- Ochrona zasobów (slowapi rate limiting, CPU PyTorch)
- Integracja z MongoDB Atlas

## Uruchomienie lokalne
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

---

## 🧠 Moduł Predykcji AI — Sieć Neuronowa Skoków Narciarskich

### Architektura modelu

| Parametr | Wartość |
|---|---|
| Typ sieci | PyTorch MLP (Multi-Layer Perceptron) |
| Warstwy | 25 → 128 → 64 → 32 → 2 |
| Normalizacja | LayerNorm po każdej warstwie |
| Aktywacja | GELU (Gaussian Error Linear Unit) |
| Regularyzacja | Dropout 0.15 / 0.10 |
| Funkcja straty | SmoothL1Loss (Huber) |
| Optymizer | AdamW (lr=0.002, weight_decay=1e-4) |
| Scheduler | ReduceLROnPlateau (patience=4, factor=0.5) |
| Early stopping | Patience 8 epok na val_loss |
| Wyjścia | `[pred_dist_ratio, pred_points_normalized]` |

### 25 cech wejściowych sieci

| # | Cecha | Normalizacja | Opis |
|---|---|---|---|
| 0 | `norm_hill_size` | (HS - 130) / 40 | Rozmiar skoczni HS w metrach |
| 1 | `is_normal` | 0/1 | Skocznia normalna (HS < 110) |
| 2 | `is_large` | 0/1 | Skocznia duża (HS 110–199) |
| 3 | `is_flying` | 0/1 | Skocznia mamucia (HS ≥ 200) |
| 4 | `weighted_form_ratio` | 0.0–1.2 | Ważona forma (dist/HS × waga sesji × decay × siła obsady) |
| 5 | `weighted_points` | / pts_range | Średnia ważona punktów (dynamiczny zakres: 260/300/400) |
| 6 | `weighted_rank` | / 50 | Średnia ważona pozycja w wynikach |
| 7 | `consistency_sigma` | × 10 | Odchylenie standardowe dist_ratio (powtarzalność skoków) |
| 8 | `conversion_delta` | / 10 | Konwersja presji: trening → konkurs (± pozycje) |
| 9 | `hill_type_affinity` | 0.0–1.2 | Afinitet do aktualnego typu skoczni |
| 10 | `venue_affinity` | 0.0–1.2 | Afinitet do konkretnego obiektu (blend z hill_type) |
| 11 | `season_activity` | 0.0–1.0 | Aktywność w bieżącym sezonie |
| 12 | `form_trend` | × 5 | Trend formy: ostatnie 3 vs poprzednie 4 starty |
| 13 | `has_weekend_data` | 0/1 | Czy są dane z bieżącego weekendu |
| 14 | `weekend_perf_ratio` | 0.0–1.2 | Wynik weekendowy (kwal. / trening / sobotni konkurs) |
| 15 | `effective_headwind` | / 3.0 | Zinterpolowany wiatr pod/w plecy w momencie skoku |
| 16 | `wind_speed` | / 5.0 | Ogólna prędkość wiatru |
| 17 | `effective_temp` | / 15.0 | Zinterpolowana temperatura w momencie skoku |
| 18 | `jump_sequence_index` | 0.0–1.0 | Pozycja w kolejce startowej (niezależnie od BIB!) |
| 19 | `wind_volatility` | / 1.5 | Wskaźnik porywistości (porywy/śr. prędkość) |
| 20 | `wind_trend_slope` | / 2.0 | Gradient wiatru w oknie zawodów (+2h) |
| 21 | `direction_shift` | / 180.0 | Zmiana kierunku wiatru w dolinie |
| 22 | `temperature_drift` | / 5.0 | Zmiana temperatury w oknie konkursu |
| 23 | `career_starts_log` | / 7.0 | log(1 + liczba startów) — doświadczenie kariery |
| 24 | `has_wc_experience` | 0/1 | Flaga: czy zawodnik startował w Pucharze Świata |

### Hierarchia sesji i wag

```
COMPETITION (1.0) > QUALIFICATION (0.65) > TRIAL (0.40) > TRAINING (0.20)
```

Rangi zawodów:
```
WC / OWG / WSC / SFWC (1.0) > GP (0.75) > COC (0.55) > FC (0.35) > FESA (0.30)
```

Zanikanie czasowe: `weight = exp(-0.693 × Δdays / 150)` → ~1.0 teraz, ~0.18 rok temu, ~0 po 5 latach.

### Normalizacja siły obsady

Każdy skok jest ważony jakością obsady danej serii (field_quality_factor). Średni rating (dist_ratio) zawodników startujących w sesji jest porównywany z bazowym 0.93 (elita PŚ), co automatycznie koryguje wynik: skok na 130 m w zawodach COC z młodzieżową obsadą ≠ skok na 130 m w konkursie PŚ z najlepszą trzydziestką świata.

### Profile psychologiczne i techniczne

Model automatycznie klasyfikuje zawodników w 4 wymiarach:

1. **Konwersja Presji**: 🔥 Zwierzę konkursowe (≥+2.5 pozycji) | ⚖️ Równy | 📉 Syndrom treningów (≤-3.0)
2. **Powtarzalność**: 🎯 Metronom (≥82%) | 🎲 Chwiejny (≤45%)
3. **Specjalizacja**: 🦅 Lotnik (afinitet mamucich) | Uniwersalny
4. **Aero-profil wiatru**: 🪁 Glider (duży zysk pod narty, duża strata w plecy) | 🛡️ Wiatroodporny | 🌪️ Podatny na porywy

### Symulacja Monte Carlo

Model generuje 100 symulacji (konfigurowalne) z indywidualną zmiennością skoczka (sigma punktowa 3.0–10.0 pkt) modulowaną wskaźnikiem porywistości wiatru i podatnością aero-profilu. Na tej podstawie obliczane są prawdopodobieństwa: wygranej, podium, top 10, top 30 (awans do II serii).

### Kompensacja wiatru FIS (K-wind)

Współczynniki kompensacji wiatru dostosowane do regulaminu FIS:

| Typ skoczni | Metry / m·s⁻¹ | Punkty / m·s⁻¹ | Norm. punktów |
|---|---|---|---|
| Mamucia (HS ≥ 200) | 4.8 m | 9.0 pkt | /400 |
| Duża (HS 110–199) | 2.4 m | 5.4 pkt | /300 |
| Normalna (HS < 110) | 1.8 m | 3.6 pkt | /260 |

Efektywny wiatr dla każdego skoczka jest interpolowany wg pozycji w kolejce startowej × gradient wiatru w oknie konkursu.

### Obliczanie wiatru czołowego (headwind_component)

Komponent wiatru pod narty / w plecy jest obliczany wektorowo z kierunku wiatru:

```
headwind = wind_speed × cos(wind_direction° - oś_zeskoku°)
```

Domyślna oś zeskoku = 180° (lot na południe). Wartość dodatnia = wiatr pod narty (zysk), ujemna = wiatr w plecy (strata).

---

## 🔧 Zmiany eksperta (v2)

### Co było dobrze i pozostaje bez zmian:
- ✅ Hierarchia sesji (COMPETITION > QUA > TRAINING) z wagami 1.0 / 0.65 / 0.20
- ✅ Zanikanie wykładnicze z half-life 150 dni (~1 sezon)
- ✅ Normalizacja do siły obsady serii (field_quality_factor)
- ✅ Profil aero-dynamiczny (GLIDER / POWER_RESILIENT / BALANCED)
- ✅ Korytarz pogodowy z wind_trend_slope i temperature_drift
- ✅ Monte Carlo z indywidualną sigmą modulowaną gust_sensitivity
- ✅ Weekend context (kwalifikacje / treningi z bieżącego weekendu)
- ✅ Profile psychologiczne (konwersja presji, powtarzalność, specjalizacja skoczni)

### Co zmieniono:

| Zmiana | Było | Jest | Uzasadnienie |
|---|---|---|---|
| **Rozmiar wektora** | 23 cechy | **25 cech** | Dodano `career_starts_log` i `has_wc_experience` — sieć nie wiedziała, kto jest debiutantem, a kto weteranem z 500+ startami |
| **Aktywacja** | ReLU | **GELU** | GELU zapewnia gładsze gradienty i lepszą generalizację w małych sieciach (porównywalna z SiLU/Swish) |
| **Dropout** | 0.12 / 0.08 | **0.15 / 0.10** | Lekko silniejsza regularyzacja przy 25 cechach |
| **Epoki** | 20 | **40 max** | Więcej czasu na naukę; scheduler obniża LR, a early stopping (patience=8) zapobiega przeuczeniu |
| **LR Scheduler** | brak | **ReduceLROnPlateau** | Automatyczne obniżanie LR gdy walidacja przestaje się poprawiać — standard w treningu NN |
| **Early stopping** | brak | **patience=8** | Zapamiętuje najlepsze wagi (best checkpoint) i przerywa trening, gdy val_loss rośnie |
| **Normalizacja punktów** | stałe /300 | **dynamiczne /260 /300 /400** | Na skoczniach mamucich (2 serie × 200 pkt) skoczkowie zdobywają do ~400 pkt — stała /300 obcinała skalę |
| **Wind rate (K-wind)** | 3.5/2.2 | **4.8/2.4/1.8** | Dostosowane do oficjalnych współczynników kompensacji wiatru FIS |
| **Headwind estimation** | `wind_speed × 0.7` | **wektorowy cos(dir - oś)** | Estymacja `× 0.7` ignorowała kierunek wiatru. Teraz obliczamy rzut na oś zeskoku (cos). Wiatr boczny daje ~0, wiatr pod narty ~pełne wind_speed |
