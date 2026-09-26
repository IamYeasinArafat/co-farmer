"""Farm design and finance (code, no AI): the layout, the best crop combination, and whether it pays.

Works for both starting points:
  * bare land ("just the land"): lays the land out into zones (open field, shade-net house, cooled greenhouse) with
    a shed, water tank, fence, solar power if there's no mains power, and a well if there's no water; then plants
    the best crop combination in every zone and season, prices everything and checks it against the budget.
  * a working farm: prices the current crop on the existing beds, then shows what the best crop combination and an
    upgrade (e.g. a shade-net house for summer) would change.

The crop combination per zone and season (cool Oct-Apr, hot May-Sep) keeps only crops with a positive margin
(sales minus seed, water and fertilizer), and never puts more than half a zone into one crop, so one bad price
can't sink the farm. Where nothing is profitable in a season (open field in summer), the zone rests and the soil
is solarised (a common Qatar practice against soil pests).

Money: sales are gross (yield x price, Qatar Open Data where published); costs come from farms/costs.json
(planning estimates, not quotes). The Finance department (CrewAI) judges these numbers; it doesn't invent them.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
import weather
from gis import wind_dust, salinity, site_suitability

import market
from knowledge import CANDIDATE_CROPS

ROOT = Path(__file__).parent
COSTS = json.loads((ROOT / "farms" / "costs.json").read_text(encoding="utf-8"))
MONTHS = market.MONTHS
SEASONS = {"cool": ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr"], "hot": ["May", "Jun", "Jul", "Aug", "Sep"]}
SEASON_LABEL = {"cool": ("Cool season (Oct–Apr)", "الموسم البارد (أكتوبر–أبريل)"),
                "hot": ("Hot season (May–Sep)", "الموسم الحار (مايو–سبتمبر)")}
COVER_LABEL = {"open": ("Open field", "حقل مكشوف"), "shade": ("Shade-net house", "بيت شبك تظليل"),
               "greenhouse": ("Cooled greenhouse", "بيت محمي مبرّد")}
TIERS = {   # growing-area split per design option
    "hydroponic": {"label": ("Hydroponic / vertical", "زراعة مائية / رأسية"), "zones": [("hydroponic", 1.0)]},
    "starter": {"label": ("Starter: open field", "بداية: حقل مكشوف"), "zones": [("open", 1.0)]},
    "balanced": {"label": ("Balanced: open field + shade house", "متوازن: حقل مكشوف + بيت تظليل"),
                 "zones": [("open", 0.75), ("shade", 0.25)]},
    "intensive": {"label": ("Intensive: + cooled greenhouse", "مكثف: + بيت محمي مبرّد"),
                  "zones": [("open", 0.6), ("shade", 0.2), ("greenhouse", 0.2)]},

}
BED_M2 = 2000              # one Hydro Monitor sensor set + actuator set per bed of up to this size
FAMILY_LABOUR_M2 = 1000    # below this the family does the work: no paid workers
WATER_RANK = {"low": 0, "medium": 1, "high": 2}


def cost(section: str, key: str) -> float:
    return float(COSTS[section][key]["qr"])


def _month_fit(crop: dict[str, Any], month: dict[str, float], cooling_c: float) -> float:
    tmax, tmin = month["tmax"] - cooling_c, month["tmin"]
    lo, hi, acc_lo, acc_hi = crop["air"]
    if tmax <= hi + 2 and tmin >= lo - 2:
        return 1.0
    if tmax <= acc_hi and tmin >= acc_lo:
        return 0.5
    return 0.0


def _yield_t_ha(crop: dict[str, Any], cover: str) -> float:
    open_y = crop.get("yield_open_t_ha", crop["yield_t_ha"])
    return {"open": open_y, "shade": open_y * 1.1, "greenhouse": crop["yield_t_ha"]}[cover]


def crop_season(key: str, season: str, cover: str, normals: dict[str, Any], prices: dict[str, Any]) -> dict[str, Any]:
    """One crop, one season, one cover type, per m²: cycles, kg, sales, variable cost, margin."""
    from agents.strategy import price_per_kg

    crop = CANDIDATE_CROPS[key]
    cooling = COSTS["cover_cooling_c"][cover]
    fits = [_month_fit(crop, normals[m], cooling) for m in SEASONS[season]]
    months = sum(fits)                                   # tolerated months count half
    cycles = months * 30 / crop["cycle_days"]
    price, basis, confidence = price_per_kg(key, prices)
    kg = cycles * _yield_t_ha(crop, cover) / 10
    sales = kg * (price or 0)
    days = months * 30
    water_m3 = COSTS["water_l_m2_day"][crop["water"]] * COSTS["cover_water_factor"][cover] * days / 1000
    variable = (cycles * cost("opex", "seed_m2_cycle") + water_m3 * cost("opex", "water_m3")
                + cost("opex", "fertilizer_m2_year") * len(SEASONS[season]) / 12)
    return {"crop": key, "name": crop["name"], "ar": crop.get("ar", crop["name"]), "season": season, "cover": cover,
            "months": months, "cycles": round(cycles, 1), "kg_m2": round(kg, 2), "sales_m2": round(sales, 2),
            "variable_m2": round(variable, 2), "margin_m2": round(sales - variable, 2), "water_m3_m2": round(water_m3, 3),
            "price": price, "confidence": confidence, "water": crop["water"], "care": crop["care"]}


def best_combination(cover: str, area: float, goal: str | None, water_limited: bool, normals: dict[str, Any],
                     prices: dict[str, Any], only: str | None = None) -> list[dict[str, Any]]:
    """The crops for one zone, both seasons. Only profitable crops; at most half the zone in one crop."""
    goal = (goal or "").lower()
    rows: list[dict[str, Any]] = []
    for season in SEASONS:
        options = []
        for key in ([only] if only else CANDIDATE_CROPS):
            r = crop_season(key, season, cover, normals, prices)
            if r["months"] < 1 or r["margin_m2"] <= 0:
                continue                                  # doesn't grow here this season, or loses money
            score = r["margin_m2"]
            if water_limited or "water" in goal or "ماء" in goal:
                score -= WATER_RANK[r["water"]] * 0.15 * abs(r["margin_m2"])
            if r["confidence"] == "official":
                score *= 1.1                              # a real price is worth more than a guess
            options.append((score, r))
        options.sort(key=lambda o: -o[0])
        family = any(w in goal for w in ("family", "food", "عائل", "طعام"))
        picks = [r for _, r in options[:4 if family else 3]]
        shares = {1: [1.0], 2: [0.6, 0.4], 3: [0.5, 0.3, 0.2], 4: [0.4, 0.25, 0.2, 0.15]}.get(len(picks), [])
        if only and picks:
            shares = [1.0]
        if not picks:
            rows.append({"season": season, "cover": cover, "crop": None, "name": "Rest and solarise the soil",
                         "ar": "راحة وتشميس التربة", "share": 1.0, "area_m2": round(area), "kg": 0, "sales": 0.0,
                         "variable": 0.0, "margin": 0.0, "water_m3": 0.0, "note": "nothing pays in this season here"})
            continue
        for r, share in zip(picks, shares):
            a = area * share
            rows.append({**{k: r[k] for k in ("crop", "name", "ar", "cycles", "price", "confidence", "water", "care")},
                         "season": season, "cover": cover, "share": share, "area_m2": round(a),
                         "kg": round(r["kg_m2"] * a), "sales": round(r["sales_m2"] * a),
                         "variable": round(r["variable_m2"] * a), "margin": round(r["margin_m2"] * a),
                         "water_m3": round(r["water_m3_m2"] * a, 1)})
    return rows


def _is_true(value: Any, words: tuple[str, ...]) -> bool:
    return any(w in str(value or "").lower() for w in words)


def build(profile: dict[str, Any], tier: str, land_m2: float, develop_share: float = 1.0,
          existing: bool = False, fixed_crop: str | None = None) -> dict[str, Any]:
    """One design option, fully priced."""
    normals = market.refresh()["climate_normals"]["months"]
    prices = market.prices()
    from crop_advice import water_limited

    limited = water_limited(profile)
    growing = land_m2 * (1.0 if existing else COSTS["usable_share"]) * develop_share
    zones, capex = [], []

    def item(name: str, key: str, qty: float, unit: str) -> None:
        if qty > 0:
            capex.append({"item": name, "qty": round(qty, 1), "unit": unit, "unit_qr": cost("capex", key),
                          "total": round(qty * cost("capex", key)), "what": COSTS["capex"][key]["what"]})

    for i, (cover, share) in enumerate(TIERS[tier]["zones"]):
        area = growing * share
        crops = best_combination(cover, area, profile.get("goal"), limited, normals, prices,
                                 only=fixed_crop if (fixed_crop and cover == "open") else None)
        beds = max(1, math.ceil(area / BED_M2))
        zones.append({"zone": f"Z{i + 1}", "cover": cover, "area_m2": round(area), "beds": beds, "crops": crops})
        if cover == "greenhouse":
            item("Cooled greenhouse", "greenhouse_cooled_m2", area, "m²")
        elif cover == "shade":
            item("Shade-net house", "shade_house_m2", area, "m²")
        if not existing or cover != "open":
            item(f"Drip irrigation ({COVER_LABEL[cover][0].lower()})", "drip_m2", area, "m²")
            item(f"Bed preparation ({COVER_LABEL[cover][0].lower()})", "soil_prep_m2", area, "m²")
        item(f"Drip pumps ({COVER_LABEL[cover][0].lower()})", "pump", beds, "pumps")

    peak_l_day = sum(COSTS["water_l_m2_day"][r["water"]] * COSTS["cover_water_factor"][z["cover"]] * r["area_m2"]
                     for z in zones for r in z["crops"] if r["crop"])
    tank_m3 = max(5, math.ceil(peak_l_day / 1000 / 2 * 3))   # ~3 days of the busier season's water
    water = str(profile.get("water") or "")
    if not _is_true(water, ("tank", "خزان")) or not existing:
        item("Water tank", "tank_m3", tank_m3, "m³")
    if not existing:
        if not water or _is_true(water, ("no water", "none", "لا يوجد")):
            item("Well", "well", 1, "well")
        item("Shed / pump house", "shed", 1, "shed")
        item("Fence and windbreak", "fence_m", 4 * math.sqrt(land_m2 * develop_share), "m")
    gh_area = sum(z["area_m2"] for z in zones if z["cover"] == "greenhouse")
    solar_kw = 0.0
    if not profile.get("power") or _is_true(profile.get("power"), ("solar", "شمس", "none", "no ")):
        solar_kw = math.ceil(1 + len(zones) * 0.75 + gh_area * 0.02)
        item("Solar power with battery", "solar_kw", solar_kw, "kW")
    beds_total = sum(z["beds"] for z in zones)
    item("Hydro Monitor sensor sets", "sensor_set", beds_total, "sets")
    item("Hydro Monitor actuator sets", "actuator_set", beds_total, "sets")
    item("Hydro Monitor master node", "master", 1, "node")
    capex_total = sum(c["total"] for c in capex)

    rows = [r for z in zones for r in z["crops"]]
    sales = sum(r["sales"] for r in rows)
    variable = sum(r["variable"] for r in rows)
    workers = 0 if growing < FAMILY_LABOUR_M2 else math.ceil(growing / cost("opex", "m2_per_worker"))
    labour = workers * cost("opex", "worker_month") * 12
    kwh = gh_area * cost("opex", "cooling_kwh_m2_year") + len(zones) * 0.5 * 6 * 365
    electricity = 0.0 if solar_kw else kwh * cost("opex", "electricity_kwh")
    packing = sales * cost("opex", "packing_share")
    maintenance = capex_total * cost("opex", "maintenance_share")
    opex = {"Seeds, water and fertilizer": round(variable), "Workers": round(labour), "Electricity": round(electricity),
            "Packing and transport": round(packing), "Maintenance": round(maintenance)}
    opex_total = sum(opex.values())
    profit = sales - opex_total
    return {
        "tier": tier, "label": TIERS[tier]["label"], "land_m2": round(land_m2), "growing_m2": round(growing),
        "develop_share": develop_share, "zones": zones, "capex": capex, "capex_total": round(capex_total),
        "sales": round(sales), "opex": opex, "opex_total": round(opex_total), "profit": round(profit),
        "margin_pct": round(100 * profit / sales) if sales else 0, "workers": workers,
        "payback_years": round(capex_total / profit, 1) if profit > 0 else None,
        "water_m3_year": round(sum(r["water_m3"] for r in rows)), "tank_m3": tank_m3, "solar_kw": solar_kw,
    }


def land_area(profile: dict[str, Any]) -> float:
    if profile.get("land_m2"):
        return float(profile["land_m2"])
    total = 0.0
    for f in profile.get("fields") or []:
        size = f.get("size_m") or []
        if isinstance(size, (list, tuple)) and len(size) == 2:
            total += float(size[0]) * float(size[1])
    return total


def plan(profile: dict[str, Any]) -> dict[str, Any]:
    """Every design option priced, the recommended one (within the budget), and plain warnings."""
    existing = profile.get("stage") != "land"
    land = land_area(profile) or 1000.0
    budget = profile.get("budget_qr")
    current_crop = next((f.get("crop") for f in profile.get("fields") or [] if f.get("crop") not in (None, "", "undecided")), None)
    from farmer_view import crop_key

    current_key = crop_key(current_crop) if current_crop else None
    options = {t: build(profile, t, land, existing=existing) for t in TIERS}
    warnings: list[str] = []
    phase = None
    affordable = {t: o for t, o in options.items() if budget is None or o["capex_total"] <= budget}
    profitable = {t: o for t, o in affordable.items() if o["profit"] > 0}
    pool = profitable or affordable
    if pool:
        # best yearly profit, but an option that takes more than 5 years to pay back loses to a quicker one
        best = max(pool.values(), key=lambda o: o["profit"] - (1e9 if (o["payback_years"] or 99) > 5 and len(pool) > 1 else 0))
    else:
        # nothing fits the budget: develop part of the land now (phase 1), the rest later
        starter = options["starter"]
        # the shed, well and solar don't shrink with the area, so search for the largest share that really fits
        lo, hi = 0.02, 1.0
        best = build(profile, "starter", land, develop_share=lo, existing=existing)
        for _ in range(12):
            mid = (lo + hi) / 2
            trial = build(profile, "starter", land, develop_share=mid, existing=existing)
            if trial["capex_total"] <= (budget or 0):
                lo, best = mid, trial
            else:
                hi = mid
        share = lo
        if best["capex_total"] > (budget or 0):
            warnings.append("Even a small start costs more than the budget: the fixed costs (shed, well, solar) come first.")
        phase = {"share": round(share, 2), "now_m2": best["growing_m2"], "full_capex": starter["capex_total"]}
        warnings.append(f"The budget covers about {share:.0%} of the land for now: start there, and grow the rest "
                        "from the first seasons' profit.")
    if best["profit"] <= 0:
        warnings.append("Even the best option loses money on these assumptions: check the prices in "
                        "farms/market_prices.json and the costs in farms/costs.json, or start smaller with family labour.")
    if best["payback_years"] and best["payback_years"] > 5:
        warnings.append(f"It takes about {best['payback_years']} years to earn back the set-up cost: consider a smaller start.")
    hot_rest = [z["zone"] for z in best["zones"] for r in z["crops"] if r["season"] == "hot" and not r["crop"]]
    if hot_rest:
        warnings.append("Open field rests in summer (May–Sep): solarise the soil and use the time for repairs.")
    current = None
    if existing and current_key:
        current = build(profile, "starter", land, existing=True, fixed_crop=current_key)
    return {"stage": "land" if not existing else "farming", "budget_qr": budget, "options": options,
            "recommended": best, "phase": phase, "current": current, "warnings": warnings,
            "assumptions": "Planning estimates (farms/costs.json) and Qatar Open Data prices and yields; sales are "
                           "before costs, profit is after the listed costs. Check with local suppliers."}


def summary_text(p: dict[str, Any]) -> str:
    """Compact numbers for the Finance department and the Director."""
    r = p["recommended"]
    lines = [f"Recommended design: {r['label'][0]} on {r['growing_m2']:,} m² of growing area"
             + (f" (phase 1: {p['phase']['share']:.0%} of the land)" if p["phase"] else "") + "."]
    lines.append(f"Set-up cost ≈ {r['capex_total']:,} QR" + (f" (budget {p['budget_qr']:,} QR)" if p["budget_qr"] else " (no budget given)")
                 + f"; yearly sales ≈ {r['sales']:,} QR; yearly running costs ≈ {r['opex_total']:,} QR; profit ≈ {r['profit']:,} QR/yr"
                 + (f"; pays back in ≈ {r['payback_years']} years." if r["payback_years"] else "; does not pay back."))
    for z in r["zones"]:
        crops = "; ".join(f"{c['season']}: {c['name']} {c['share']:.0%}" for c in z["crops"])
        lines.append(f"{z['zone']} {COVER_LABEL[z['cover']][0]} {z['area_m2']:,} m²: {crops}.")
    if p.get("current"):
        c, mix = p["current"], p["options"]["starter"]
        lines.append(f"Today (current crop only, same beds): sales ≈ {c['sales']:,} QR/yr, profit ≈ {c['profit']:,} QR/yr "
                     f"({'profitable' if c['profit'] > 0 else 'LOSING MONEY'}).")
        if mix["profit"] > c["profit"]:
            lines.append(f"A mixed crop combination on the same beds, no new buildings, would earn more: ≈ {mix['profit']:,} QR/yr "
                         f"(+{mix['profit'] - c['profit']:,} QR/yr).")
        else:
            lines.append(f"Keep the current crop: it earns more than a mixed combination on the same beds "
                         f"(≈ {mix['profit']:,} QR/yr, {mix['profit'] - c['profit']:,} QR/yr less). Diversifying would only "
                         "be for spreading price and pest risk.")
    lines += [f"Warning: {w}" for w in p["warnings"]]
    return "\n".join(lines)


def site_svg(p: dict[str, Any], ar: bool = False) -> str:
    """A simple site plan: the zones as coloured bands, and the buildings along the road side."""
    r = p["recommended"]
    colours = {"open": "#7cc47f", "shade": "#b9d98a", "greenhouse": "#8ec5e8"}
    w, h, pad = 640, 360, 14
    inner_w, inner_h = w - 2 * pad, h - 2 * pad - 70
    parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;'
             'font-family:Segoe UI,Arial,sans-serif">',
             f'<rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="16" fill="#f3efe4" stroke="#b9a98a" stroke-width="2" '
             'stroke-dasharray="8 5"/>']
    x = pad
    total = sum(z["area_m2"] for z in r["zones"]) or 1
    minimum = 170
    widths = [max(minimum, inner_w * z["area_m2"] / total) for z in r["zones"]]
    scale = inner_w / sum(widths)                       # keep small zones readable, then fit everything in
    for z, zw in zip(r["zones"], widths):
        zw *= scale
        parts.append(f'<rect x="{x:.0f}" y="{pad}" width="{zw:.0f}" height="{inner_h}" rx="10" fill="{colours[z["cover"]]}" '
                     'stroke="#ffffff" stroke-width="3"/>')
        if z["cover"] == "open":
            for k in range(1, 6):   # crop rows
                yy = pad + inner_h * k / 6
                parts.append(f'<line x1="{x + 10:.0f}" y1="{yy:.0f}" x2="{x + zw - 10:.0f}" y2="{yy:.0f}" stroke="#5aa55e" '
                             'stroke-width="2" stroke-dasharray="3 6"/>')
        elif z["cover"] == "shade":
            parts.append(f'<rect x="{x + 6:.0f}" y="{pad + 6}" width="{zw - 12:.0f}" height="{inner_h - 12}" rx="6" fill="none" '
                         'stroke="#6f8f3a" stroke-width="2" stroke-dasharray="2 4"/>')
        else:
            for k in range(1, 5):   # greenhouse arches
                xx = x + zw * k / 5
                parts.append(f'<line x1="{xx:.0f}" y1="{pad + 8}" x2="{xx:.0f}" y2="{pad + inner_h - 8}" stroke="#4d93c4" stroke-width="2"/>')
        label = COVER_LABEL[z["cover"]][1 if ar else 0]
        crops = ", ".join(dict.fromkeys((c["ar"] if ar else c["name"].split(" (")[0]) for c in z["crops"] if c["crop"]))
        cx = x + zw / 2
        parts.append(f'<text x="{cx:.0f}" y="{pad + inner_h / 2 - 14:.0f}" text-anchor="middle" font-size="{15 if zw > 200 else 13}" font-weight="700" fill="#1f3b22">{label}</text>')
        parts.append(f'<text x="{cx:.0f}" y="{pad + inner_h / 2 + 6:.0f}" text-anchor="middle" font-size="13" fill="#1f3b22">{z["area_m2"]:,} m² · {z["beds"]} bed(s)</text>')
        if crops:
            limit = int(zw / 6.5)
            parts.append(f'<text x="{cx:.0f}" y="{pad + inner_h / 2 + 26:.0f}" text-anchor="middle" font-size="11" fill="#2f4f32">'
                         f'{crops if len(crops) <= limit else crops[:limit - 1] + "…"}</text>')
        x += zw
    by = pad + inner_h + 14
    items = [("🏠", "Shed" if not ar else "مخزن", "#c9a27a"), ("💧", f"Tank {r['tank_m3']} m³" if not ar else f"خزان {r['tank_m3']} م³", "#7fb7d9")]
    if r["solar_kw"]:
        items.append(("☀️", f"Solar {r['solar_kw']} kW" if not ar else f"طاقة شمسية {r['solar_kw']} ك.و", "#f0c75e"))
    items.append(("📡", "Hydro Monitor master" if not ar else "وحدة التحكم", "#b7a7d9"))
    bx = pad
    for icon, text, colour in items:
        parts.append(f'<rect x="{bx}" y="{by}" width="148" height="42" rx="10" fill="{colour}" opacity="0.9"/>')
        parts.append(f'<text x="{bx + 74}" y="{by + 26}" text-anchor="middle" font-size="13" fill="#222">{icon} {text}</text>')
        bx += 156
    parts.append("</svg>")
    return "".join(parts)

def _site_suitability(profile: dict) -> dict | None:
    lat, lon = profile.get("latitude"), profile.get("longitude")
    if lat is None or lon is None:
        return None
    fc = weather.forecast(lat, lon)          # already cached per-farm, no new rate-limit risk
    wind = wind_dust.wind_risk(fc)
    dust = wind_dust.dust_risk(lat, lon)
    sal = salinity.estimate(profile)
    return site_suitability.score(wind, dust, sal)