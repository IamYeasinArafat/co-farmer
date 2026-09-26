"""Summariser (code, no AI; Hydro Monitor plan 3.4 step 2).

Reads the window from the database and computes, per field: now / min / max / average / trend of each
reading, pump runs, how fast the soil dries in the heat, yesterday's peak, the Open-Meteo forecast and the
farmer's latest note. A day of 10-second readings is thousands of rows per sensor; each department gets
a few lines. It also resamples the rows into 5-minute steps for the code tools.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

import db
import weather
from farm import FARMS, field_kinds, local_now, tz_of
from knowledge import KIND_FIELD

STEP_MIN = 5            # resampling step for the code tools
WINDOW_H = 24           # what the departments summarise
ARCHIVE_H = 48          # the forecast tool compares today with yesterday
TREND_H = 3             # trend_per_h is fitted over the last 3 hours


def _slope_per_h(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of (epoch seconds, value), per hour."""
    if len(points) < 3:
        return 0.0
    xs, ys = [p[0] / 3600 for p in points], [p[1] for p in points]
    xm, ym = mean(xs), mean(ys)
    den = sum((x - xm) ** 2 for x in xs)
    return sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / den if den else 0.0


def _buckets(rows: list[dict[str, Any]], tz) -> list[dict[str, Any]]:
    """Readings -> one wide row per 5-minute step, keyed with the code tools' field names."""
    acc: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["type"] != "reading" or r["kind"] not in KIND_FIELD:
            continue
        try:
            value = float(r["value"])
        except ValueError:
            continue
        acc[int(r["ts"] // (STEP_MIN * 60))][KIND_FIELD[r["kind"]]].append(value)
    out = []
    for step in sorted(acc):
        row = {"timestamp": datetime.fromtimestamp(step * STEP_MIN * 60, tz).strftime("%Y-%m-%d %H:%M:%S")}
        row.update({f: round(mean(v), 2) for f, v in acc[step].items()})
        out.append(row)
    return out


def _pump_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair 'activated' / 'deactivated' action rows into runs with a duration."""
    runs, open_at = [], None
    for r in rows:
        if r["type"] != "action" or r["kind"] != "pump":
            continue
        if r["value"] == "activated":
            open_at = r["ts"]
        elif r["value"] == "deactivated" and open_at is not None:
            runs.append({"start": open_at, "seconds": round(r["ts"] - open_at)})
            open_at = None
    return runs


def _drying_per_h(points: list[tuple[float, float]]) -> float | None:
    """Average rate the soil loses moisture between waterings (%/h, negative). Pump jumps are ignored.
    Works on 15-minute averages: comparing raw 10-second readings would count sensor noise as drying."""
    acc: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for t, v in points:
        acc[int(t // 900)].append((t, v))
    smooth = [(mean(t for t, _ in g), mean(v for _, v in g)) for _, g in sorted(acc.items())]
    drops = []
    for (t0, v0), (t1, v1) in zip(smooth, smooth[1:]):
        if t1 > t0 and v1 <= v0:
            drops.append((v1 - v0) / ((t1 - t0) / 3600))
    return round(mean(drops), 2) if len(drops) >= 3 else None


def summarise_field(farm: dict[str, Any], field_id: str, rows: list[dict[str, Any]], now: datetime,
                    forecast: dict[str, Any] | None, note: str | None) -> dict[str, Any]:
    tz = tz_of(farm)
    since = (now - timedelta(hours=WINDOW_H)).timestamp()
    window = [r for r in rows if r["ts"] >= since]
    summary: dict[str, Any] = {"field": field_id, "window": f"last {WINDOW_H} h"}
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        if r["type"] == "reading":
            try:
                series[r["kind"]].append((r["ts"], float(r["value"])))
            except ValueError:
                pass
    last_ts = max((r["ts"] for r in window), default=None)
    summary["last_reading"] = datetime.fromtimestamp(last_ts, tz).strftime("%H:%M") if last_ts else None

    for kind in field_kinds(farm["hardware"])[field_id]:
        pts = [p for p in series.get(kind, []) if p[0] >= since]
        if not pts:
            continue
        values = [v for _, v in pts]
        recent = [p for p in pts if p[0] >= pts[-1][0] - TREND_H * 3600]
        stat = {"now": round(values[-1], 1), "min": round(min(values), 1), "max": round(max(values), 1),
                "avg": round(mean(values), 1)}
        if kind != "light":
            stat["trend_per_h"] = round(_slope_per_h(recent), 2)
        summary[kind] = stat

    # yesterday and today, in local calendar days
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    y0, y10, y18 = today0 - timedelta(days=1), today0 - timedelta(hours=14), today0 - timedelta(hours=6)
    runs = _pump_runs(rows)
    y_runs = [r for r in runs if y0.timestamp() <= r["start"] < today0.timestamp()]
    yesterday: dict[str, Any] = {"pump_runs": len(y_runs), "pump_seconds": sum(r["seconds"] for r in y_runs)}
    temps = [v for t, v in series.get("temp_air", []) if y0.timestamp() <= t < today0.timestamp()]
    if temps:
        yesterday["temp_air_max"] = round(max(temps), 1)
    soil_y = [p for p in series.get("soil_moisture", []) if y10.timestamp() <= p[0] < y18.timestamp()]
    yesterday["soil_drying_after_10am_per_h"] = _drying_per_h(soil_y)
    summary["yesterday"] = yesterday
    summary["today"] = {"pump_runs": len([r for r in runs if r["start"] >= today0.timestamp()])}
    summary["drying_per_h_48h"] = _drying_per_h(series.get("soil_moisture", []))

    day = weather.day_summary(forecast, now.date())
    if day:
        summary["forecast_today"] = day
    tomorrow = weather.day_summary(forecast, (now + timedelta(days=1)).date())
    if tomorrow:
        summary["forecast_tomorrow"] = {k: tomorrow[k] for k in ("temp_max", "hottest_hour") if k in tomorrow}
    if forecast and forecast.get("offline"):
        summary["forecast_note"] = f"offline: forecast cached at {forecast.get('fetched_at')}"
    if note:
        summary["farmer_note"] = note
    return summary


def summarise(con, farm: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """{'fields': {F1: summary}, 'history': {F1: [wide rows 24 h]}, 'archive': {F1: [wide rows 48 h]}, 'forecast': ...}"""
    now = now or local_now(farm)
    tz = tz_of(farm)
    profile = farm["profile"]
    forecast = weather.forecast(profile["latitude"], profile["longitude"], FARMS / farm["id"] / "forecast_cache.json")
    out: dict[str, Any] = {"now": now.isoformat(timespec="minutes"), "fields": {}, "history": {}, "archive": {},
                           "forecast_source": None if not forecast else ("cache" if forecast.get("offline") else "Open-Meteo")}
    for field_id in field_kinds(farm["hardware"]):
        rows = db.read_log(con, (now - timedelta(hours=ARCHIVE_H)).timestamp(), field_id, farm["id"])
        recent_notes = [n for n in db.notes(con, field_id, limit=3, farm_id=farm["id"])
                        if datetime.fromisoformat(n["created_at"]) >= now - timedelta(days=3)]
        note = recent_notes[0]["text"] if recent_notes else None
        out["fields"][field_id] = summarise_field(farm, field_id, rows, now, forecast, note)
        wide = _buckets(rows, tz)
        cut = (now - timedelta(hours=WINDOW_H)).strftime("%Y-%m-%d %H:%M:%S")
        out["archive"][field_id] = wide
        out["history"][field_id] = [r for r in wide if r["timestamp"] >= cut]
    rates = {f: s.get("drying_per_h_48h") for f, s in out["fields"].items() if s.get("drying_per_h_48h")}
    out["compare"] = compare_fields(rates)
    return out


def compare_fields(rates: dict[str, float]) -> str | None:
    """'F1 has dried 40 % faster than F2 over the last 48 h' when beds differ by more than 25 %."""
    if len(rates) < 2:
        return None
    (fast, r_fast), (slow, r_slow) = sorted(rates.items(), key=lambda kv: kv[1])[0], sorted(rates.items(), key=lambda kv: kv[1])[-1]
    if r_slow >= 0 or abs(r_fast) < abs(r_slow) * 1.25:
        return None
    pct = round(100 * (abs(r_fast) / abs(r_slow) - 1))
    return f"{fast} has dried {pct} % faster than {slow} over the last 48 h ({abs(r_fast)} vs {abs(r_slow)} %/h between waterings)"
