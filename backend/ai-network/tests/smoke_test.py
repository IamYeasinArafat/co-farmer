"""Smoke test: every module on both demo farms, no AI calls (they cost quota and time). Run before a demo:

    .venv\\Scripts\\python.exe tests\\smoke_test.py

It checks the farm files, crop suggestions, the farm plan and finance, the onboarding design for bare land and for a
working farm, the code check, the summariser and every code tool, the farmer board helpers in both languages, the
assistant's context, the HTTP API, and that all three dashboard pages render without an error.
"""

from __future__ import annotations

import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

results: list[tuple[str, bool, str]] = []


def check(name: str):
    def wrap(fn):
        try:
            detail = fn() or ""
            results.append((name, True, str(detail)))
        except Exception as err:   # report and keep going
            results.append((name, False, f"{type(err).__name__}: {err}\n{traceback.format_exc(limit=3)}"))
        return fn
    return wrap


import checker  # noqa: E402
import crop_advice  # noqa: E402
import db  # noqa: E402
import farm  # noqa: E402
import farm_plan  # noqa: E402
import farmer_view as fv  # noqa: E402
import network  # noqa: E402
import onboarding  # noqa: E402
import summariser  # noqa: E402

FARMS = farm.list_farms()
con = db.connect()


@check("farm files load and validate")
def _():
    loaded = [farm.load_farm(f) for f in FARMS]
    assert loaded, "no farms"
    return ", ".join(f"{f['id']} ({farm.specialist_count(f['network'])} specialists, extras: "
                     f"{[d['name'] for d in f['network'].get('extras', [])]})" for f in loaded)


@check("crop suggestions")
def _():
    out = []
    for f in FARMS:
        advice = crop_advice.suggest(farm.load_farm(f))
        first = next(iter(advice["fields"].values()))
        assert first["now"] or first["later"], f"{f}: nothing to plant"
        out.append(f"{f}: now {[r['name'] for r in first['now']]}")
    return "; ".join(out)


@check("farm plan and finance")
def _():
    out = []
    for f in FARMS:
        p = farm_plan.plan(farm.load_farm(f)["profile"])
        r = p["recommended"]
        assert r["capex_total"] > 0 and r["sales"] > 0, f"{f}: empty plan"
        assert all(c["margin"] >= 0 for z in r["zones"] for c in z["crops"]), "a losing crop was kept"
        assert all(c["share"] <= 0.6 or len([x for x in z["crops"] if x["season"] == c["season"]]) == 1
                   for z in r["zones"] for c in z["crops"]), "one crop takes too much of a zone"
        farm_plan.site_svg(p)
        farm_plan.site_svg(p, ar=True)
        out.append(f"{f}: {r['label'][0]}, set-up {r['capex_total']:,} QR, profit {r['profit']:,} QR/yr")
    return "; ".join(out)


@check("onboarding design: bare land, within budget")
def _():
    profile = {"location": "Al Shahaniya", "stage": "land", "fields": [], "water": "well", "power": None, "internet": "wifi",
               "budget_qr": 120000, "problems": ["heat"], "land_m2": 8000, "goal": "income", "latitude": 25.37, "longitude": 51.22}
    parts = onboarding.design(profile, "test_land", None)
    farm.validate_network(parts["network"], parts["hardware"])
    r = parts["plan"]["recommended"]
    assert r["capex_total"] <= 120000 * 1.02, "over budget"
    assert parts["profile"]["fields"], "no beds"
    return f"{len(parts['profile']['fields'])} beds, set-up {r['capex_total']:,} QR, phase {parts['plan']['phase']}"


@check("onboarding design: working farm")
def _():
    profile = {"location": "Al Khor", "stage": "farming", "water": "tank, hose", "power": "shed", "internet": "mobile",
               "budget_qr": None, "problems": ["pale leaves"], "fertilizer": "NPK",
               "fields": [{"field_id": "F1", "size_m": [10, 20], "type": "soil bed, open air", "crop": "lettuce", "planted": "2026-09-10"}]}
    parts = onboarding.design(profile, "test_farm", None)
    farm.validate_network(parts["network"], parts["hardware"])
    return onboarding.proposal_text(parts)[:120]


