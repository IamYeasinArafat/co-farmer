"""Stand-in for the ESP32 master node, for testing without hardware (Hydro Monitor plan 5.1 M1, M4).

Does what the plan's master does, over the same HTTP API:
  * every 10 s: posts one row per reading (CSV, plan 2.4) for each field's sensor set to POST /log
  * checks GET /ranges/version every few minutes, downloads new ranges, keeps them in a local file (the SD card)
  * enforces them (plan 2.5): soil below the minimum -> pump on for pump_seconds (logs activated/deactivated),
    tank below its minimum -> alert and block the pump, air above its maximum -> alert (this farm has no fan)

While it runs, type:
    dry F1      pull F1's soil probe: its moisture drops, the pump should start (the demo's "live hardware" step)
    refill      fill the tank
    quit        stop

    python fake_master.py                          # http://localhost:8000, ranges checked every 5 min
    python fake_master.py --check 20 --speed 20    # demo: check ranges every 20 s, bed dries 20x faster
"""

from __future__ import annotations

import argparse
import json
import math
import random
import threading
import time
import urllib.error
import urllib.request

from farm import ROOT, field_kinds, load_farm, local_now
from knowledge import KIND_DEVICE, KIND_UNIT

SD_CARD = ROOT / "data" / "master_sd_ranges.json"


def http(url: str, body: bytes | None = None, content_type: str = "text/csv") -> tuple[int, bytes]:
    request = urllib.request.Request(url, body, {"Content-Type": content_type} if body else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--period", type=float, default=10, help="seconds between readings (plan: ~10)")
    parser.add_argument("--check", type=float, default=300, help="seconds between range checks (plan demo: 5 min)")
    parser.add_argument("--speed", type=float, default=1, help="make the beds dry and the pump run this many times faster")
    args = parser.parse_args()

    farm = load_farm()
    kinds = field_kinds(farm["hardware"])
    modules = {f["field_id"]: (f["sensor_set"]["module_id"], f["actuator_set"]["module_id"]) for f in farm["hardware"]["fields"]}
    ranges = json.loads(SD_CARD.read_text()) if SD_CARD.exists() else {"version": 0, **farm["limits"]["start"]}
    rng = random.Random()
    soil = {f: 36.0 for f in modules}
    drying = {f: (3.0 if i == 0 else 1.85) for i, f in enumerate(modules)}   # %/h at midday heat, F1 faster
    tank = 60.0
    pump_until: dict[str, float] = {}
    alerts: set[str] = set()
    stop = threading.Event()
    print(f"Fake master for {', '.join(modules)} -> {args.url}  (ranges v{ranges['version']} on the 'SD card')")
    print("Type 'dry F1', 'refill' or 'quit'.")

    def console() -> None:
        nonlocal tank
        while not stop.is_set():
            try:
                cmd = input().strip().lower().split()
            except (EOFError, KeyboardInterrupt):
                return
            if not cmd:
                continue
            if cmd[0] == "quit":
                stop.set()
            elif cmd[0] == "dry" and len(cmd) > 1 and cmd[1].upper() in soil:
                soil[cmd[1].upper()] = 18.0
                print(f"  probe pulled out of {cmd[1].upper()}: soil moisture reads 18 %")
            elif cmd[0] == "refill":
                tank = 95.0
                print("  tank refilled to 95 %")

    threading.Thread(target=console, daemon=True).start()
    last_check = 0.0
    while not stop.is_set():
        now = local_now(farm)
        stamp = now.isoformat(timespec="seconds")
        hour = now.hour + now.minute / 60
        temp = 33 + 7 * math.cos((hour - 14) / 24 * 2 * math.pi) + rng.gauss(0, 0.3)
        lines = []

        def row(field: str, module: str, device: str, typ: str, kind: str, value: str, unit: str) -> None:
            lines.append(f"{field},{module},{device},{typ},{kind},{value},{unit},{stamp}")

        for field, (s_mod, a_mod) in modules.items():
            r = ranges.get(field, {})
            heat = max(0.15, (temp - 30) / 9)
            soil[field] -= drying[field] * heat * args.speed * args.period / 3600
            # pump running?
            if field in pump_until:
                soil[field] += 5.0 / 120 * args.period * args.speed
                if time.time() >= pump_until[field]:
                    del pump_until[field]
                    row(field, a_mod, "relay1", "action", "pump", "deactivated", "")
                    print(f"  {field}: pump off (soil {soil[field]:.1f} %)")
            # the master's rules (plan 2.5)
            level_min = (ranges.get("F1", {}).get("level") or [15, 100])[0]
            if field not in pump_until and r.get("soil_moisture") and soil[field] < r["soil_moisture"][0]:
                if tank < level_min:
                    if f"block{field}" not in alerts:
                        print(f"  ALERT {field}: soil {soil[field]:.1f} % but tank {tank:.0f} % is below {level_min} %: pump blocked")
                        alerts.add(f"block{field}")
                else:
                    seconds = r.get("pump_seconds", 120)
                    pump_until[field] = time.time() + seconds / args.speed
                    tank -= 1.5 * seconds / 120
                    alerts.discard(f"block{field}")
                    row(field, a_mod, "relay1", "action", "pump", "activated", "")
                    print(f"  {field}: soil {soil[field]:.1f} % < {r['soil_moisture'][0]} % -> pump on for {seconds} s")
            if r.get("temp_air") and temp > r["temp_air"][1] and f"heat{field}" not in alerts:
                print(f"  ALERT {field}: air {temp:.1f} °C above {r['temp_air'][1]} °C (no fan on this farm)")
                alerts.add(f"heat{field}")
            values = {"temp_air": temp, "humidity": 72 - 38 * (temp - 27) / 13 + rng.gauss(0, 1),
                      "soil_moisture": soil[field] + rng.gauss(0, 0.15),
                      "light": max(0.0, 95000 * math.sin((hour - 5.5) / 12.2 * math.pi)) if 5.5 <= hour <= 17.7 else 0.0,
                      "level": tank}
            for kind in kinds[field]:
                v = values[kind]
                row(field, s_mod, KIND_DEVICE[kind], "reading", kind, f"{v:.0f}" if kind == "light" else f"{max(0.0, v):.1f}", KIND_UNIT[kind])

        code, body = http(f"{args.url}/log", "\n".join(lines).encode())
        if code != 200:
            print(f"  POST /log -> {code} {body[:120]!r} (rows would wait on the SD card)")

        if time.time() - last_check >= args.check:
            last_check = time.time()
            code, body = http(f"{args.url}/ranges/version")
            if code == 200 and json.loads(body)["version"] != ranges["version"]:
                code, body = http(f"{args.url}/ranges?have={ranges['version']}")
                if code == 200:
                    ranges = json.loads(body)
                    SD_CARD.parent.mkdir(exist_ok=True)
                    SD_CARD.write_text(json.dumps(ranges, indent=2))
                    alerts.clear()
                    print(f"  new ranges v{ranges['version']} saved to the SD card: "
                          + "; ".join(f"{f} soil {v['soil_moisture'][0]:g}-{v['soil_moisture'][1]:g} %, pump {v['pump_seconds']} s"
                                      for f, v in ranges.items() if isinstance(v, dict)))
        stop.wait(args.period)


if __name__ == "__main__":
    main()
