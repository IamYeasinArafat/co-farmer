"""Market & Strategy (an extra department, advice only): code tools for prices, crop fit and profitability.

These look beyond today's readings: local prices (Qatar Open Data and the farm's price table), which crops
fit these beds in each season, and which changes would raise gross revenue. Their output is advice; it
never changes a range.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

import llm
import market
from knowledge import CANDIDATE_CROPS

from .base import done, resolve_model

MONTHS = market.MONTHS
SEASONS = {"Winter (Dec–Feb)": [11, 0, 1], "Spring (Mar–May)": [2, 3, 4],
           "Summer (Jun–Aug)": [5, 6, 7], "Autumn (Sep–Nov)": [8, 9, 10]}
RESEARCH_TTL_H = 12      # reuse web research for 12 h so twice-daily briefings spend few search credits


# ── shared helpers ───────────────────────────────────────────────────────
def _crop_key(farm: dict[str, Any]) -> str:
    return farm["crop"].lower().replace(" ", "_")


def price_per_kg(crop: str, table: dict[str, Any]) -> tuple[float | None, str, str]:
    """(QR/kg the grower receives, basis, confidence)."""
    row = table["crops"].get(crop)
    if not row:
        avg = table["vegetable_average_farmgate"]
        return avg["qr_per_kg"], "vegetable average farm-gate", "proxy"
    if row.get("farmgate"):
        return row["farmgate"], "farm-gate", row["confidence"]
    if row.get("retail"):
        share = table.get("retail_to_farmgate_share", 0.45)
        return round(row["retail"] * share, 2), f"{share:.0%} of retail", row["confidence"]
    return None, "no price", "none"


def indoor_climate(state: dict[str, Any]) -> dict[str, float]:
    """Mean daily max/min air temperature and humidity these beds have actually had."""
    rows = state.get("archive") or state.get("history") or [state["reading"]]
    days: dict[str, list[float]] = {}
    for r in rows:
        if isinstance(r.get("air_temperature"), (int, float)):
            days.setdefault(str(r["timestamp"])[:10], []).append(r["air_temperature"])
    rh = [r["humidity"] for r in rows if isinstance(r.get("humidity"), (int, float))]
    return {
        "tmax": round(mean(max(v) for v in days.values()), 1) if days else float("nan"),
        "tmin": round(mean(min(v) for v in days.values()), 1) if days else float("nan"),
        "rh": round(mean(rh), 1) if rh else float("nan"),
        "days": len(days),
    }


PASSIVE_GAIN_C = {"tmax": 2.0, "tmin": 1.0}   # an uncooled greenhouse runs a little warmer than outside


def projected_months(state: dict[str, Any], summary: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Rough indoor climate per month from Qatar's normals and this greenhouse's observed cooling.

    Cooling capacity = how far the greenhouse is below outside today. In a hotter month the cooling pulls
    temperatures down toward today's indoor level, at most by that capacity; in cooler months it is off and
    the greenhouse runs slightly warmer than outside.
    """
    normals = summary["climate_normals"]["months"]
    now = datetime.fromisoformat(str(state["reading"]["timestamp"]))
    here, outside_now = indoor_climate(state), normals[MONTHS[now.month - 1]]
    out = {}
    for m, n in normals.items():
        month = {}
        for k in ("tmax", "tmin"):
            capacity = max(0.0, outside_now[k] - here[k])   # how far the cooling gets below outside today
            value = n[k] + PASSIVE_GAIN_C[k]
            if value > here[k]:                               # hot enough for the cooling to run
                value = max(here[k], n[k] - capacity)
            month[k] = round(value, 1)
        out[m] = month
    return out


def month_fit(crop: dict[str, Any], month: dict[str, float]) -> float:
    lo, hi, acc_lo, acc_hi = crop["air"]
    if month["tmax"] <= hi and month["tmin"] >= lo:
        return 1.0
    if month["tmax"] <= acc_hi and month["tmin"] >= acc_lo:
        return 0.5
    return 0.0


def windows(fits: dict[str, float]) -> str:
    """'Nov-Mar' style label for the months a crop fits (score >= 0.5), wrapping around the year."""
    good = [fits[m] >= 0.5 for m in MONTHS]
    if all(good):
        return "all year"
    if not any(good):
        return "never"
    start = next(i for i in range(12) if good[i] and not good[i - 1])
    spans, i = [], start
    for _ in range(12):
        if good[i % 12] and not good[(i - 1) % 12]:
            begin = i % 12
        if good[i % 12] and not good[(i + 1) % 12]:
            spans.append(MONTHS[begin] if begin == i % 12 else f"{MONTHS[begin]}–{MONTHS[i % 12]}")
        i += 1
    return ", ".join(spans)


