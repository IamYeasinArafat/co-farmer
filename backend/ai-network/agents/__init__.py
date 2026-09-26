"""Agent library: the skills a network spec can assign to its agents."""

from __future__ import annotations

from typing import Any, Callable

from .analytics import anomaly, forecast, trend
from .crop_science import crop_physiology, leaf_quality, plant_health
from .diagnosis import ai_diagnosis
from .monitors import range_monitor
from .soil import fertilizer, irrigation
from .strategy import crop_fit, crop_suggest, finance_check, market_watch, profitability, web_research

SKILLS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "range_monitor": range_monitor,
    "irrigation": irrigation,
    "fertilizer": fertilizer,
    "crop_physiology": crop_physiology,
    "leaf_quality": leaf_quality,
    "plant_health": plant_health,
    "ai_diagnosis": ai_diagnosis,
    "trend": trend,
    "anomaly": anomaly,
    "forecast": forecast,
    "market_watch": market_watch,
    "crop_suggest": crop_suggest,
    "finance_check": finance_check,
    "crop_fit": crop_fit,
    "profitability": profitability,
    "web_research": web_research,
}

__all__ = ["SKILLS"]
