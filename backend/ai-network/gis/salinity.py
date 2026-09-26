"""No free parcel-level salinity API exists on a hackathon timeline. Tiered estimate only."""
from __future__ import annotations
from typing import Any

COASTAL_TOWNS = {"al khor", "al wakrah", "doha", "mesaieed", "al thakhira", "al ruwais", "dukhan"}


def estimate(profile: dict[str, Any]) -> dict[str, Any]:
    water = str(profile.get("water") or "").lower()
    if "salt" in water or "ملح" in water or "brackish" in water:
        return {"level": "high", "basis": "farmer-reported"}
    if "well" in water or "بئر" in water:
        place = str(profile.get("location") or profile.get("place") or "").lower()
        coastal = any(t in place for t in COASTAL_TOWNS)
        return {"level": "medium" if coastal else "low-medium", "basis": "regional estimate: Qatar well water is commonly brackish, not measured for this farm"}
    return {"level": "unknown", "basis": "insufficient data — ask for a water/soil test if one exists"}