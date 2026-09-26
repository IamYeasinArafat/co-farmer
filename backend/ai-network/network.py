"""One run of the farm agent network (Hydro Monitor plan 3.4).

    1. Trigger     every 6 hours (server.py) or the "Run now" button (app.py, POST /run)
    2. Summarise   code, no AI: summariser.py reads the window and the Open-Meteo forecast
    3. Code tools  each department's specialists count and check (agents/): code does the counting
    4. Departments the CrewAI crew judges and writes fixed-JSON reports (crew.py)
    5. Director    one plan: ranges per field, a message and to-dos for the farmer (+ Arabic)
    6. Check       code clamps every range inside the hard limits and flags it (checker.py)
    7. Save        a new version the master downloads with GET /ranges

If any step fails, nothing changes: the master keeps the last ranges.

    python network.py                 # one run on the active farm, printed
    python network.py --route hybrid  # Director on a free cloud model (needs OPENROUTER_API_KEY)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import timedelta
from typing import Any, Callable

import checker
import db
import llm
import summariser
from agents import SKILLS
from agents.base import resolve_model, worst
from farm import field_info, field_kinds, load_farm, local_now
from knowledge import KIND_FIELD

log = logging.getLogger("network")
VALID_DELAY_MIN = 5          # a plan made at 06:00 is valid from 06:05 (plan 6.3)


def department_order(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Departments with every upstream department first (Crop Science after Agri-Environment and Soil & Water)."""
    from graphlib import TopologicalSorter

    by_id = {d["id"]: d for d in groups}
    return [by_id[i] for i in TopologicalSorter({d["id"]: {u for u in d["depends_on"] if u in by_id} for d in groups}).static_order()]


def ranges_in_force(con, farm: dict[str, Any]) -> tuple[int, dict[str, dict[str, Any]]]:
    plan = db.latest_plan(con, farm["id"])
    if not plan:
        return 0, farm["limits"]["start"]
    return plan["version"], {f: v for f, v in plan["ranges"].items() if f not in ("version", "valid_from")}


