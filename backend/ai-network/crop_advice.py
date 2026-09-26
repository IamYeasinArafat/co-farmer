"""Crop suggestion (plan 3.3: the Crop Suggestion role) - code, no AI, so it works on day one.

For every candidate crop (knowledge.CANDIDATE_CROPS) and every field it answers the farmer's real questions:
can I plant it now and will the whole season suit it, when is it ready, how much could it bring in from this bed,
how much water does it need, and how hard is it to grow. Climate comes from Qatar's monthly normals (Qatar Open
Data, 2020-2024); greenhouses are assumed to run about 6 °C cooler at midday (fan-and-pad, an estimate).
Money is gross revenue (yield x price): seed, labour, water and energy are not included, and every price says
how reliable it is. The CrewAI Crop Suggestion / Market department explains these results; it doesn't invent them.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import market
from knowledge import CANDIDATE_CROPS

MONTHS = market.MONTHS
GREENHOUSE_COOLING_C = 6.0
PREP_DAYS = 7          # seeds and bed preparation: "plant now" means the season starting about a week from today
WATER_RANK = {"low": 0, "medium": 1, "high": 2}
CARE_RANK = {"easy": 0, "medium": 1, "hard": 2}


def _covered(field: dict[str, Any]) -> bool:
    return any(w in (field.get("type") or "").lower() for w in ("greenhouse", "covered", "tunnel", "net house", "indoor"))


def _month_fit(crop: dict[str, Any], month: dict[str, float], covered: bool) -> float:
    """1 = ideal all month, 0.5 = tolerated, 0 = too hot or too cold."""
    tmax = month["tmax"] - (GREENHOUSE_COOLING_C if covered else 0)
    tmin = month["tmin"]
    lo, hi, acc_lo, acc_hi = crop["air"]
    if tmax <= hi + 2 and tmin >= lo - 2:
        return 1.0
    if tmax <= acc_hi and tmin >= acc_lo:
        return 0.5
    return 0.0


def season_fit(crop: dict[str, Any], start: date, normals: dict[str, dict[str, float]], covered: bool) -> tuple[float, str | None]:
    """Worst month over the crop's whole season if planted on `start`, and that month's name when it's the problem."""
    months = max(1, math.ceil(crop["cycle_days"] / 30))
    worst, where = 1.0, None
    for k in range(months):
        m = MONTHS[(start.month - 1 + k) % 12]
        fit = _month_fit(crop, normals[m], covered)
        if fit < worst:
            worst, where = fit, m
    return worst, where


def next_window(crop: dict[str, Any], today: date, normals: dict[str, dict[str, float]], covered: bool) -> date | None:
    """First month start (within a year) from which the whole season fits."""
    for k in range(1, 13):
        first = (today.replace(day=1) + timedelta(days=32 * k)).replace(day=1)
        if season_fit(crop, first, normals, covered)[0] >= 0.5:
            return first
    return None


def water_limited(profile: dict[str, Any]) -> bool:
    text = " ".join([profile.get("water") or "", *(profile.get("problems") or [])]).lower()
    return any(w in text for w in ("dry", "drought", "shortage", "limited", "tank", "expensive", "saline", "salty", "brackish",
                                   "جاف", "جفاف", "نقص", "خزان", "مالح", "ملوحة"))


def field_area(field: dict[str, Any]) -> float:
    size = field.get("size_m") or [0, 0]
    try:
        return float(size[0]) * float(size[1])
    except (TypeError, ValueError, IndexError):
        return 0.0


