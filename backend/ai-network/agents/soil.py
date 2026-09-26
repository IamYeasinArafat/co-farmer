"""Soil & Water code tools for soil-bed farms (Hydro Monitor plan 3.3): Irrigation, and Nutrients & Fertilizer.

Both read the summariser's numbers for their field (state["summary"]) as well as the live readings.
"""

from __future__ import annotations

from typing import Any

from .base import finding, done
from .monitors import range_monitor


def irrigation(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Soil moisture and tank against the ranges in force, plus how fast the bed dries and how often the pump ran."""
    result = range_monitor(agent, state)
    s = state.get("summary", {})
    facts = []
    moisture = s.get("soil_moisture")
    if moisture:
        facts.append(f"soil moisture now {moisture['now']} % (24 h min {moisture['min']}, trend {moisture['trend_per_h']:+} %/h)")
    y = s.get("yesterday", {})
    if y.get("soil_drying_after_10am_per_h") is not None:
        facts.append(f"yesterday the soil dried {abs(y['soil_drying_after_10am_per_h'])} %/h after 10:00")
    if "pump_runs" in y:
        facts.append(f"pump ran {y['pump_runs']} time(s) yesterday ({y.get('pump_seconds', 0)} s in total)")
    today = s.get("today", {})
    if "pump_runs" in today:
        facts.append(f"{today['pump_runs']} run(s) so far today")
    if s.get("level"):
        facts.append(f"tank {s['level']['now']} %")
    if facts:
        result["summary"] = result["summary"].rstrip(".") + ". Irrigation: " + "; ".join(facts) + "."
    result["facts"] = result["summary"]
    return result


def fertilizer(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """No EC or N-P-K sensor on these beds, so this role gives advice from the schedule, the crop stage and notes."""
    field = state.get("field", {})
    farm = state["farm"]
    notes = state.get("summary", {}).get("farmer_note")
    issues = []
    if notes and any(w in notes.lower() for w in ("pale", "yellow", "light green", "faded")):
        issues.append({"kind": "risk", "field": "nitrogen", "direction": "low", "severity": "WARNING",
                       "message": f"Farmer reports '{notes}': could be nitrogen shortage or heat and light stress; "
                                  "no soil test or EC sensor to tell them apart",
                       "action": "Check the leaves (photo) before changing fertilizer", "cmd": "NOTIFY"})
    summary = (f"Fertilizer: {farm.get('fertilizer', 'unknown')}. Crop age {field.get('age_days', '?')} days "
               f"({field.get('stage', 'unknown stage')}). No EC or N-P-K sensor: fertilizer advice only, no automatic dosing.")
    result = finding(agent, issues, summary)
    if issues:
        result["summary"] = summary + " " + result["summary"]
    return done(result)
