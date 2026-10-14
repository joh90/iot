"""Air quality numbers and bands, all worked out in code (the 4.0 spike: the model called
PSI 146 "moderate"; NEA says unhealthy).

AQI matches aqicn.org (PLAN "Air quality"): US EPA AQI with the pre-2024 PM2.5 breakpoints,
on NEA's latest ONE-HOUR PM2.5 reading, and the highest of that and NEA's own PM10, O3, SO2
and CO sub-indices. Checked 2026-10-15 01:00, central: PM2.5 1h 93 -> 170, PM10 108,
O3 29, SO2 4, CO 17 -> AQI 170, as aqicn showed. PSI is NEA's official 24-hour index.
"""

from __future__ import annotations

# (C low, C high, I low, I high), ug/m3, EPA before the 2024 revision
PM25_BREAKPOINTS = (
    (0.0, 12.0, 0, 50), (12.1, 35.4, 51, 100), (35.5, 55.4, 101, 150), (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300), (250.5, 350.4, 301, 400), (350.5, 500.4, 401, 500),
)
AQI_BANDS = ((50, "good"), (100, "moderate"), (150, "unhealthy for sensitive groups"), (200, "unhealthy"),
             (300, "very unhealthy"))
PSI_BANDS = ((50, "good"), (100, "moderate"), (200, "unhealthy"), (300, "very unhealthy"))
# NEA's health advisory for healthy people, by 24-hour PSI
PSI_ADVICE = ((100, "normal activities"), (200, "reduce prolonged or strenuous outdoor physical exertion"),
              (300, "avoid prolonged or strenuous outdoor physical exertion"))
PM25_1H_BANDS = ((55, "band I, normal"), (150, "band II, elevated"), (250, "band III, high"))
UV_BANDS = ((2, "low"), (5, "moderate"), (7, "high"), (10, "very high"))
OTHER_SUB_INDICES = ("pm10_sub_index", "o3_sub_index", "so2_sub_index", "co_sub_index")


def _band(value: float, bands: tuple[tuple[float, str], ...], top: str) -> str:
    for limit, name in bands:
        if value <= limit:
            return name
    return top


def aqi_pm25(conc: float) -> int:
    """US EPA AQI (pre-2024 table) for a PM2.5 concentration; 500 is the top of the scale."""
    c = int(conc * 10) / 10                  # EPA truncates to 0.1 ug/m3
    for c_lo, c_hi, i_lo, i_hi in PM25_BREAKPOINTS:
        if c <= c_hi:
            return round((i_hi - i_lo) / (c_hi - c_lo) * (max(c, c_lo) - c_lo) + i_lo)
    return 500


def aqi_band(aqi: int) -> str:
    return _band(aqi, AQI_BANDS, "hazardous")


def psi_band(psi: int) -> str:
    return _band(psi, PSI_BANDS, "hazardous")


def psi_advice(psi: int) -> str:
    return _band(psi, PSI_ADVICE, "minimise outdoor activity")


def pm25_1h_band(conc: int) -> str:
    return _band(conc, PM25_1H_BANDS, "band IV, very high")


def uv_band(uv: int) -> str:
    return _band(uv, UV_BANDS, "extreme")


def aqi(pm25_1h: float, sub_indices: dict[str, float]) -> tuple[int, str]:
    """(AQI, the pollutant that sets it), as aqicn.org shows it."""
    parts = {"PM2.5": aqi_pm25(pm25_1h)}
    for key in OTHER_SUB_INDICES:
        v = sub_indices.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            parts[key.split("_")[0].upper()] = round(v)
    name = max(parts, key=lambda k: parts[k])
    return parts[name], name
