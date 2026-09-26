"""Seed the database with a "heatwave day" for the demo (Hydro Monitor plan 5.1 M6).

48 hours of synthetic readings for beds F1 and F2, ending now, in the plan's log format: temperatures
climbing to ~39 °C, dry midday air, and F1 drying about 40 % faster than F2 (a blocked drip emitter, which
the Data & Analytics department should spot). The master's own rule is simulated too: soil below the
minimum runs the pump for pump_seconds and logs activated / deactivated. Starts plan version 1 with the
starting ranges if the farm has no plan yet, and adds the farmer's note from the plan's worked example.

    python seed_demo.py            # add 48 h ending now
    python seed_demo.py --reset    # delete data/hydro.db first
"""

from __future__ import annotations

import argparse
import math
import random
from datetime import timedelta

import db
from farm import field_kinds, load_farm, local_now
from knowledge import KIND_DEVICE, KIND_UNIT

STEP_S = 60                                 # one row per sensor per minute (the real master sends every ~10 s)
DRYING = {"F1": 3.0, "F2": 1.85}            # %/h in the midday heat; F1 dries ~40 % faster
PUMP_GAIN_PER_S = 5.0 / 120                 # a 120 s drip run lifts the bed by ~5 %
TANK_PER_RUN = 1.5                          # % of the shared tank per 120 s run


def temperature(t, day_offset: int) -> float:
    """Diurnal curve: ~28 °C before dawn, peaking around 14:00; each day ~1.5 °C hotter (a heatwave building)."""
    hour = t.hour + t.minute / 60
    peak = 38.6 + 1.6 * day_offset             # yesterday 38.6 °C (plan 6.3), today ~40
    low = 28.0 + 0.8 * day_offset
    x = math.cos((hour - 14) / 24 * 2 * math.pi)
    return low + (peak - low) * (x + 1) / 2


def light(t) -> float:
    hour = t.hour + t.minute / 60
    if not 5.5 <= hour <= 17.7:
        return 0.0
    return 95_000 * math.sin((hour - 5.5) / 12.2 * math.pi) ** 1.3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete data/hydro.db first")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--farm", default=None)
    args = parser.parse_args()
    con = db.connect()
    if args.reset:   # empty every table (works even while server.py or the dashboard has the file open)
        with con:
            for table in ("log", "plans", "runs", "approvals", "notes"):
                con.execute(f"DELETE FROM {table}")
        con.execute("VACUUM")
    farm = load_farm(args.farm)
    kinds = field_kinds(farm["hardware"])
    modules = {f["field_id"]: (f["sensor_set"]["module_id"], f["actuator_set"]["module_id"]) for f in farm["hardware"]["fields"]}
    start = farm["limits"]["start"]
    rng = random.Random(7)
    now = local_now(farm).replace(second=0, microsecond=0)
    t = now - timedelta(hours=args.hours)
    soil = {"F1": 40.0, "F2": 41.0}
    tank = 82.0
    pump_off_at: dict[str, object] = {}
    rows = []

    def row(field, module, device, kind_type, kind, value, unit, when):
        rows.append({"field_id": field, "module_id": module, "device_id": device, "type": kind_type, "kind": kind,
                     "value": value, "unit": unit, "timestamp": when.isoformat(timespec="seconds")})

    while t <= now:
        day_offset = (t.date() - now.date()).days + 1           # yesterday = 0, today = 1
        temp = temperature(t, day_offset)
        hour = t.hour + t.minute / 60
        for field, (s_mod, a_mod) in modules.items():
            heat = max(0.0, (temp - 30) / 9)                     # 0 at 30 °C, ~1 at 39 °C
            day = 1.0 if 10 <= hour <= 18 else 0.25
            soil[field] -= DRYING[field] * max(0.15, heat) * day * STEP_S / 3600 + rng.gauss(0, 0.02)
            # the master's rule (plan 2.5): soil below the minimum -> pump on for pump_seconds, unless the tank is low
            if field in pump_off_at and t >= pump_off_at[field]:
                row(field, a_mod, "relay1", "action", "pump", "deactivated", "", t)
                del pump_off_at[field]
            if field not in pump_off_at and soil[field] < start[field]["soil_moisture"][0] and tank >= 15:
                seconds = start[field]["pump_seconds"]
                row(field, a_mod, "relay1", "action", "pump", "activated", "", t + timedelta(seconds=1))
                pump_off_at[field] = t + timedelta(seconds=seconds + 1)
                soil[field] += PUMP_GAIN_PER_S * seconds
                tank -= TANK_PER_RUN * seconds / 120
            values = {"temp_air": temp + rng.gauss(0, 0.25),
                      "humidity": 72 - 38 * (temp - 27) / 13 + rng.gauss(0, 1.2),
                      "soil_moisture": soil[field] + rng.gauss(0, 0.15),
                      "light": light(t) * (1 + rng.gauss(0, 0.03)),
                      "level": tank + rng.gauss(0, 0.2)}
            for kind in kinds[field]:
                v = values[kind]
                row(field, s_mod, KIND_DEVICE[kind], "reading", kind, f"{max(0.0, v):.1f}" if kind != "light" else f"{max(0.0, v):.0f}",
                    KIND_UNIT[kind], t)
        t += timedelta(seconds=STEP_S)

    n = db.insert_rows(con, rows, farm["id"])
    if not db.latest_plan(con, farm["id"]):
        db.save_plan(con, farm["id"], (now - timedelta(hours=args.hours)).isoformat(timespec="seconds"), start,
                     made_by="onboarding (starting ranges)", trigger="onboarding",
                     message_en="Starting ranges from onboarding.", todos=[], flags=[])
    if not db.notes(con, "F1", farm_id=farm["id"]):
        db.add_note(con, "F1", "leaves on the west edge look pale", farm["id"])
    print(f"Seeded {n} rows for {', '.join(modules)} ({args.hours} h ending {now:%Y-%m-%d %H:%M}), "
          f"tank now {tank:.0f} %, plan version {db.latest_plan(con, farm['id'])['version']}.")


if __name__ == "__main__":
    main()
