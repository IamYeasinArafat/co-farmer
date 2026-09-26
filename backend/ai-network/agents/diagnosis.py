"""AI Diagnosis agent: runs the team's fine-tuned farm-qwen model (../finetuning/finetuning.py).

farm-qwen was trained on fact sheets in one exact format: code checks every sensor against the crop's
ranges, and the model writes JSON {evidence, diagnosis, severity, do, dont}. This agent rebuilds that
fact sheet from the live snapshot so the model sees what it was trained on. If farm-qwen isn't
installed, the fallback model gets the same system prompt.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import llm
from knowledge import crop_profile

from .base import resolve_model

# Copied verbatim from finetuning.py so base models behave like the fine-tune.
SYSTEM_PROMPT = (
    "You are the AI advisor for small hydroponic farms growing many different crops. You receive a fact sheet: code has already "
    "checked every sensor against the crop's ranges, which the sheet prints. Judge readings only against the printed ranges, and "
    "say so when a range is a general guide rather than crop-specific. Read the readings together, because farm problems are "
    "combinations, not single readings. Warm root water holds less oxygen, and roots short of oxygen stop drinking. Hot, dry air "
    "pulls water out of the leaves. A tank that rises while the pump is off means the pipes drained back. Distrust physically "
    "impossible readings. If a sensor is missing, don't advise actions that need its reading. If a forecast is given, prepare for it. "
    "Answer with JSON only, with the keys evidence, diagnosis, severity (OK, WATCH, WARNING or CRITICAL), do and dont. "
    "Quote the readings you rely on in evidence before you give a verdict."
)
VPD_GUIDE = (0.4, 1.6)


def _g(v: float) -> str:
    return f"{v:g}"


def _label(value: float, ideal: tuple, acceptable: tuple | None, note: str = "") -> str:
    acc = acceptable or ideal
    if value > acc[1]:
        return f"ABOVE acceptable ({_g(acc[0])}-{_g(acc[1])}{note})"
    if value < acc[0]:
        return f"BELOW acceptable ({_g(acc[0])}-{_g(acc[1])}{note})"
    if value > ideal[1]:
        return f"ABOVE ideal ({_g(ideal[0])}-{_g(ideal[1])}{note})"
    if value < ideal[0]:
        return f"BELOW ideal ({_g(ideal[0])}-{_g(ideal[1])}{note})"
    return f"OK (ideal {_g(ideal[0])}-{_g(ideal[1])}{note})"


def _vpd(air_c: float, rh: float) -> float:
    return 0.6108 * math.exp(17.27 * air_c / (air_c + 237.3)) * (1 - rh / 100)


def fact_sheet(state: dict[str, Any]) -> str:
    """Rebuild finetuning.build_fact_sheet() from the live snapshot; missing sensors print 'no sensor'."""
    r, farm = state["reading"], state["farm"]
    profile = crop_profile(farm["crop"])
    ideal, acc = profile["ranges"], profile.get("acceptable", {})
    rows: list[tuple[str, str]] = []

    def has(field: str) -> bool:
        return isinstance(r.get(field), (int, float))

    air, rh = r.get("air_temperature"), r.get("humidity")
    rows.append((f"air {air:.1f} °C", _label(air, ideal["air_temperature"], acc.get("air_temperature"))) if has("air_temperature") else ("air", "no sensor"))
    rows.append((f"humidity {rh:.0f} %", _label(rh, ideal["humidity"], acc.get("humidity"))) if has("humidity") else ("humidity", "no sensor"))
    if has("air_temperature") and has("humidity"):
        v = _vpd(air, rh)
        rows.append((f"VPD {v:.1f} kPa", ("LOW" if v < VPD_GUIDE[0] else "HIGH" if v > VPD_GUIDE[1] else "OK") + f" (general guide {_g(VPD_GUIDE[0])}-{_g(VPD_GUIDE[1])})"))
    if has("water_temperature"):
        rows.append((f"root water {r['water_temperature']:.1f} °C",
                     _label(r["water_temperature"], ideal["water_temperature"], acc.get("water_temperature"), ", general guide; no crop-specific data")))
    else:
        rows.append(("root water", "no sensor"))
    if has("water_level"):
        cap = float(farm.get("tank_litres", 20))
        litres, refill, dry = cap * r["water_level"] / 100, cap * 0.3, cap * 0.14
        state_txt = "CRITICAL LOW" if litres < dry else "LOW" if litres < refill else "OK"
        rows.append((f"tank {litres:.1f} L of {_g(cap)} L", f"{state_txt} (refill below {refill:g}; pump runs dry below {dry:g})"))
    else:
        rows.append(("tank", "no sensor"))
    rows.append(("flow", "no sensor"))
    rows.append((f"pH {r['soil_ph']:.1f}", _label(r["soil_ph"], ideal["soil_ph"], acc.get("soil_ph"))) if has("soil_ph") else ("pH", "no sensor"))
    rows.append(("EC", "no sensor"))

    now = datetime.fromisoformat(str(r["timestamp"]))
    lines = ["Crop: red amaranth (leafy)", f"Ranges: {profile.get('source', 'crop profile')}"]
    for i, (reading, verdict) in enumerate(rows):
        prefix = f"Now ({now:%H:%M})" if i == 0 else ""
        lines.append(f"{prefix:<14}{reading:<20} {verdict}")

    history = state.get("history", [])
    changes = []
    for field, name, unit in (("air_temperature", "air", "°C"), ("humidity", "humidity", "%"), ("water_level", "tank", "%")):
        series = [h[field] for h in history if isinstance(h.get(field), (int, float))]
        if len(series) >= 2 and abs(series[-1] - series[0]) >= 1:
            changes.append(f"{name} {series[-1] - series[0]:+.1f} {unit}")
    span_h = round((len(history) - 1) * state.get("interval_minutes", 15) / 60, 1) if history else 0
    lines.append(f"Last {span_h:g} hours  " + (", ".join(changes) if changes else "no big changes"))
    return "\n".join(lines)


def ai_diagnosis(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    sheet = fact_sheet(state)
    result: dict[str, Any] = {"status": "OK", "issues": [], "fact_sheet": sheet,
                              "model": {"assigned": agent.get("model"), "used": None}}
    if state.get("purpose") != "briefing" or state.get("llm_mode", "off") in ("off", "director"):
        result["summary"] = "Standing by: AI diagnosis runs when LLM reasoning is on."
        result["facts"] = result["summary"]
        return result

    model = resolve_model(agent.get("model"), state)
    answer = llm.chat_json(model, SYSTEM_PROMPT, sheet, temperature=0.1)
    if not answer:
        result["summary"] = f"{model} did not return a valid diagnosis."
        result["facts"] = result["summary"]
        return result

    def text(value: Any) -> str:
        return "; ".join(map(str, value)) if isinstance(value, list) else str(value or "")

    severity = str(answer.get("severity", "OK")).strip().upper()
    result.update(   # advisory only: status stays OK so rule-checked findings alone drive the plan and the LEDs
        summary=f"[{model} · {severity}] {text(answer.get('diagnosis'))}",
        facts=text(answer.get("evidence")),
        advice={"severity": severity, "do": answer.get("do", []), "dont": answer.get("dont", [])},
    )
    result["model"]["used"] = model
    return result