@check("onboarding parsing (no model)")
def _():
    assert onboarding.parse_area("1 hectare") == 10000 and onboarding.parse_area("100 by 50") == 5000
    assert onboarding.parse_money("150k") == 150000 and onboarding.parse_money("150 ألف") == 150000
    assert onboarding._crop_name("خس") == "lettuce"
    assert onboarding._relative_date("قبل أسبوعين") and onboarding._relative_date("three weeks ago")
    p = onboarding.empty_profile()
    p["location"] = "x"
    assert onboarding.missing(p) == ["stage"]
    p = onboarding.rule_fill(p, "I just have the land", "stage")
    assert p["stage"] == "land" and onboarding.missing(p)[0] == "land"
    p = onboarding.rule_fill(p, "none", "land")
    assert p["land_m2"] is None and "land" not in onboarding.missing(p)
    return "area, money, Arabic crop names, dates, stage and null handling"


@check("code check (clamp, step limit, rejection)")
def _():
    hard = {"soil_moisture": [25, 55], "temp_air": [15, 32], "humidity": [35, 85], "level": [15, 100], "pump_seconds": [10, 300]}
    cur = {"F1": {"soil_moisture": [30, 45], "temp_air": [24, 32], "humidity": [40, 80], "pump_seconds": 120}}
    kinds = {"F1": ["temp_air", "humidity", "soil_moisture"]}
    r, f = checker.check({"F1": {**cur["F1"], "pump_seconds": 10}}, cur, hard, kinds)
    assert r["F1"]["pump_seconds"] == 60 and f[0]["type"] == "limited"
    r, f = checker.check({"F1": {**cur["F1"], "soil_moisture": [25, 55]}}, cur, hard, kinds)
    assert r["F1"]["soil_moisture"] == [30, 45] and f[0]["type"] == "rejected"
    r, f = checker.check({"F1": {**cur["F1"], "temp_air": [24, 36]}}, cur, hard, kinds)
    assert r["F1"]["temp_air"] == [24, 32] and f[0]["type"] == "clamped"
    return "limited / rejected / clamped all behave"


@check("summariser and every code tool")
def _():
    out = []
    for f in FARMS:
        fm = farm.load_farm(f)
        s = summariser.summarise(con, fm)
        if not any(s["history"].values()):
            out.append(f"{f}: no readings yet (expected for bare land)")
            continue
        version, current = network.ranges_in_force(con, fm)
        groups = fm["network"]["departments"] + fm["network"].get("extras", [])
        tools = network.code_tools(fm, s, current, groups)
        missing_depts = [d["id"] for d in groups if not tools.get(d["id"])]
        assert not missing_depts, f"{f}: no code-tool results for {missing_depts}"
        brief = network.director_brief(fm, s, version, current, {}, crop_advice.suggest(fm), farm_plan.plan(fm["profile"]))
        assert "Hard limits" in brief
        out.append(f"{f}: {len(groups)} departments ran")
    return "; ".join(out)


@check("farmer board helpers (English and Arabic)")
def _():
    for f in FARMS:
        fm = farm.load_farm(f)
        plan = db.latest_plan(con, f)
        for ar in (False, True):
            cards = fv.bed_cards(con, fm, plan, ar)
            assert cards and all(c["headline"] for c in cards)
        fv.market_tips(plan)
    missing = [k for k, v in fv.T.items() if len(v) != 2 or not v[1]]
    assert not missing, f"labels without Arabic: {missing}"
    return f"{len(fv.T)} labels, bed cards for {FARMS}"


@check("assistant context")
def _():
    import assistant

    for f in FARMS:
        text = assistant.build_context(farm.load_farm(f), db.latest_plan(con, f))
        assert "Farm:" in text
    return "ok"


@check("HTTP API (server.py)")
def _():
    from fastapi.testclient import TestClient

    import server

    client = TestClient(server.app)
    v = client.get("/ranges/version").json()["version"]
    assert client.get(f"/ranges?have={v}").status_code == 304
    assert client.get("/ranges").json()["version"] == v
    assert client.post("/log", content="garbage,row").status_code == 400
    return f"version {v}, 304 on ?have, 400 on a bad row"


@check("dashboard pages render")
def _():
    from streamlit.testing.v1 import AppTest

    out = []
    for f in FARMS:
        farm.set_active(f)
        at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=180).run()
        assert not at.exception, f"{f} My farm: {[e.value for e in at.exception]}"
        out.append(f"{f}: My farm ok ({len(at.tabs)} tabs)")
    farm.set_active("alkhor" if "alkhor" in FARMS else FARMS[0])
    return "; ".join(out)


print()
width = max(len(n) for n, _, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail.splitlines()[0][:150] if ok else ''}")
    if not ok:
        print("      " + detail.replace("\n", "\n      "))
failed = sum(1 for _, ok, _ in results if not ok)
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)
