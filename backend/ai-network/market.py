"""Market and climate data for the Market & Strategy department.

Three layers, most trusted first:
  1. Official Qatar Open Data (data.gov.qa), downloaded and cached in farms/market_cache.json:
     retail prices, farm-gate value of vegetables, self-sufficiency, Qatari yields, monthly climate.
  2. farms/market_prices.json: the price table the profitability agent uses. Seeded with official and
     researched figures (each with source, date and confidence). The farmer can edit it any time.
  3. Web research (agents/strategy.py, Tavily): recent prices for crops the official data doesn't cover,
     stored in farms/research_cache.json and always marked unverified.

    python market.py          # refresh the official cache now
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

FARMS = Path(__file__).parent / "farms"
CACHE = FARMS / "market_cache.json"
PRICES = FARMS / "market_prices.json"
RESEARCH = FARMS / "research_cache.json"
API = "https://www.data.gov.qa/api/explore/v2.1/catalog/datasets/{}/exports/json"
DATASETS = {
    "retail": "annual_average_consumer_prices_selected_commodities",
    "value": "value-of-agricultural-and-fish-production",
    "quantity": "quantity-of-agricultural-production-and-self-sufficiency",
    "yields": "production-area-and-average-yield-of-crops",
    "greenhouse": "cropped-area-and-production-of-crops-in-greenhouses",
    "climate": "monthly-temperature-and-relative-humidity-statistics-qatar",
}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _download(dataset: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(API.format(dataset), headers={"User-Agent": "quars-agent-network"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _year(row: dict[str, Any]) -> int:
    return int(str(row.get("year"))[:4])


def _per_kg(price: float, unit: str) -> float | None:
    match = re.match(r"\s*([\d.]+)\s*(kg|gm|g)\b", unit or "", re.I)
    if not match:
        return None
    amount = float(match.group(1)) / (1000 if match.group(2).lower() in ("gm", "g") else 1)
    return round(price / amount, 2) if amount else None


def build_summary(raw: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    retail = {}
    for row in raw["retail"]:
        if row.get("category") not in ("Fresh Vegetables", "Fresh Fruits", "Potato &  Potato Products"):
            continue
        per_kg = _per_kg(row.get("average_price_qr") or 0, row.get("unit_of_measure", ""))
        key = row["commodity"].strip()
        if per_kg and (key not in retail or _year(row) > retail[key]["year"]):
            retail[key] = {"qr_per_kg": per_kg, "year": _year(row)}

    def latest(rows: list[dict[str, Any]], **match: str) -> dict[str, Any] | None:
        hits = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
        return max(hits, key=_year) if hits else None

    value, quantity = latest(raw["value"], product="Vegetables"), latest(raw["quantity"], product="Vegetables")
    farmgate = None
    if value and quantity and quantity.get("quantity"):
        # value is published in thousand QR and quantity in tonnes, so thousand QR / tonne = QR / kg
        farmgate = {"qr_per_kg": round(value["value"] / quantity["quantity"], 2), "year": _year(value)}

    top_year = max(_year(r) for r in raw["quantity"])
    self_sufficiency = {r["product"]: round(r["self_sufficiency_ratio"], 3) for r in raw["quantity"]
                        if _year(r) == top_year and r.get("self_sufficiency_ratio") is not None}

    yield_year = max(_year(r) for r in raw["yields"])
    yields = {r["crop"].strip(): r["yield"] for r in raw["yields"] if _year(r) == yield_year and r.get("yield")}
    gh_year = max(_year(r) for r in raw["greenhouse"])
    greenhouse = {r["crops"].strip(): round(r["production"] / r["area"], 1)
                  for r in raw["greenhouse"] if _year(r) == gh_year and r.get("area")}

    climate_years = sorted({_year(r) for r in raw["climate"]})[-5:]
    acc: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in raw["climate"]:
        if _year(r) in climate_years and r.get("value") is not None:
            kind = "t" if r["climate_indicator"].startswith("Temp") else "rh"
            acc[(r["month"], kind + ("max" if r["statistical_measure"].startswith("Max") else "min"))].append(r["value"])
    climate = {m: {k: round(mean(acc[(m, k)]), 1) for k in ("tmax", "tmin", "rhmax", "rhmin") if acc[(m, k)]}
               for m in MONTHS}

    return {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "Qatar Open Data portal (data.gov.qa), Planning and Statistics Authority",
        "retail_prices": retail,
        "farmgate_vegetables": farmgate,
        "self_sufficiency": {"year": top_year, "ratios": self_sufficiency},
        "yields_open_field_t_ha": {"year": yield_year, "crops": yields},
        "yields_greenhouse_t_ha": {"year": gh_year, "crops": greenhouse},
        "climate_normals": {"years": f"{climate_years[0]}-{climate_years[-1]}", "months": climate},
    }


def refresh(max_age_days: float = 7, force: bool = False) -> dict[str, Any]:
    """Return the official summary, re-downloading when the cache is old. Falls back to the cache offline."""
    cached = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else None
    if cached and not force:
        age = datetime.now() - datetime.fromisoformat(cached["fetched_at"])
        if age < timedelta(days=max_age_days):
            return cached
    try:
        summary = build_summary({k: _download(v) for k, v in DATASETS.items()})
        CACHE.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return summary
    except Exception:
        if cached:
            return cached
        raise


def prices() -> dict[str, Any]:
    return json.loads(PRICES.read_text(encoding="utf-8"))


def research_cache() -> dict[str, Any]:
    return json.loads(RESEARCH.read_text(encoding="utf-8")) if RESEARCH.exists() else {}


def save_research(entries: dict[str, Any]) -> None:
    RESEARCH.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    data = refresh(force=True)
    print(f"cached {CACHE.name}: {len(data['retail_prices'])} retail prices, "
          f"vegetable farm-gate {data['farmgate_vegetables']}, climate {data['climate_normals']['years']}")
