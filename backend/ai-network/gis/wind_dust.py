"""Wind and dust risk by coordinate. Wind reuses weather.py's forecast; dust is Open-Meteo's
free Air Quality API (no key)."""
from __future__ import annotations
import json, urllib.parse, urllib.request
from typing import Any

AIR_QUALITY = "https://air-quality-api.open-meteo.com/v1/air-quality"


def _get(url: str, params: dict) -> dict:
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=15) as r:
        return json.loads(r.read())


def dust_risk(lat: float, lon: float) -> dict[str, Any] | None:
    try:
        data = _get(AIR_QUALITY, {"latitude": lat, "longitude": lon, "hourly": "dust,pm10",
                                   "forecast_days": 3, "timezone": "auto"})
    except Exception:
        return None
    dust = [v for v in data.get("hourly", {}).get("dust", []) if v is not None]
    if not dust:
        return None
    avg, peak = sum(dust) / len(dust), max(dust)
    return {"avg_ug_m3": round(avg, 1), "peak_ug_m3": round(peak, 1),
            "risk": "high" if avg > 150 else "medium" if avg > 50 else "low",
            "source": "Open-Meteo Air Quality"}


def wind_risk(forecast: dict | None) -> dict[str, Any] | None:
    speeds = [v for v in (forecast or {}).get("daily", {}).get("wind_speed_10m_max", []) if v is not None]
    if not speeds:
        return None
    peak = max(speeds)
    return {"peak_kmh": peak, "risk": "high" if peak > 50 else "medium" if peak > 30 else "low"}