# ── 3. code tools ────────────────────────────────────────────────────────
def code_tools(farm: dict[str, Any], summary: dict[str, Any], current: dict[str, dict[str, Any]],
               groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{dept_id: {field_id: {'status', 'issues', 'specialists': {name: summary}}}} for every field with data."""
    out: dict[str, dict[str, Any]] = {d["id"]: {} for d in groups}
    hard = farm["limits"]["hard"]
    advice_done: set[str] = set()       # farm-level (advice-only) departments run once, on the first field
    for field_id in field_kinds(farm["hardware"]):
        history = summary["history"].get(field_id) or []
        if not history:
            continue
        state = {
            "farm": {"crop": farm["profile"]["crop"], "name": farm["profile"]["name"], "fertilizer": farm["profile"].get("fertilizer"),
                     "profile": farm["profile"]},
            "reading": history[-1], "history": history, "archive": summary["archive"][field_id],
            "interval_minutes": summariser.STEP_MIN, "summary": summary["fields"][field_id],
            "field": field_info(farm, field_id),
            "ranges_override": {KIND_FIELD[k]: tuple(v) for k, v in current.get(field_id, {}).items() if k in KIND_FIELD},
            "acceptable_override": {KIND_FIELD[k]: tuple(v) for k, v in hard.items() if k in KIND_FIELD},
            "findings": {}, "reports": {}, "purpose": "briefing", "llm_mode": "off",
        }
        for dept in department_order(groups):
            if dept.get("advice_only"):
                if dept["id"] in advice_done:
                    continue
                advice_done.add(dept["id"])
            specialists = {}
            issues = []
            for spec in dept["specialists"]:
                agent = {**spec, "reads_from": dept["depends_on"], "sources": [field_id]}
                finding = SKILLS[spec["skill"]](agent, state)
                state["findings"][spec["id"]] = finding
                specialists[spec["name"]] = finding
                issues += finding.get("issues", [])
            state["reports"][dept["id"]] = {"issues": issues}
            out[dept["id"]][field_id] = {"status": worst([f.get("status", "OK") for f in specialists.values()]),
                                         "issues": issues, "specialists": specialists}
    return out


# ── 4. department briefs ─────────────────────────────────────────────────
DEPT_KEYS = {   # which summary lines each department reads
    "agri_environment": ["temp_air", "humidity", "light", "yesterday.temp_air_max", "forecast_today", "forecast_tomorrow"],
    "soil_water": ["soil_moisture", "level", "yesterday", "today", "forecast_today"],
    "crop_science": ["temp_air", "humidity", "light", "farmer_note"],
    "data_analytics": [],
}


def _lines(summary: dict[str, Any], keys: list[str]) -> str:
    parts = []
    for key in keys:
        top, _, sub = key.partition(".")
        value = summary.get(top)
        if sub and isinstance(value, dict):
            value = value.get(sub)
            top = key
        if value is not None:
            parts.append(f"{top}: {json.dumps(value, ensure_ascii=False)}")
    return "; ".join(parts)


def _fmt_range(setting: str, value: Any) -> str:
    return f"{value} s" if setting == "pump_seconds" else f"{value[0]:g}–{value[1]:g}"


def department_brief(dept: dict[str, Any], farm: dict[str, Any], summary: dict[str, Any], tools: dict[str, Any],
                     current: dict[str, dict[str, Any]], upstream: list[str] | None = None) -> str:
    profile = farm["profile"]
    fields = field_kinds(farm["hardware"])
    lines = [f"Farm: {profile['name']}, {profile['location']}; {profile['crop']}; local time {summary['now']}."]
    for f in fields:
        info = field_info(farm, f)
        lines.append(f"Field {f}: {info.get('type', '')}, day {info.get('age_days', '?')} after planting ({info.get('stage', '?')}); "
                     f"sensors: {', '.join(fields[f])}.")
    keys = DEPT_KEYS.get(dept["id"], [k for s in dept["specialists"] for k in s.get("kinds", [])])
    if keys:
        lines.append("24-hour summary per field (code):")
        for f in fields:
            text = _lines(summary["fields"].get(f, {}), keys)
            if text:
                lines.append(f"- {f}: {text}")
    lines.append("Results from your specialists' code tools:")
    for f, result in tools.get(dept["id"], {}).items():
        for name, finding in result["specialists"].items():
            lines.append(f"- {f} · {name}: {finding.get('summary')}")
    if dept["id"] == "data_analytics" and summary.get("compare"):
        lines.append(f"- Field comparison (code): {summary['compare']}.")
    if dept.get("advice_only"):
        advice = next(iter(tools.get(dept["id"], {}).values()), {}).get("specialists", {})
        suggestions = [s for f in advice.values() for s in f.get("suggestions", [])]
        if suggestions:
            lines.append("Suggestions from the Profitability tool: " + "; ".join(f"{s['title']}: {s['detail']}" for s in suggestions))
    if dept["sets"]:
        hard = farm["limits"]["hard"]
        if "soil_moisture" in dept["sets"] or "pump_seconds" in dept["sets"]:
            lines.append(WATERING)
        lines.append("Settings you may change (in force now | hard limits):")
        for f in fields:
            for s in dept["sets"]:
                if s in current.get(f, {}):
                    lines.append(f"- {f} {s}: {_fmt_range(s, current[f][s])} | {hard[s][0]:g}–{hard[s][1]:g}")
    if upstream:
        lines.append("What the departments you depend on reported (do not repeat them; say what they mean for the plants):")
        lines += [f"- {line}" for line in upstream]
    lines.append(f"Your job: {dept['agent']['goal']}.")
    return "\n".join(lines)


def digest(report: dict[str, Any]) -> str:
    """One compact line per department report for the next stage (instead of the raw JSON)."""
    parts = [f"[{report.get('code')}] {report['summary']}"]
    if report.get("warnings"):
        parts.append("Warnings: " + "; ".join(report["warnings"][:3]))
    if report.get("todos"):
        parts.append("To-dos: " + "; ".join(report["todos"][:3]))
    proposals = [f"{r['field_id']} {r['kind']} {r['min']:g}-{r['max']:g} ({r['reason']})" for r in report.get("ranges", [])]
    proposals += [f"{p['field_id']} pump_seconds {p['seconds']} ({p['reason']})" for p in report.get("pump_seconds", [])]
    if proposals:
        parts.append("Proposes: " + "; ".join(proposals))
    return " ".join(parts)


def key_facts(farm: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    """The numbers the Director's message should rest on, straight from the summariser."""
    facts = []
    fields = summary["fields"]
    fc = next((s["forecast_today"] for s in fields.values() if s.get("forecast_today")), None)
    if fc:
        facts.append(f"Forecast today: up to {fc['temp_max']} °C around {fc['hottest_hour']}"
                     + (f", above 32 °C {fc['above_32C']}" if fc.get("above_32C") else "") + ".")
    for f, s in fields.items():
        bits = []
        if s.get("soil_moisture"):
            bits.append(f"soil moisture {s['soil_moisture']['now']} % (trend {s['soil_moisture']['trend_per_h']:+} %/h)")
        if s.get("yesterday", {}).get("pump_runs") is not None:
            bits.append(f"pump ran {s['yesterday']['pump_runs']} times yesterday")
        if s.get("level"):
            bits.append(f"tank {s['level']['now']} %")
        if s.get("farmer_note"):
            bits.append(f"farmer's note: '{s['farmer_note']}'")
        facts.append(f"{f}: " + ", ".join(bits) + ".")
    if summary.get("compare"):
        facts.append(summary["compare"] + ".")
    return facts


def capabilities(farm: dict[str, Any]) -> str:
    """What the master can actually switch, so nobody promises a fan the farm doesn't have."""
    kit = sorted({d.get("what", d["kind"]) for f in farm["hardware"]["fields"] for d in f["actuator_set"]["devices"]})
    return (f"The system can only switch: {', '.join(kit)}. " + WATERING
            + " Anything else, such as shade cloth, cooling or checking drip lines, is a to-do for the farmer; "
              "the system never does it.")


WATERING = ("How the ranges work: the master waters a bed when its soil moisture falls BELOW the soil_moisture minimum, "
            "for pump_seconds. To water a bed earlier or more (a bed that dries fast, a heatwave), RAISE its minimum "
            "(e.g. 30 -> 35) or its pump_seconds; lowering them means LESS water. temp_air and humidity ranges only decide "
            "when an alert fires; change them only if a report asks for it.")


def suggested_todos(summary: dict[str, Any], crops: dict[str, Any] | None = None) -> list[str]:
    """Candidate to-dos computed by code from the facts; the Director keeps the ones that fit."""
    todos, fields = [], summary["fields"]
    fc = next((s["forecast_today"] for s in fields.values() if s.get("forecast_today")), None)
    if fc and fc["temp_max"] >= 35 and fc.get("hottest_hour"):
        h = int(fc["hottest_hour"][:2])
        todos.append(f"Put the shade cloth over the beds from {h - 2:02d}:00 to {h + 2:02d}:00 (forecast {fc['temp_max']} °C)")
    compare = summary.get("compare")
    if compare:
        todos.append(f"Check the drip emitters in {compare.split()[0]}: it is drying much faster than the other bed")
    for f, s in fields.items():
        note = s.get("farmer_note")
        if note and any(w in note.lower() for w in ("pale", "yellow", "spot", "burn", "scorch", "wilt")):
            todos.append(f"Send a photo of {f}: '{note}'")
        if s.get("level") and s["level"]["now"] < 30:
            todos.append(f"Refill the tank (now {s['level']['now']} %)")
    choices: dict[str, list[str]] = {}   # beds with the same best crop share one to-do
    best_row: dict[str, dict[str, Any]] = {}
    for field_id, f in ((crops or {}).get("fields") or {}).items():
        if f.get("current_crop") in (None, "", "undecided") and f.get("now"):
            best = f["now"][0]
            choices.setdefault(best["crop"], []).append(field_id)
            best_row[best["crop"]] = best
    for crop, fields in choices.items():
        best = best_row[crop]
        alt = next((f["now"][1]["name"] for f in crops["fields"].values() if len(f.get("now", [])) > 1), None)
        todos.append(f"Choose a crop for {' and '.join(fields)}: {best['name']} fits now (ready in about {best['days']} days, "
                     f"{best['water']} water, {best['confidence']} price)" + (f"; {alt} is the next best" if alt else ""))
    return todos


def director_brief(farm: dict[str, Any], summary: dict[str, Any], version: int, current: dict[str, dict[str, Any]],
                   reports: dict[str, dict[str, Any]], crops: dict[str, Any] | None = None,
                   money: dict[str, Any] | None = None) -> str:
    hard = farm["limits"]["hard"]
    fields = field_kinds(farm["hardware"])
    lines = [f"Farm: {farm['profile']['name']}; {farm['profile']['crop']}; local time {summary['now']}.",
             f"Ranges in force now (version {version}); they stay as they are unless you list a change:"]
    for f in fields:
        lines.append(f"- {f}: " + ", ".join(f"{s} {_fmt_range(s, v)}" for s, v in current.get(f, {}).items()))
    lines.append("Hard limits (a change must stay inside): " + ", ".join(f"{s} {v[0]:g}-{v[1]:g}" for s, v in hard.items()))
    lines.append("Key facts (code):")
    lines += [f"- {fact}" for fact in key_facts(farm, summary)]
    lines.append(capabilities(farm))
    todos = suggested_todos(summary, crops)
    if todos:
        lines.append("Suggested to-dos from the code tools (keep the ones the reports support, reword freely):")
        lines += [f"- {t}" for t in todos]
    lines.append("Department reports:")
    lines += [f"- {digest(r)}" for r in reports.values() if not r.get("advice_only")]
    for r in (r for r in reports.values() if r.get("advice_only")):
        lines.append(f"{r['name']} advice (optional, never changes a range): {r['summary']}")
    if money:
        import farm_plan

        lines.append("Farm finances (code): " + farm_plan.summary_text(money).replace("\n", " ")
                     + " If the farm loses money or a better crop combination pays clearly more, make that one to-do.")
    if crops and any(f.get("current_crop") in (None, "", "undecided") for f in crops["fields"].values()):
        import crop_advice

        lines.append("No crop is chosen yet on some beds. Crop suggestions (code): " + crop_advice.summary_text(crops)
                     + " Make choosing and planting a crop the first to-do, naming the best option and why.")
    lines.append(
        "Write the plan. changes: only settings that should change today, each with a reason from the reports "
        "(for example a narrower soil moisture band for a bed that dries fast, or a longer pump run in a heatwave); "
        "leave it empty if nothing should change, and never widen a range to the hard limits just because you can. "
        "message: to the farmer, as 'you', at most 4 plain sentences: today's main risk with its number and time, what "
        "the system will do, and what they should do. todos: concrete tasks for the farmer (not for the system), most "
        "urgent first, for example shade cloth at the hottest hours, checking a drip line, sending a photo. "
        "Write the message and the to-dos in English, once: the system translates them into Arabic.")
    return "\n".join(lines)


def apply_changes(current: dict[str, dict[str, Any]], changes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The Director lists changes only; everything else stays as it is in force."""
    proposed = {f: dict(v) for f, v in current.items()}
    for c in changes:
        if c["field_id"] not in proposed:
            continue
        if c["setting"] == "pump_seconds":
            value = c.get("seconds") if c.get("seconds") is not None else c.get("max")
            if value is not None:
                proposed[c["field_id"]]["pump_seconds"] = value
        elif c.get("min") is not None or c.get("max") is not None:
            now = current[c["field_id"]].get(c["setting"]) or [None, None]   # one end given: keep the other
            proposed[c["field_id"]][c["setting"]] = [c["min"] if c.get("min") is not None else now[0],
                                                     c["max"] if c.get("max") is not None else now[1]]
    return proposed


def fallback_report(dept_id: str, tools: dict[str, Any]) -> dict[str, Any]:
    """When a model's reply can't be used: the code tools' own findings, so the Director still hears the numbers."""
    issues = [f"{f}: {i['message']}" for f, r in tools.get(dept_id, {}).items() for i in r["issues"]]
    return {"summary": "Model reply unusable; code-tool findings: " + ("; ".join(issues[:6]) or "nothing out of range") + ".",
            "warnings": issues[:6], "todos": [], "ranges": [], "pump_seconds": [], "by": "code"}


# ── the run ──────────────────────────────────────────────────────────────
def run(con=None, farm_id: str | None = None, trigger: str = "button", route: str | None = None,
        progress: Callable[[str], None] | None = None, translate: bool = True) -> dict[str, Any]:
    """Run once. Returns {'status': 'ok'|'failed', 'plan'?, 'error'?, ...}. Never raises."""
    con = con or db.connect()
    farm = load_farm(farm_id)
    net = farm["network"]
    step = progress or (lambda msg: log.info(msg))
    run_id = db.start_run(con, farm["id"], trigger)
    started = time.perf_counter()
    detail: dict[str, Any] = {"route": route}
    try:
        step("Summarising the last 24 h and the forecast (code)")
        summary = summariser.summarise(con, farm)
        if not any(summary["history"].values()):
            raise RuntimeError("no readings in the last 48 h: is the master sending rows?")
        version, current = ranges_in_force(con, farm)
        groups = net["departments"] + net.get("extras", [])
        step("Running the specialists' code tools")
        tools = code_tools(farm, summary, current, groups)

        routes = llm.load_routes()
        route = route if route in routes else llm.default_route()
        chosen = routes[route]
        state = {"installed_models": llm.available_models(), "fallback_model": chosen["fallback"], "model_map": chosen["map"]}
        models = {d["id"]: resolve_model(d["agent"]["llm"], state) for d in groups}
        models["director"] = resolve_model(net["director"]["agent"]["llm"], state)
        models["translator"] = models["director"]
        detail["models"] = models
        if not state["installed_models"] and not any(llm.provider_ready(m) for m in models.values()):
            raise RuntimeError("no model available: start Ollama or set a cloud API key")

        import crew   # imported late: CrewAI takes a few seconds to load

        ordered = department_order(groups)
        upstream_ids = {u for d in groups for u in d["depends_on"]}
        stages = [[d for d in ordered if d["id"] in upstream_ids], [d for d in ordered if d["id"] not in upstream_ids]]
        reports: dict[str, dict[str, Any]] = {}
        for n, stage in enumerate(stages, 1):
            if not stage:
                continue
            step(f"Stage {n}: {', '.join(d['name'] for d in stage)} (CrewAI)")
            briefs = [{**d, "brief": department_brief(d, farm, summary, tools, current,
                                                      [digest(reports[u]) for u in d["depends_on"] if u in reports])}
                      for d in stage]
            out, error = crew.run_departments(briefs, models)
            for d in stage:
                report = out.get(d["id"]) or fallback_report(d["id"], tools)
                report.setdefault("by", models[d["id"]])
                report.update(name=d["name"], code=d.get("code"), advice_only=bool(d.get("advice_only")),
                              status=worst([r["status"] for r in tools[d["id"]].values()]) if tools[d["id"]] else "OK")
                reports[d["id"]] = report

        languages = net["director"].get("languages", [])
        step("Stage 3: Farm Director writes the plan" + (" and the Arabic message" if "ar" in languages else ""))
        import crop_advice
        import farm_plan

        crops = crop_advice.suggest(farm)
        money = farm_plan.plan(farm["profile"])
        plan, error = crew.run_director(net["director"], models["director"],
                                        director_brief(farm, summary, version, current, reports, crops, money))
        if not plan:
            raise RuntimeError(f"the Farm Director gave no usable plan ({error or 'invalid JSON'})")
        insight_ids = [d for d in reports]
        translation = crew.translate(plan["message"], plan["todos"], models["translator"],
                                     [reports[d]["summary"] for d in insight_ids]) if translate and "ar" in languages else None
        if translation and translation.get("extras_ar"):
            for d, text in zip(insight_ids, translation["extras_ar"]):
                reports[d]["summary_ar"] = text

        step("Checking the plan against the hard limits (code)")
        proposed = apply_changes(current, plan["changes"])
        ranges, flags = checker.check(proposed, current, farm["limits"]["hard"], field_kinds(farm["hardware"]))
        valid_from = (local_now(farm) + timedelta(minutes=VALID_DELAY_MIN)).isoformat(timespec="seconds")
        advice = {"market": [s for per in tools.get("market_strategy", {}).values() for f in per["specialists"].values()
                             for s in f.get("suggestions", [])],
                  "crops": crops, "farm_plan": money}
        translation = translation or {}
        saved = db.save_plan(
            con, farm["id"], valid_from, ranges, proposed=proposed, flags=flags, message_en=plan["message"],
            message_ar=translation.get("message_ar"), todos=plan["todos"], todos_ar=translation.get("todos_ar"),
            reports={**reports, "_changes": plan["changes"]}, summaries=summary["fields"], advice=advice,
            made_by=models["director"], trigger=trigger)
        detail.update(seconds=round(time.perf_counter() - started), flags=len(flags), previous_version=version)
        db.finish_run(con, run_id, "ok", saved["version"], detail=detail)
        step(f"Saved version {saved['version']}: the master picks it up at its next check")
        return {"status": "ok", "plan": saved, "reports": reports, "flags": flags, "summary": summary, "detail": detail}
    except Exception as err:
        log.exception("agent run failed")
        detail["seconds"] = round(time.perf_counter() - started)
        db.finish_run(con, run_id, "failed", error=f"{type(err).__name__}: {err}", detail=detail)
        return {"status": "failed", "error": f"{err}", "detail": detail}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--route", default=None, help="model route (default: the first in farms/model_routes.json)")
    parser.add_argument("--no-arabic", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    result = run(trigger="cli", route=args.route, progress=lambda m: print(f"· {m}", flush=True), translate=not args.no_arabic)
    if result["status"] != "ok":
        print("FAILED:", result["error"])
        return
    plan = result["plan"]
    print(f"\nVersion {plan['version']} (valid from {plan['valid_from']}), {result['detail']['seconds']} s")
    for r in result["reports"].values():
        print(f"\n[{r['code']}] {r['name']} ({r['by']}): {r['summary']}")
        for w in r["warnings"][:3]:
            print(f"   ! {w}")
    print("\nRanges:", json.dumps(plan["ranges"]))
    for f in plan["flags"]:
        print(f"FLAG {f['field']} {f['setting']}: {f['why']}")
    print("\nMessage:", plan["message_en"])
    print("Arabic:", plan["message_ar"])
    for t in plan["todos"]:
        print(" -", t)


if __name__ == "__main__":
    main()
