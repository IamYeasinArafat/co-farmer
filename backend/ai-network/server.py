"""Farm computer endpoint (Hydro Monitor plan 2.3, 2.5, 5.2): FastAPI + SQLite.

    POST /log              rows from the master: CSV lines in the plan's log format (or JSON); stored in SQLite
    GET  /ranges/version   {"version": 13}: the master checks this every few minutes (tiny)
    GET  /ranges           the latest ranges file; ?have=13 answers 304 Not Modified if that is still the latest
    POST /run              "Run now": starts an agent run in the background
    GET  /status           latest version, last runs, rows received

It also runs the agent network on a schedule: every 6 hours (00:00, 06:00, 12:00, 18:00 farm time) by default.

    python server.py                       # http://0.0.0.0:8000, reachable by the ESP32 on the same WiFi
    python server.py --every 6 --port 8000
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import db
import network
from farm import load_farm, local_now

log = logging.getLogger("server")
app = FastAPI(title="Hydro Monitor farm computer")
con = db.connect()
_run_lock = threading.Lock()
SETTINGS: dict[str, Any] = {"every_hours": 6, "route": None}   # None: the default route


def _start_run(trigger: str) -> bool:
    """Start an agent run in a background thread; False if one is already running."""
    if not _run_lock.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            result = network.run(db.connect(), trigger=trigger, route=SETTINGS["route"])
            log.info("agent run (%s): %s %s", trigger, result["status"],
                     result.get("plan", {}).get("version") if result["status"] == "ok" else result.get("error"))
        finally:
            _run_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def _scheduler() -> None:
    """Run at every multiple of `every_hours` from midnight, farm time (plan 3.4: every 6 hours)."""
    farm = load_farm()
    while True:
        now = local_now(farm)
        step = timedelta(hours=SETTINGS["every_hours"])
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        nxt = midnight + step * (int((now - midnight) / step) + 1)
        time.sleep(max(1.0, (nxt - now).total_seconds()))
        if not _start_run("schedule"):
            log.warning("scheduled run skipped: a run is still going")


@app.post("/log")
async def post_log(request: Request) -> dict[str, Any]:
    body = (await request.body()).decode("utf-8", errors="replace")
    try:
        payload: Any = await request.json() if "json" in request.headers.get("content-type", "") else body
        rows = db.parse_rows(payload)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err
    return {"stored": db.insert_rows(con, rows, load_farm()["id"])}


@app.get("/ranges/version")
def ranges_version() -> dict[str, Any]:
    plan = db.latest_plan(con, load_farm()["id"])
    return {"version": plan["version"] if plan else 0}


@app.get("/ranges")
def get_ranges(have: int | None = None) -> Response:
    plan = db.latest_plan(con, load_farm()["id"])
    if not plan:
        raise HTTPException(404, "no plan yet: run the onboarding or seed_demo.py")
    if have is not None and have == plan["version"]:
        return Response(status_code=304)
    return JSONResponse(plan["ranges"])


@app.post("/run")
def post_run() -> dict[str, Any]:
    if not _start_run("button"):
        raise HTTPException(409, "an agent run is already going")
    return {"started": True}


@app.get("/status")
def status() -> dict[str, Any]:
    farm = load_farm()
    plan = db.latest_plan(con, farm["id"])
    count = con.execute("SELECT COUNT(*) FROM log WHERE farm_id = ?", (farm["id"],)).fetchone()[0]
    return {"farm": farm["id"], "version": plan["version"] if plan else 0, "rows": count,
            "running": _run_lock.locked(), "runs": db.runs(con, 5), "every_hours": SETTINGS["every_hours"],
            "time": local_now(farm).isoformat(timespec="seconds")}


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--every", type=float, default=6, help="hours between scheduled agent runs (0 = off)")
    parser.add_argument("--route", default=None, help="model route (default: the first in farms/model_routes.json)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    SETTINGS.update(every_hours=args.every, route=args.route)
    if args.every > 0:
        threading.Thread(target=_scheduler, daemon=True).start()
        log.info("agent runs every %g h, and on POST /run", args.every)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
