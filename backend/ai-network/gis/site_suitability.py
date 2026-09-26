"""Score each production system against the three GIS layers."""
from __future__ import annotations
from typing import Any

SYSTEMS = ["open", "shade", "greenhouse", "hydroponic"]
PENALTY = {"wind_dust": {"open": 0.20, "shade": 0.08, "greenhouse": 0.0, "hydroponic": 0.0},
           "salinity":  {"open": 0.15, "shade": 0.10, "greenhouse": 0.03, "hydroponic": 0.0}}
BAND = {"low": 0.0, "low-medium": 0.3, "medium": 0.6, "high": 1.0, "unknown": 0.3}


def score(wind: dict | None, dust: dict | None, salinity: dict | None) -> dict[str, Any]:
    risk = {"wind_dust": max(BAND.get((wind or {}).get("risk"), 0), BAND.get((dust or {}).get("risk"), 0)),
            "salinity": BAND.get((salinity or {}).get("level"), 0)}
    scores = {s: round(1.0 - sum(PENALTY[layer][s] * risk[layer] for layer in risk), 2) for s in SYSTEMS}
    return {"scores": scores, "risk": risk}