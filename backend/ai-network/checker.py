"""Code check (Hydro Monitor plan 3.4 step 5): the Director's ranges before the master sees them.

Every range is clamped inside the hard limits; anything clamped is flagged for the dashboard. A setting the
Director left out or got wrong (not a number, min not below max, or a band widened to the whole hard-limit
span, which would switch the check off) keeps the value in force, also flagged.
"""

from __future__ import annotations

from typing import Any

from knowledge import RANGE_KINDS

KIND_LABEL = {"soil_moisture": ("soil moisture", "%"), "temp_air": ("air temperature", "°C"),
              "humidity": ("humidity", "%"), "level": ("tank level", "%"), "pump_seconds": ("pump run", "s")}


# How far one plan may move a setting from the value in force. A plan can still go all the way over a few
# versions, but one bad answer can't swing the farm (e.g. a pump run cut from 120 s to 10 s in a heatwave).
MAX_STEP = {"soil_moisture": 10.0, "temp_air": 4.0, "humidity": 15.0, "level": 10.0}
MAX_PUMP_CHANGE = 0.5          # pump_seconds may change by at most 50 % per plan


def settings_for(kinds: list[str]) -> list[str]:
    """The ranges-file settings a field can have: bands for its sensors, plus the pump run."""
    return [k for k in RANGE_KINDS if k in kinds] + ["pump_seconds"]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check(proposed: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], hard: dict[str, list[float]],
          kinds_by_field: dict[str, list[str]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """proposed / current: {field_id: {setting: [min, max] | seconds}}. Returns (ranges to save, flags)."""
    ranges: dict[str, dict[str, Any]] = {}
    flags: list[dict[str, Any]] = []

    def flag(field: str, setting: str, what: str, proposed_value: Any, used: Any, why: str) -> None:
        flags.append({"field": field, "setting": setting, "type": what, "proposed": proposed_value, "used": used, "why": why})

    for field, kinds in kinds_by_field.items():
        out: dict[str, Any] = {}
        mine = proposed.get(field) or {}
        now = current.get(field) or {}
        for setting in settings_for(kinds):
            name, unit = KIND_LABEL[setting]
            lo_hard, hi_hard = hard[setting]
            value = mine.get(setting)
            if setting == "pump_seconds":
                seconds = _num(value)
                if seconds is None:
                    out[setting] = now.get(setting)
                    if value is not None:
                        flag(field, setting, "invalid", value, out[setting], "not a number: kept the value in force")
                    continue
                used = int(min(max(seconds, lo_hard), hi_hard))
                if used != seconds:
                    edge = "above" if seconds > hi_hard else "below"
                    flag(field, setting, "clamped", seconds, used,
                         f"{seconds:g} {unit} is {edge} the {hi_hard if edge == 'above' else lo_hard:g} {unit} hard limit")
                before = _num(now.get(setting))
                if before and abs(used - before) > before * MAX_PUMP_CHANGE:
                    stepped = int(round(before * (1 + MAX_PUMP_CHANGE if used > before else 1 - MAX_PUMP_CHANGE)))
                    stepped = int(min(max(stepped, lo_hard), hi_hard))
                    flag(field, setting, "limited", seconds, stepped,
                         f"{before:g} → {seconds:g} {unit} is too big a change for one plan (at most "
                         f"{MAX_PUMP_CHANGE:.0%}): moved to {stepped} {unit}")
                    used = stepped
                out[setting] = used
                continue
            pair = value if isinstance(value, (list, tuple)) and len(value) == 2 else None
            lo, hi = (_num(pair[0]), _num(pair[1])) if pair else (None, None)
            if lo is None or hi is None or lo >= hi:
                out[setting] = now.get(setting)
                if value is not None:
                    flag(field, setting, "invalid", value, out[setting], f"not a valid {name} range: kept the one in force")
                continue
            used = [round(min(max(lo, lo_hard), hi_hard), 1), round(min(max(hi, lo_hard), hi_hard), 1)]
            if used == [lo_hard, hi_hard] and now.get(setting) and list(now[setting]) != used:
                # widening a band to the whole hard-limit span is never a real plan: it switches the check off
                flag(field, setting, "rejected", [lo, hi], now.get(setting),
                     f"{name} widened to the full {lo_hard:g}–{hi_hard:g} {unit} hard limits: kept the range in force")
                out[setting] = now.get(setting)
                continue
            if used[0] >= used[1]:
                flag(field, setting, "invalid", [lo, hi], now.get(setting), f"{name} range falls outside the hard limits: kept the one in force")
                out[setting] = now.get(setting)
                continue
            if used != [round(lo, 1), round(hi, 1)]:
                flag(field, setting, "clamped", [lo, hi], used,
                     f"{name} {lo:g}–{hi:g} {unit} goes past the {lo_hard:g}–{hi_hard:g} {unit} hard limits")
            before = now.get(setting)
            step = MAX_STEP.get(setting)
            if before and step and max(abs(used[0] - before[0]), abs(used[1] - before[1])) > step:
                limited = [round(min(max(used[0], before[0] - step), before[0] + step), 1),
                           round(min(max(used[1], before[1] - step), before[1] + step), 1)]
                if limited[0] < limited[1]:
                    flag(field, setting, "limited", used, limited,
                         f"{name} moved more than {step:g} {unit} in one plan: stepped to {limited[0]:g}–{limited[1]:g} {unit}")
                    used = limited
            out[setting] = used
        ranges[field] = out
    return ranges, flags
