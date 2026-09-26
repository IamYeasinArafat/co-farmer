"""Monitoring skill used by the Agri-Environment and Soil & Water departments."""

from __future__ import annotations

from typing import Any



from .base import check_field, finding, done, profile_for


def range_monitor(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Check each owned field against the crop's ideal and tolerated bands."""
    reading = state["reading"]
    profile = profile_for(state)
    ranges, acceptable = profile["ranges"], profile.get("acceptable", {})
    issues, readings = [], {}
    for field in agent["inputs"]:
        value = reading.get(field)
        if not isinstance(value, (int, float)):
            continue
        readings[field] = value
        if field not in ranges:
            continue
        if field == "light" and value < ranges[field][0]:
            continue  # dawn, dusk and night are normal; one reading can't show a daily light deficit
        issue = check_field(field, float(value), ranges[field], acceptable.get(field))
        if issue:
            issues.append(issue)
    result = finding(agent, issues, f"All {len(readings)} monitored readings are within the ideal band.", readings=readings)
    return done(result)
