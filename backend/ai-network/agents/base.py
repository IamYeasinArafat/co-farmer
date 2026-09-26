"""Helpers shared by the code tools: range checks, status roll-up, the ranges in force, and model choice."""

from __future__ import annotations

from typing import Any

import llm
from knowledge import LABELS, crop_profile, fmt

CRITICAL_FRACTION = 0.35  # no tolerated band known: a deviation beyond this share of the ideal band is critical


def check_field(field: str, value: float, bounds: tuple[float, float],
                acceptable: tuple[float, float] | None = None) -> dict[str, Any] | None:
    """Outside the ideal band -> WARNING; outside the tolerated band -> CRITICAL."""
    low, high = bounds
    if low <= value <= high:
        return None
    direction = "high" if value > high else "low"
    if acceptable:
        critical = not acceptable[0] <= value <= acceptable[1]
    else:
        deviation = value - high if direction == "high" else low - value
        critical = deviation / (high - low) > CRITICAL_FRACTION
    return {
        "kind": "reading",
        "field": field,
        "value": value,
        "direction": direction,
        "severity": "CRITICAL" if critical else "WARNING",
        "message": f"{LABELS.get(field, field)} {direction.upper()}: {fmt(field, value)} (ideal {fmt(field, low)}–{fmt(field, high)})",
    }


def worst(statuses: list[str]) -> str:
    for level in ("CRITICAL", "WARNING"):
        if level in statuses:
            return level
    return "OK"


def finding(agent: dict[str, Any], issues: list[dict[str, Any]], ok_summary: str, **extra: Any) -> dict[str, Any]:
    for issue in issues:
        issue.setdefault("agent", agent["id"])
        issue.setdefault("sources", agent.get("sources", []))
    summary = "; ".join(i["message"] for i in issues) + "." if issues else ok_summary
    return {"status": worst([i["severity"] for i in issues]), "summary": summary, "issues": issues, **extra}


def resolve_model(requested: str | None, state: dict[str, Any]) -> str:
    """Where the assigned model runs: its route (cloud if the key is set, or local if installed),
    then the assigned model itself if installed, otherwise the fallback model."""
    installed = state.get("installed_models", [])
    requested = llm.local_name(requested) if requested else requested

    def usable(model: str | None) -> bool:
        return bool(model) and (llm.provider_ready(model) or model in installed or f"{model}:latest" in installed)

    routed = state.get("model_map", {}).get(requested or "")
    for candidate in (routed, requested, state["fallback_model"]):
        if usable(candidate):
            return candidate
    return installed[0] if installed else state["fallback_model"]   # e.g. cloud route chosen but no API key


def done(result: dict[str, Any]) -> dict[str, Any]:
    """Code tools only count and check; the department's CrewAI agent does the judging (crew.py)."""
    result["facts"] = result["summary"]
    return result


def profile_for(state: dict[str, Any]) -> dict[str, Any]:
    """The crop profile, with the ranges currently in force (the latest plan version) replacing the crop's
    ideal band where the plan sets one, so code tools check readings against what the master enforces."""
    profile = crop_profile(state["farm"]["crop"])
    ranges, acceptable = state.get("ranges_override") or {}, state.get("acceptable_override") or {}
    if not ranges and not acceptable:
        return profile
    return {**profile, "ranges": {**profile["ranges"], **ranges},
            "acceptable": {**profile.get("acceptable", {}), **acceptable}}   # the hard limits count as "tolerated"
