"""Crop Science department skills. These agents read the reports of the upstream departments."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from knowledge import LABELS

from .base import finding, done, profile_for

HEAT_STRESS_C = 35
PIGMENT_HEAT_C = 35


def _upstream_issues(agent: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    reports = state.get("reports", {})
    return [i for dept in agent.get("reads_from", []) for i in reports.get(dept, {}).get("issues", []) if i["kind"] == "reading"]


def _hours(rows: int, state: dict[str, Any]) -> float:
    return round(rows * state.get("interval_minutes", 15) / 60, 1)


def crop_physiology(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Translate the environment and root-zone reports into crop-level stress."""
    profile = profile_for(state)
    issues = []
    for upstream in _upstream_issues(agent, state):
        impact = profile["impacts"].get(upstream["field"], {}).get(upstream["direction"])
        if impact:
            issues.append({
                "kind": "risk", "field": upstream["field"], "direction": upstream["direction"],
                "severity": upstream["severity"], "sources": upstream.get("sources", []),
                "message": f"{LABELS.get(upstream['field'], upstream['field'])} {upstream['direction']} → {impact}",
            })

    history = state.get("history", [])
    hot = sum(1 for r in history if (r.get("air_temperature") or 0) > HEAT_STRESS_C)
    if _hours(hot, state) >= 0.5:   # "cumulative" needs time: a few hot minutes are already covered by the Climate agent
        issues.append({
            "kind": "risk", "field": "air_temperature", "direction": "high",
            "severity": "CRITICAL" if hot >= len(history) / 3 else "WARNING",
            "message": f"{_hours(hot, state)} h above {HEAT_STRESS_C}°C in the last {_hours(len(history) - 1, state)} h: cumulative heat stress",
        })
    result = finding(agent, issues, "Environment and root zone support vigorous leaf growth.")
    return done(result)


def leaf_quality(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Market quality of red leaves: betalain colour and nitrate content."""
    reading = state["reading"]
    ranges = profile_for(state)["ranges"]
    temp, light, nitrogen = reading.get("air_temperature"), reading.get("light"), reading.get("nitrogen")
    hour = datetime.fromisoformat(str(reading["timestamp"])).hour
    issues = []

    if temp is not None and temp > PIGMENT_HEAT_C:
        issues.append({"kind": "risk", "field": "air_temperature", "direction": "high",
                       "severity": "CRITICAL" if temp > 40 else "WARNING",
                       "message": f"Heat ({temp}°C) suppresses betalain pigment: expect faded red leaves and a lower market grade",
                       "action": "Prioritise cooling in the red-leaf beds; harvest mature leaves early in the morning", "cmd": "COOL"})
    if light is not None and 10 <= hour <= 15 and light < ranges["light"][0]:
        issues.append({"kind": "risk", "field": "light", "direction": "low", "severity": "WARNING",
                       "message": f"Midday light only {light:,.0f} lux: weak betalain synthesis, greener leaves",
                       "action": "Retract shade screens / clean greenhouse cover", "cmd": "SHADE_OPEN"})
    if nitrogen is not None:
        n_high = ranges["nitrogen"][1]
        if nitrogen > n_high or (nitrogen > 0.8 * n_high and light is not None and 10 <= hour <= 15 and light < ranges["light"][0]):
            issues.append({"kind": "risk", "field": "nitrogen", "direction": "high", "severity": "WARNING",
                           "message": f"Soil N {nitrogen} mg/kg: amaranth accumulates leaf nitrate — test leaves before harvest",
                           "action": "Hold nitrogen fertigation and test leaf nitrate before the next harvest", "cmd": "HOLD_FERTILIZER"})
    result = finding(agent, issues, "Leaf colour and nitrate conditions support premium-grade purple leaves.")
    return done(result)


def plant_health(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Pest and disease risk from how long conducive conditions have lasted."""
    history = state.get("history", []) or [state["reading"]]
    issues = []
    for rule in profile_for(state)["pest_disease"]:
        share = sum(1 for r in history if rule["when"](r)) / len(history)
        active = rule["when"](state["reading"])
        if active or share >= 0.2:
            issues.append({
                "kind": "risk", "field": rule["id"], "direction": "high",
                "severity": "CRITICAL" if share >= 0.5 else "WARNING",
                "message": f"{rule['name']}: conducive conditions for {share:.0%} of the window" + (" (active now)" if active else ""),
                "action": rule["action"], "cmd": rule.get("cmd", "SCOUT"),
            })
    result = finding(agent, issues, "No pest or disease conditions detected in the window.")
    return done(result)
