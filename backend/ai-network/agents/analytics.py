"""Data & Analytics department skills: trends, anomalies and short-range forecasts."""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Any

from knowledge import DIURNAL, LABELS, fmt

from .base import finding, done, profile_for

TREND_SHARE = 0.25    # change across the window, as a share of the optimal band...
EDGE_SHARE = 0.15     # ...that pushes the reading into the outer 15% of the band
ANOMALY_Z = 3.0       # current value vs. window baseline, in standard deviations...
ANOMALY_SHARE = 0.1   # ...and at least 10% of the band (ignores sensor noise)
HORIZON_MIN = 6 * 60      # forecast 6 hours ahead
SLOPE_MIN = 3 * 60        # fit the drift over the last 3 hours
DRIFT_CAP_MIN = 2 * 60    # extrapolate the drift at most 2 h (noise would otherwise explode)


def _steps(minutes: float, state: dict[str, Any]) -> int:
    return max(1, round(minutes / state.get("interval_minutes", 15)))


def _fields(agent: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, tuple[float, float]]]:
    ranges = profile_for(state)["ranges"]
    wanted = agent["inputs"]
    return [(f, ranges[f]) for f in wanted if f in ranges and f not in DIURNAL]


def _series(rows: list[dict[str, Any]], field: str) -> list[float]:
    return [float(r[field]) for r in rows if isinstance(r.get(field), (int, float))]


def _hours(steps: int, state: dict[str, Any]) -> float:
    return round(steps * state.get("interval_minutes", 15) / 60, 1)


def trend(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    history = state.get("history", [])
    issues = []
    for field, (low, high) in _fields(agent, state):
        series = _series(history, field)
        if len(series) < 4:
            continue
        delta, current, band = series[-1] - series[0], series[-1], high - low
        near_edge = current > high - EDGE_SHARE * band if delta > 0 else current < low + EDGE_SHARE * band
        if abs(delta) > TREND_SHARE * band and near_edge:
            issues.append({"kind": "trend", "field": field, "direction": "high" if delta > 0 else "low", "severity": "WARNING",
                           "message": f"{LABELS[field]} {'rose' if delta > 0 else 'fell'} {fmt(field, round(abs(delta), 2))} over the last {_hours(len(series) - 1, state)} h"})
    result = finding(agent, issues, f"No worrying trends across {len(history)} recent readings.")
    return done(result)


def anomaly(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    history = state.get("history", [])
    issues = []
    for field, (low, high) in _fields(agent, state):
        series = _series(history, field)
        if len(series) < 5:
            continue
        baseline, spread, current = mean(series[:-1]), pstdev(series[:-1]), series[-1]
        if spread > 0 and abs(current - baseline) / spread > ANOMALY_Z and abs(current - baseline) > ANOMALY_SHARE * (high - low):
            z = (current - baseline) / spread
            issues.append({"kind": "anomaly", "field": field, "direction": "high" if z > 0 else "low", "severity": "WARNING",
                           "message": f"{LABELS[field]} anomaly: {fmt(field, current)} vs. {fmt(field, round(baseline, 2))} baseline ({z:+.1f}σ)"})
    result = finding(agent, issues, "No sudden jumps against the recent baseline.")
    return done(result)


def forecast(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Seasonal-naive forecast: yesterday's curve + today's offset, extrapolating the offset's drift.

    Using yesterday's shape stops the normal morning warm-up from looking like a crisis.
    """
    archive = state.get("archive", [])
    issues = []
    day, horizon = _steps(24 * 60, state), _steps(HORIZON_MIN, state)
    slope_n, cap = max(3, _steps(SLOPE_MIN, state)), _steps(DRIFT_CAP_MIN, state)
    if len(archive) > day + slope_n:
        now = len(archive) - 1
        for field, (low, high) in _fields(agent, state):
            values = [r.get(field) for r in archive]
            if not all(isinstance(v, (int, float)) for v in values[now - day - slope_n:]):
                continue
            offsets = [values[i] - values[i - day] for i in range(now - slope_n + 1, now + 1)]
            xs = range(slope_n)
            x_bar, y_bar = mean(xs), mean(offsets)
            drift = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, offsets)) / sum((x - x_bar) ** 2 for x in xs)
            current = values[now]
            if not low <= current <= high:
                continue  # already out of range: the monitors own this
            for k in range(1, horizon + 1):
                yesterday = values[now - day + k] if now - day + k < now else values[now]
                projected = yesterday + offsets[-1] + drift * min(k, cap)
                if projected > high or projected < low:
                    edge = high if projected > high else low
                    issues.append({"kind": "forecast", "field": field, "direction": "high" if projected > high else "low",
                                   "severity": "WARNING",
                                   "message": f"{LABELS[field]} projected to cross {fmt(field, edge)} in ~{_hours(k, state)} h (now {fmt(field, current)})"})
                    break
    result = finding(agent, issues, "No threshold breaches projected in the next 6 h.")
    return done(result)
