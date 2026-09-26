"""Open-Meteo: forecast, geocoding and past weather (free for non-commercial use, no API key).

The last good forecast is cached per farm (farms/<farm_id>/forecast_cache.json), so a run at a venue
with bad WiFi still gets a forecast; the summary says how old it is.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
TIMEOUT_S = 15


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=TIMEOUT_S) as response:
        return json.loads(response.read())


def geocode(name: str) -> dict[str, Any] | None:
    """'Al Khor' -> {name, country, latitude, longitude, timezone}, or None."""
    try:
        hits = _get(GEOCODE, {"name": name.split(",")[0].strip(), "count": 5, "language": "en"}).get("results", [])
    except Exception:
        return None
    if not hits:
        return None
    wanted = name.lower()
    best = next((h for h in hits if (h.get("country") or "").lower() in wanted), hits[0])
    return {k: best.get(k) for k in ("name", "country", "latitude", "longitude", "timezone")}


def fetch_forecast(lat: float, lon: float, days: int = 3) -> dict[str, Any]:
    return _get(FORECAST, {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": days,
        "hourly": "temperature_2m,relative_humidity_2m, shortwave_radiation",
        "daily": "temperature_2m_max,temperature_2m_min,relative_humidity_2m_min,precipitation_sum,wind_speed_10m_max",
    })


def forecast(lat: float, lon: float, cache: Path | None = None) -> dict[str, Any] | None:
    """Live forecast, or the cached one when offline (marked with its age)."""
    try:
        data = fetch_forecast(lat, lon)
        data["fetched_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if cache:
            cache.write_text(json.dumps(data), encoding="utf-8")
        data["offline"] = False
        return data
    except Exception:
        if cache and cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            data["offline"] = True
            return data
        return None


def day_summary(data: dict[str, Any] | None, day: date) -> dict[str, Any] | None:
    """{'temp_max', 'temp_min', 'hottest_hour', 'humidity_min', ...} for one local day."""
    if not data:
        return None
    daily, hourly = data.get("daily", {}), data.get("hourly", {})
    key = day.isoformat()
    if key not in daily.get("time", []):
        return None
    i = daily["time"].index(key)
    hours = [(t, v) for t, v in zip(hourly.get("time", []), hourly.get("temperature_2m", [])) if t.startswith(key) and v is not None]
    hottest = max(hours, key=lambda tv: tv[1])[0][11:16] if hours else None
    out = {"temp_max": daily["temperature_2m_max"][i], "temp_min": daily["temperature_2m_min"][i],
           "hottest_hour": hottest, "humidity_min": daily["relative_humidity_2m_min"][i],
           "rain_mm": daily["precipitation_sum"][i], "wind_max_kmh": daily["wind_speed_10m_max"][i]}
    hot = [t[11:16] for t, v in hours if v >= 32]
    if hot:
        out["above_32C"] = f"{hot[0]}–{hot[-1]}"
    return out


def week_outlook(lat: float, lon: float) -> str | None:
    """One line for the onboarding assistant: the coming week's temperatures."""
    try:
        d = _get(FORECAST, {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 7,
                            "daily": "temperature_2m_max,temperature_2m_min"})["daily"]
    except Exception:
        return None
    return (f"next 7 days: highs {min(d['temperature_2m_max']):.0f}–{max(d['temperature_2m_max']):.0f} °C, "
            f"lows {min(d['temperature_2m_min']):.0f}–{max(d['temperature_2m_min']):.0f} °C")


def past_month(lat: float, lon: float) -> str | None:
    """One line of past weather (the last 30 days) for the onboarding assistant."""
    end = date.today() - timedelta(days=6)   # the archive lags by a few days
    try:
        d = _get(ARCHIVE, {"latitude": lat, "longitude": lon, "timezone": "auto",
                           "start_date": (end - timedelta(days=30)).isoformat(), "end_date": end.isoformat(),
                           "daily": "temperature_2m_max"})["daily"]
    except Exception:
        return None
    highs = [v for v in d["temperature_2m_max"] if v is not None]
    return f"past 30 days: average high {sum(highs) / len(highs):.0f} °C, hottest {max(highs):.0f} °C" if highs else None

def day_solar(data: dict, day) -> dict | None:
    """Average + peak solar irradiance (W/m²) and a rough daily kWh/m² for one local day."""
    hourly = data.get("hourly", {})
    key = day.isoformat()
    vals = [v for t, v in zip(hourly.get("time", []), hourly.get("shortwave_radiation", []))
            if t.startswith(key) and v is not None]
    if not vals:
        return None
    return {"avg_w_m2": round(sum(vals) / len(vals), 1), "peak_w_m2": round(max(vals), 1),
            "kwh_m2_day": round(sum(vals) / 1000, 2)}