def suggest(farm: dict[str, Any], today: date | None = None, top: int = 3) -> dict[str, Any]:
    """{'fields': {F1: {'now': [...], 'later': [...], 'area_m2', 'covered'}}, 'notes': [...]} ranked best first."""
    today = today or date.today()
    profile = farm["profile"]
    normals = market.refresh()["climate_normals"]["months"]
    prices = market.prices()
    from agents.strategy import price_per_kg   # shared price rules (farm-gate, retail share, proxies)

    limited = water_limited(profile)
    out: dict[str, Any] = {"today": today.isoformat(), "fields": {}, "water_limited": limited,
                           "notes": ["Money is gross revenue (harvest × price); seed, labour and water costs are not included.",
                                     "Climate: Qatar monthly averages 2020-2024" + ("; greenhouses assumed ~6 °C cooler at midday" if
                                                                                   any(_covered(f) for f in profile["fields"]) else "")]}
    for field in profile["fields"]:
        covered = _covered(field)
        area = field_area(field)
        rows = []
        for key, crop in CANDIDATE_CROPS.items():
            price, basis, confidence = price_per_kg(key, prices)
            yield_t_ha = crop["yield_t_ha"] if covered else crop.get("yield_open_t_ha", crop["yield_t_ha"])
            kg_m2 = yield_t_ha / 10
            per_m2 = kg_m2 * (price or 0)
            fit_now, bad_month = season_fit(crop, today + timedelta(days=PREP_DAYS), normals, covered)
            later = None if fit_now >= 0.5 else next_window(crop, today, normals, covered)
            water_pen = WATER_RANK[crop["water"]] * (2 if limited else 1)
            score = (per_m2 / max(crop["cycle_days"], 1) * 30) * 4 + fit_now * 10 - water_pen * 2 - CARE_RANK[crop["care"]] * 1.5
            reasons, reasons_ar = [], []   # (English, Arabic) side by side for the farmer board

            def why(en: str, ar: str) -> None:
                reasons.append(en)
                reasons_ar.append(ar)

            if fit_now == 1.0:
                why("the weather suits it for the whole season" if not covered else "fits this greenhouse all season",
                    "الطقس مناسب له طوال الموسم")
            elif fit_now == 0.5:
                why(f"it tolerates the weather, though {bad_month} is at its limit", "يتحمل الطقس، لكن بعض الأيام على الحد")
            if crop["air"][3] >= 40:
                why("handles Qatar's summer heat", "يتحمل حرارة الصيف في قطر")
            if crop["water"] == "low":
                why("needs little water", "يحتاج ماءً قليلاً")
            elif crop["water"] == "high" and limited:
                why("needs a lot of water, which is tight on this farm", "يحتاج ماءً كثيراً، والماء محدود في مزرعتك")
            if crop["care"] == "easy":
                why("easy to grow", "سهل الزراعة")
            if confidence == "official":
                why("backed by an official Qatar price", "مدعوم بسعر رسمي في قطر")
            rows.append({
                "crop": key, "name": crop["name"], "ar": crop.get("ar", crop["name"]),
                "fit_now": fit_now, "plant_from": later.isoformat() if later else None,
                "plant_from_month": MONTHS[later.month - 1] if later else None,
                "ready_on": (today + timedelta(days=crop["cycle_days"])).isoformat() if fit_now >= 0.5 else None,
                "days": crop["cycle_days"], "water": crop["water"], "care": crop["care"],
                "kg_m2": round(kg_m2, 2), "harvest_kg": round(kg_m2 * area), "price_qr_kg": price, "price_basis": basis,
                "confidence": confidence, "qr_per_harvest": round(per_m2 * area), "qr_m2": round(per_m2, 1),
                "yield_source": crop["yield_source"], "score": round(score, 2), "reasons": reasons, "reasons_ar": reasons_ar,
            })
        now = sorted([r for r in rows if r["fit_now"] >= 0.5], key=lambda r: -r["score"])
        later = sorted([r for r in rows if r["fit_now"] < 0.5 and r["plant_from"]], key=lambda r: (r["plant_from"], -r["score"]))
        # "later": the best crop for each coming start month, so the farmer sees a calendar, not a list
        by_month: dict[str, dict[str, Any]] = {}
        for r in sorted(later, key=lambda r: -r["score"]):
            by_month.setdefault(r["plant_from_month"], r)
        out["fields"][field["field_id"]] = {
            "covered": covered, "area_m2": round(area), "current_crop": field.get("crop") or None,
            "now": now[:top], "later": sorted(by_month.values(), key=lambda r: r["plant_from"])[:top],
            "all": sorted(rows, key=lambda r: -r["score"]),
        }
    return out


def summary_text(advice: dict[str, Any]) -> str:
    """A few lines for the CrewAI department and the Director (numbers from code)."""
    lines = []
    for field_id, f in advice["fields"].items():
        now = "; ".join(f"{r['name']} (ready in {r['days']} days, ≈{r['qr_per_harvest']:,} QR per harvest from "
                        f"{f['area_m2']:,} m² at {r['price_qr_kg']} QR/kg, {r['confidence']} price, {r['water']} water)"
                        for r in f["now"]) or "nothing fits the coming weeks"
        later = "; ".join(f"{r['name']} from {r['plant_from_month']}" for r in f["later"])
        lines.append(f"{field_id} ({'greenhouse' if f['covered'] else 'open air'}): plant now: {now}."
                     + (f" Later: {later}." if later else ""))
    if advice["water_limited"]:
        lines.append("Water is limited on this farm, so thirsty crops were ranked lower.")
    return "\n".join(lines)
