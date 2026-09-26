"""A farm is the four things the onboarding assistant produces (Hydro Monitor plan 3.2), one file each:

    farms/<farm_id>/profile.json    farm profile: location, fields, crop, water, fertilizer, problems
    farms/<farm_id>/hardware.json   hardware plan / installation record: sensor and actuator sets per field
    farms/<farm_id>/limits.json     hard limits and starting ranges
    farms/<farm_id>/network.json    the agent network as CrewAI agent definitions

farms/active.json names the farm the system runs.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from agents import SKILLS
from knowledge import KIND_FIELD, RANGE_KINDS, growth_stage

ROOT = Path(__file__).parent
FARMS = ROOT / "farms"
PARTS = ("profile", "hardware", "limits", "network")


def active_farm_id() -> str:
    path = FARMS / "active.json"
    return json.loads(path.read_text(encoding="utf-8"))["farm_id"] if path.exists() else "alkhor"


def set_active(farm_id: str) -> None:
    (FARMS / "active.json").write_text(json.dumps({"farm_id": farm_id}, indent=2) + "\n", encoding="utf-8")


def field_kinds(hardware: dict[str, Any]) -> dict[str, list[str]]:
    """{'F1': ['temp_air', 'humidity', ...]} from the sensor sets."""
    return {f["field_id"]: [k for d in f["sensor_set"]["devices"] for k in d["kinds"]] for f in hardware["fields"]}


def field_info(farm: dict[str, Any], field_id: str, today: date | None = None) -> dict[str, Any]:
    """Profile of one field plus its crop age and growth stage."""
    info = next((f for f in farm["profile"]["fields"] if f["field_id"] == field_id), {"field_id": field_id})
    info = dict(info)
    if info.get("planted"):
        days = ((today or date.today()) - date.fromisoformat(info["planted"])).days
        info["age_days"] = days
        info["stage"] = growth_stage(info.get("crop") or farm["profile"].get("crop"), days)
    return info


def validate_network(network: dict[str, Any], hardware: dict[str, Any]) -> dict[str, Any]:
    """Return a runnable copy. Specialists whose sensors no field has go dormant rather than failing,
    which is how one department template fits every farm."""
    net = copy.deepcopy(network)
    available = {k for kinds in field_kinds(hardware).values() for k in kinds}
    errors: list[str] = []
    net["dormant"] = []
    groups = net.get("departments", []) + net.get("extras", [])
    ids = [d["id"] for d in groups]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate department ids.")
    for dept in groups:
        for key in ("role", "goal", "backstory", "llm"):
            if not dept.get("agent", {}).get(key):
                errors.append(f"{dept.get('name', dept['id'])}: agent definition needs '{key}'.")
        for dep in dept.setdefault("depends_on", []):
            if dep not in ids:
                errors.append(f"{dept['name']} depends on unknown department '{dep}'.")
        for setting in dept.setdefault("sets", []):
            if setting not in RANGE_KINDS + ["pump_seconds"]:
                errors.append(f"{dept['name']} sets unknown setting '{setting}'.")
        active = []
        for spec in dept.get("specialists", []):
            if spec.get("skill") not in SKILLS:
                errors.append(f"{spec['name']}: unknown skill '{spec.get('skill')}'.")
            declared = spec.get("inputs", [])
            wanted = sorted(available) if declared == ["*"] else declared
            if declared and declared != ["*"] and not set(declared) & available:
                net["dormant"].append({"name": spec["name"], "department": dept["name"], "needs": declared})
                continue
            spec["kinds"] = [k for k in wanted if k in available]
            spec["inputs"] = [KIND_FIELD[k] for k in spec["kinds"] if k in KIND_FIELD]   # names the code tools use
            active.append(spec)
        dept["specialists"] = active
    for key in ("role", "goal", "backstory", "llm"):
        if not net.get("director", {}).get("agent", {}).get(key):
            errors.append(f"Farm Director definition needs '{key}'.")
    try:
        TopologicalSorter({d["id"]: d["depends_on"] for d in groups}).prepare()
    except CycleError:
        errors.append("Department dependencies contain a cycle.")
    if errors:
        raise ValueError("Invalid agent network:\n- " + "\n- ".join(errors))
    return net


def load_farm(farm_id: str | None = None) -> dict[str, Any]:
    farm_id = farm_id or active_farm_id()
    folder = FARMS / farm_id
    farm = {part: json.loads((folder / f"{part}.json").read_text(encoding="utf-8")) for part in PARTS}
    farm["id"] = farm_id
    plan_file = folder / "plan.json"          # the onboarding's farm plan (layout, crop combination, finance)
    farm["plan"] = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else None
    farm["network"] = validate_network(farm["network"], farm["hardware"])
    return farm


def save_farm(farm_id: str, parts: dict[str, Any]) -> Path:
    """Write the onboarding assistant's approved outputs."""
    folder = FARMS / farm_id
    folder.mkdir(parents=True, exist_ok=True)
    validate_network(parts["network"], parts["hardware"])
    for part in PARTS:
        (folder / f"{part}.json").write_text(json.dumps(parts[part], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if parts.get("plan"):
        (folder / "plan.json").write_text(json.dumps(parts["plan"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return folder


def list_farms() -> list[str]:
    return sorted(p.name for p in FARMS.iterdir() if (p / "profile.json").exists())


def specialist_count(network: dict[str, Any], extras: bool = False) -> int:
    groups = network["departments"] + (network.get("extras", []) if extras else [])
    return sum(len(d["specialists"]) for d in groups)


def local_now(farm: dict[str, Any]) -> datetime:
    """Now in the farm's local time (fixed offset; Qatar has no daylight saving)."""
    return datetime.now().astimezone(tz_of(farm))


def tz_of(farm: dict[str, Any]):
    from datetime import timedelta, timezone

    sign, hh, mm = farm["profile"].get("utc_offset", "+03:00")[0], *farm["profile"].get("utc_offset", "+03:00")[1:].split(":")
    delta = timedelta(hours=int(hh), minutes=int(mm))
    return timezone(delta if sign == "+" else -delta)