def crop_table(state: dict[str, Any]) -> list[dict[str, Any]]:
    summary, table = market.refresh(), market.prices()
    months = projected_months(state, summary)
    rows = []
    for key, crop in CANDIDATE_CROPS.items():
        fits = {m: month_fit(crop, months[m]) for m in MONTHS}
        price, basis, confidence = price_per_kg(key, table)
        kg_m2 = crop["yield_t_ha"] / 10
        monthly = [kg_m2 / crop["cycle_days"] * 30 * (price or 0) * fits[m] for m in MONTHS]
        rows.append({"crop": key, "name": crop["name"], "fits": fits, "window": windows(fits),
                     "price": price, "price_basis": basis, "confidence": confidence,
                     "yield_source": crop["yield_source"], "kg_m2_cycle": round(kg_m2, 2), "cycle_days": crop["cycle_days"],
                     "monthly_qr_m2": [round(v, 2) for v in monthly], "annual_qr_m2": round(sum(monthly), 1)})
    return rows


# ── agents ───────────────────────────────────────────────────────────────
def market_watch(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    summary, table = market.refresh(), market.prices()
    crop = _crop_key(state["farm"])
    price, basis, confidence = price_per_kg(crop, table)
    veg = summary["self_sufficiency"]["ratios"].get("Vegetables")
    retail = summary["retail_prices"]
    local = {k: v for k, v in retail.items() if "Qatar" in k}
    lines = [
        f"Qatar produced {veg:.0%} of the vegetables it consumed in {summary['self_sufficiency']['year']}, so most produce is imported: local supply has room to grow." if veg else "",
        f"Official average farm-gate value of vegetables: {summary['farmgate_vegetables']['qr_per_kg']} QR/kg ({summary['farmgate_vegetables']['year']}).",
        "Official retail prices for local produce: " + ", ".join(f"{k.split('/')[0].strip()} {v['qr_per_kg']} QR/kg ({v['year']})" for k, v in local.items()) + ".",
        f"Current crop ({state['farm']['crop']}): {price} QR/kg ({basis}, confidence: {confidence}).",
    ]
    if confidence in ("proxy", "none"):
        lines.append(f"No published price for {state['farm']['crop']}: enter your buyer's price in farms/market_prices.json.")
    result = {"status": "OK", "summary": " ".join(l for l in lines if l), "issues": [],
              "data_age": summary["fetched_at"], "sources": [summary["source"], "farms/market_prices.json"]}
    return done(result)


def crop_fit(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    rows = crop_table(state)
    here = indoor_climate(state)
    month = MONTHS[datetime.fromisoformat(str(state["reading"]["timestamp"])).month - 1]
    now_fit = [r["name"] for r in rows if r["fits"][month] >= 0.5]
    lines = [f"These beds have averaged {here['tmax']}°C by day and {here['tmin']}°C by night over {here['days']} day(s).",
             f"Crops that fit {month}: {', '.join(now_fit) or 'none of the candidates'}."]
    lines += [f"{r['name']}: {r['window']}" for r in rows]
    result = {"status": "OK", "summary": " ".join(lines[:2]) + " Seasons: " + "; ".join(lines[2:]) + ".", "issues": [],
              "table": [{k: r[k] for k in ("crop", "name", "window", "fits")} for r in rows]}
    return done(result)


def profitability(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    rows = crop_table(state)
    crop_key = _crop_key(state["farm"])
    current = next((r for r in rows if r["crop"] == crop_key), None)
    if current is None:
        crop_name = state["farm"].get("crop") or "the current crop"
        result = {
            "status": "INFO",
            "summary": f"Profitability comparison is deferred because the farm crop is '{crop_name}', which is not yet mapped to a candidate crop profile.",
            "issues": [],
            "suggestions": [],
            "seasons": [],
            "beats_current_yearly": [],
            "ranking": [{k: r[k] for k in ("name", "annual_qr_m2", "price", "price_basis", "confidence", "window")}
                        for r in sorted(rows, key=lambda r: -r["annual_qr_m2"])],
        }
        return done(result)
    base = current["annual_qr_m2"] or 0.01
    # two-season rotation: keep the current crop in its best months, switch to ONE alternative in the rest
    best_alt, rotation_total, alt_months = None, base, []
    for alt in rows:
        if alt["crop"] == current["crop"]:
            continue
        months = [m for i, m in enumerate(MONTHS) if alt["monthly_qr_m2"][i] > current["monthly_qr_m2"][i]]
        total = round(sum(max(a, c) for a, c in zip(alt["monthly_qr_m2"], current["monthly_qr_m2"])), 1)
        if months and total > rotation_total:
            best_alt, rotation_total, alt_months = alt, total, months
    if best_alt:
        alt_window = windows({m: 1.0 if m in alt_months else 0.0 for m in MONTHS})
        cur_window = windows({m: 0.0 if m in alt_months else 1.0 for m in MONTHS})
        segments = [f"{best_alt['name']} {alt_window}", f"{current['name']} {cur_window}"]

    suggestions = []
    for r in sorted(rows, key=lambda r: -r["annual_qr_m2"]):
        if r["crop"] != current["crop"] and r["annual_qr_m2"] > base * 1.1 and r["window"] != "never":
            gain = round(100 * (r["annual_qr_m2"] / base - 1))
            suggestions.append({"type": "crop", "title": f"Grow {r['name']} ({r['window']})", "gain_pct": gain,
                                "detail": f"≈{r['annual_qr_m2']} QR/m²/yr vs {base} for {current['name']}; "
                                          f"price {r['price']} QR/kg ({r['price_basis']}, {r['confidence']}), yield {r['yield_source']}.",
                                "confidence": r["confidence"]})
    if best_alt and rotation_total > base * 1.1:
        suggestions.insert(0, {"type": "rotation", "title": "Seasonal rotation: " + ", ".join(segments),
                               "gain_pct": round(100 * (rotation_total / base - 1)),
                               "detail": f"≈{rotation_total} QR/m²/yr vs {base} growing only {current['name']}.",
                               "confidence": f"{current['confidence']} / {best_alt['confidence']}"})
    alternatives = [r for r in sorted(rows, key=lambda r: -r["annual_qr_m2"]) if r["crop"] != current["crop"] and r["price"]]
    best = alternatives[0] if alternatives else None
    if best and current["price"] and current["confidence"] in ("proxy", "estimate", "none"):
        # the current crop's price is not confirmed: say where the decision flips
        break_even = round(current["price"] * best["annual_qr_m2"] / base, 2)
        suggestions.append({"type": "price", "title": f"Confirm your {current['name'].lower()} price",
                            "gain_pct": 0,
                            "detail": f"Its price here is a {current['confidence']} ({current['price']} QR/kg). If your buyer pays "
                                      f"less than {break_even} QR/kg, {best['name']} ({best['window']}) earns more per m².",
                            "confidence": current["confidence"]})
    diversify = next((r for r in alternatives if r["confidence"] == "official" and r["window"] != "never"), None)
    if diversify and not any(diversify["name"] in s_["title"] for s_ in suggestions):
        share = round(100 * diversify["annual_qr_m2"] / base)
        suggestions.append({"type": "diversify", "title": f"Diversify with {diversify['name']} ({diversify['window']})",
                            "gain_pct": share - 100,
                            "detail": f"Backed by an official Qatar price ({diversify['price']} QR/kg, {diversify['price_basis']}); "
                                      f"≈{diversify['annual_qr_m2']} QR/m²/yr ({share}% of {current['name'].lower()}). "
                                      "A second crop spreads price and pest risk.",
                            "confidence": diversify["confidence"]})
    suggestions = suggestions[:3]
    summary = (f"{current['name']} earns an estimated {base} QR/m²/yr gross ({current['price']} QR/kg, {current['confidence']} price). "
               + ("" if any(s["gain_pct"] > 0 for s in suggestions) else "No candidate beats it by more than 10%. ")
               + " ".join(f"{s['title']}: {s['detail']}" for s in suggestions)
               + " Gross revenue only; seed, labour, energy and water costs are not included.")
    seasons = []
    for label, idx in SEASONS.items():
        cur = sum(current["monthly_qr_m2"][i] for i in idx)
        top = max(rows, key=lambda r: sum(r["monthly_qr_m2"][i] for i in idx))
        best = sum(top["monthly_qr_m2"][i] for i in idx)
        if top["crop"] == current["crop"] or best <= cur:
            seasons.append(f"{label}: {current['name']} earns most ({cur:.1f} QR/m²)")
        else:
            seasons.append(f"{label}: {top['name']} earns more than {current['name']} ({best:.1f} vs {cur:.1f} QR/m², "
                           f"+{round(100 * (best / cur - 1)) if cur else 100}%, {top['confidence']} price)")
    beats = [r["name"] for r in rows if r["crop"] != current["crop"] and r["annual_qr_m2"] > base]
    result = {"status": "OK", "summary": summary, "issues": [], "suggestions": suggestions, "seasons": seasons,
              "beats_current_yearly": beats,
              "ranking": [{k: r[k] for k in ("name", "annual_qr_m2", "price", "price_basis", "confidence", "window")}
                          for r in sorted(rows, key=lambda r: -r["annual_qr_m2"])]}
    return done(result)


def _tavily(query: str) -> dict[str, Any] | None:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return None
    body = {"query": query, "search_depth": "basic", "include_answer": True, "max_results": 5}
    request = urllib.request.Request("https://api.tavily.com/search", json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.loads(response.read())
    except Exception:
        return None


def research_topics(state: dict[str, Any]) -> list[str]:
    rows = sorted(crop_table(state), key=lambda r: -r["annual_qr_m2"])
    crops = [state["farm"]["crop"]] + [r["name"] for r in rows if r["crop"] != _crop_key(state["farm"])][:2]
    return [f"{c} price per kg Qatar {datetime.now().year}" for c in crops]


def research(queries: list[str]) -> list[dict[str, Any]]:
    """Web search with a 12 h cache; returns [{query, answer, sources[], fetched_at}]."""
    cache, out = market.research_cache(), []
    for q in queries:
        hit = cache.get(q)
        if hit and datetime.now() - datetime.fromisoformat(hit["fetched_at"]) < timedelta(hours=RESEARCH_TTL_H):
            out.append(hit)
            continue
        data = _tavily(q)
        if data is None:
            continue
        entry = {"query": q, "answer": data.get("answer") or "",
                 "sources": [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("content") or "")[:300]}
                             for r in data.get("results", [])[:5]],
                 "fetched_at": datetime.now().isoformat(timespec="seconds")}
        cache[q] = entry
        out.append(entry)
    market.save_research(cache)
    return out


def web_research(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if not os.environ.get("TAVILY_API_KEY"):
        return {"status": "OK", "issues": [], "notes": [], "model": {"assigned": agent.get("model"), "used": None},
                "summary": "Web research is off: add a free TAVILY_API_KEY to arm/.env. Using official data and the price table.",
                "facts": ""}
    notes = research(research_topics(state))
    facts = "\n".join(f"- {n['query']}: {n['answer'] or 'no direct answer'} (sources: "
                      + ", ".join(s["url"] for s in n["sources"][:3]) + ")" for n in notes)
    result: dict[str, Any] = {"status": "OK", "issues": [], "notes": notes, "summary": facts or "No web results.",
                              "model": {"assigned": agent.get("model"), "used": None}, "facts": facts}
    if notes and state.get("llm_mode", "off") not in ("off", "director"):
        model = resolve_model(agent.get("model"), state)
        text = llm.chat(model, agent.get("prompt", "You are a market researcher."),
                        f"Web search results (unverified):\n{facts}\n\nIn 2-3 sentences, summarise what these say about current "
                        "prices in Qatar. Quote QR amounts only if they appear above, name the source site, and say they are unverified.")
        if text:
            result["summary"], result["model"]["used"] = text, model
    return result


def crop_suggest(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Crop Suggestion (plan 3.3): what to plant in each bed now and in the coming months (crop_advice.py)."""
    import crop_advice

    advice = crop_advice.suggest({"profile": state["farm"]["profile"]})
    return done({"status": "OK", "summary": crop_advice.summary_text(advice), "issues": [], "crops": advice})


def finance_check(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Profit & Budget: sales, running costs, set-up cost, payback and the budget for the recommended design and the
    best crop combination (farm_plan.py). Flags anything that doesn't pay."""
    import farm_plan

    result = farm_plan.plan(state["farm"]["profile"])
    best = result["recommended"]
    issues = []
    if best["profit"] <= 0:
        issues.append({"kind": "risk", "field": "profit", "direction": "low", "severity": "CRITICAL",
                       "message": f"The recommended design loses about {abs(best['profit']):,} QR a year on these assumptions"})
    elif best["payback_years"] and best["payback_years"] > 5:
        issues.append({"kind": "risk", "field": "payback", "direction": "high", "severity": "WARNING",
                       "message": f"Set-up cost takes about {best['payback_years']} years to earn back"})
    current = result.get("current")
    if current and current["profit"] < result["options"]["starter"]["profit"]:
        gain = result["options"]["starter"]["profit"] - current["profit"]
        issues.append({"kind": "risk", "field": "crop_mix", "direction": "low", "severity": "WARNING",
                       "message": f"A better crop combination on the same beds could add about {gain:,} QR a year"})
    status = "CRITICAL" if any(i["severity"] == "CRITICAL" for i in issues) else "WARNING" if issues else "OK"
    return done({"status": status, "summary": farm_plan.summary_text(result), "issues": issues, "farm_plan": result})
