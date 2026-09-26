# Hydro Monitor: architecture and how the code works

This document covers:
1. The big picture
2. The modules
3. One agent run, step by step
4. The onboarding assistant
5. Crop suggestion
6. The dashboard
7. The database (`data/hydro.db`), table by table
8. Configuration files
9. How to extend it

Every path is relative to the repository root.

---

## 1. The big picture

Hydro Monitor has two parts, and two small formats connect them (plan 1.3).

```
            IOT PART (in the fields)                               AGENTIC PART (the laptop)
 ┌──────────────┐  radio   ┌──────────────────────┐  POST /log   ┌─────────────┐      ┌──────────────────┐
 │ Sensor set   │ ───────► │ MASTER (ESP32 + SD)  │ ───────────► │ server.py   │ ───► │ data/hydro.db    │
 │ S1, S2 …     │  10 s    │ logs, checks ranges, │  CSV rows    │ (FastAPI)   │      │ (SQLite)         │
 └──────────────┘          │ switches the pumps   │ ◄─────────── │             │ ◄─── │ log, plans, runs │
 ┌──────────────┐  relay   │                      │  GET /ranges └─────────────┘      │ approvals, notes │
 │ Actuator set │ ◄─────── │                      │  (versioned)                      └────────┬─────────┘
 │ A1, A2 pump  │          └──────────────────────┘                                            │
 └──────────────┘                                                                              ▼
                               every 6 h / "Get fresh advice":  network.py  (summariser → code tools →
                                                                CrewAI departments → Farm Director → checker)
                                                                               │
                               app.py (Streamlit): My farm · Set up a farm · Developer
```

- **Log rows** go up: every reading and every pump switch, in the plan's CSV format.
- **Ranges** come down: a versioned JSON file with the allowed band per field and setting. The master enforces it and keeps the last version on its SD card, so the farm stays safe even if the laptop is off.

Three rules shape the whole design:

1. **Code counts, agents judge, code checks.**
   - Every number comes from code: the summariser and the code tools.
   - The AI agents turn the numbers into judgements, plans and plain-language advice.
   - Code then clamps anything the agents propose inside the farm's hard limits before the master sees it.
2. **The agents never switch hardware.** They set ranges, and the master's firmware decides what each range means.
3. **The farmer board never waits for a model.** Bed status and crop suggestions are computed by code; the AI's message is an extra on top.

---

## 2. The modules

### Entry points (things you run)

| File | What it does |
|---|---|
| `app.py` | Streamlit dashboard with three pages: **My farm** (farmer board), **Set up a farm** (onboarding), **Developer** |
| `server.py` | FastAPI endpoint for the master (`POST /log`, `GET /ranges`, `GET /ranges/version`, `POST /run`, `GET /status`), plus the 6-hour schedule |
| `network.py` | One agent run: `run()`. Also a CLI: `python network.py [--route local]` |
| `seed_demo.py` | Fills the database with a 48-hour "heatwave day" for the demo farm (plan milestone M6) |
| `fake_master.py` | A fake ESP32 master over HTTP: posts rows every 10 s, downloads and enforces ranges, `dry F1` pulls a probe |

### The agentic core

| File | What it does |
|---|---|
| `summariser.py` | **Code, no AI.** Reads 48 h of log rows and computes, per field:<br>• now, min, max, average and trend of each reading<br>• pump runs, and how fast the soil dries after 10:00<br>• yesterday's peak, the forecast, the farmer's note<br>• the bed-to-bed comparison<br>It also resamples readings into 5-minute steps for the code tools. |
| `agents/` | The **code tools**, one skill per specialist, including `finance_check` (Profit & Budget, for the Finance department). Each counts and checks one thing and returns a finding (status, issues, summary):<br>• `monitors.py`: range checks<br>• `soil.py`: irrigation, fertilizer<br>• `crop_science.py`: physiology, leaf colour, pests and disease<br>• `analytics.py`: trend, anomaly, 6 h projection<br>• `strategy.py`: market price, crop fit, profitability, web research, crop suggestion<br>• `diagnosis.py`: the team's farm-qwen fine-tune, not used on the demo farm<br>`agents/__init__.py` maps skill names to functions (`SKILLS`). |
| `crew.py` | The **CrewAI** layer:<br>• the fixed JSON formats (pydantic models `DepartmentReport`, `DirectorPlan`, `Translation`)<br>• one CrewAI `Agent` per department built from `network.json`<br>• rate-limit fallbacks: `_run_one()` retries a task on the next OpenRouter model after a 429<br>• the Arabic translation, with a script check |
| `checker.py` | The **code check** (plan 3.4 step 5):<br>• clamps every range into the hard limits<br>• limits how far one plan may move a setting (`MAX_STEP`: e.g. soil moisture ±10 %, pump time ±50 %)<br>• rejects nonsense (min ≥ max, or a band widened to the whole hard-limit span)<br>• returns the flags shown on the dashboard |
| `crop_advice.py` | **Crop suggestion, code only:** what to plant now and in the coming months, per bed (see section 5) |
| `farm_plan.py` | **Farm design and finance, code only:**<br>• the layout (zones and buildings)<br>• the best crop combination per zone and season<br>• set-up cost, running costs, sales, profit and payback for each design option<br>• the budget check, with a phase 1 when the budget is short<br>• the site-plan drawing<br>Costs come from `farms/costs.json`. See section 5b. |
| `onboarding.py` | The **onboarding assistant**: the conversation engine, then the design of the four farm files (see section 4) |
| `assistant.py` | "Ask the farm": answers the farmer's questions from the latest plan (and a web search for price questions) |
| `farmer_view.py` | Plain-language views for the farmer board: bed cards, Arabic and English text, month names |

### Data and plumbing

| File | What it does |
|---|---|
| `db.py` | SQLite schema and helpers: log rows, plans, runs, approvals, notes (see section 7) |
| `farm.py` | Loads and validates a farm's four files (`load_farm`, `validate_network`), saves approved onboarding outputs, and handles the farm's time zone |
| `knowledge.py` | The **crop file**:<br>• amaranth ranges, impacts and pest rules<br>• growth stages, hard limits and starting ranges<br>• 18 candidate crops, with Qatar yields, climate ranges, water and care levels, and Arabic names<br>• the mapping between the plan's `kind` names and the code tools' field names |
| `weather.py` | Open-Meteo: forecast, geocoding and past weather, with a per-farm offline cache |
| `market.py` | Qatar Open Data: retail prices, farm-gate value, self-sufficiency, yields and climate normals, cached in `farms/market_cache.json`. It also reads the price table `farms/market_prices.json`. |
| `llm.py` | Model access for plain calls: Ollama or OpenAI-compatible clouds (OpenRouter, Groq, Gemini)<br>• per-provider rate budgets<br>• the OpenRouter fallback list<br>• `.env` loading<br>• route loading (`default_route()` is the first route in `farms/model_routes.json`) |

---

## 3. One agent run, step by step (`network.run`)

It's triggered every 6 hours by `server.py` (00:00, 06:00, 12:00 and 18:00, farm time), by "Get fresh advice" on the farmer board, by "Run now" on the Developer page, by `POST /run`, or by `python network.py`.

| # | Step | Code | Output |
|---|---|---|---|
| 1 | **Start** | `db.start_run` | A row in `runs` with status `running` |
| 2 | **Summarise (code)** | `summariser.summarise` | Per-field summaries for the last 24 h, 5-minute series for the code tools, and the Open-Meteo forecast. If there are no readings, the run fails with a clear error. |
| 3 | **Ranges in force** | `network.ranges_in_force` | The latest plan version's ranges, or the starting ranges |
| 4 | **Code tools** | `network.code_tools` → `agents.SKILLS[...]` | For each department and field: status, issues and each specialist's summary. The code tools check readings against the ranges **in force**, and use the hard limits as the "tolerated" band. |
| 5 | **Pick models** | `llm.load_routes`, `agents.base.resolve_model` | Each department's `llm` from `network.json`, mapped through the chosen route (OpenRouter by default). Anything not available falls back to the route's fallback. |
| 6 | **Stage 1 (CrewAI)** | `crew.run_departments` | Agri-Environment and Soil & Water, one task each, fixed-JSON `DepartmentReport` |
| 7 | **Stage 2 (CrewAI)** | `crew.run_departments` | Crop Science (it reads a code-written **digest** of stage 1), Data & Analytics, and the Market & Strategy / Crop Suggestion extra |
| 8 | **Crop advice (code)** | `crop_advice.suggest` | What to plant per bed; it goes to the Director for farms with no crop chosen |
| 9 | **Farm Director (CrewAI)** | `network.director_brief`, `crew.run_director` | `DirectorPlan`: range **changes** (each with a reason), a message for the farmer, and to-dos. The brief contains the ranges in force, the hard limits, the key facts, what the hardware can actually do, suggested to-dos computed by code, and each report's digest. |
| 10 | **Arabic** | `crew.translate` | `message_ar` and `todos_ar`. A reply containing Chinese or Korean script is retried once, then dropped rather than shown garbled. |
| 11 | **Code check** | `network.apply_changes`, `checker.check` | Changes are applied to the ranges in force. Then they're clamped to the hard limits, limited to one step per plan, or rejected, each with a flag. |
| 12 | **Save** | `db.save_plan`, `db.finish_run` | A new `plans` row: the next version number, valid from now + 5 minutes, everything behind the plan, and advice (market tips and crop suggestions). The master picks it up at its next `GET /ranges/version`. |

**If any step fails, nothing changes.** The run is marked `failed` with the error, and the master keeps the last version.

Lessons from testing that the code relies on:
- **CrewAI's hidden context.** In a sequential crew, CrewAI hands each task the previous task's output. Small models then copy it. Every task sets `context=[]`, and the next stage gets a digest instead.
- **Small models get ranges wrong.** They widen ranges to the hard limits, or get the watering direction backwards. The Director therefore lists only *changes*, its brief explains that raising the soil minimum means more water, and the checker rejects widened bands.
- **Rate limits.** Each task runs in its own kickoff, so a 429 retries one task on the next model, not the whole run.
- **Reasoning models.** Free OpenRouter models are often reasoning models. Given a small token budget, they spend all of it thinking and never write the JSON. Cloud calls therefore get `llm.CLOUD_MAX_TOKENS` (4,000) and `reasoning: {effort: low}`, and a length-limit error moves on to the next model.
- **Wild swings.** One bad answer once cut a pump run from 120 s to 10 s during a heatwave, inside the hard limits. The checker's step limit stops that.

---

## 4. The onboarding assistant (`onboarding.py`, "Set up a farm")

**The conversation.** `step()` is called once per farmer message.
- **Code picks the question.**
  - `missing()` lists the slots still unknown, in order: location → **stage** (a working farm, or just land?).
  - For a working farm: beds (count, size, type) → crop → planting date (or the goal, if the crop is undecided).
  - For bare land: **land size** → goal. The bed and fertilizer questions are skipped.
  - Then, for everyone: water → power → internet → **budget** → problems ("worries" for bare land).
  - Code parses "1 hectare", "100 by 50", "2 dunams", "150k", "150 ألف ريال".
  - The next question comes from the `QUESTIONS` bank: clear, with an example, in English or Arabic depending on how the farmer writes.
  - The model only writes a one-sentence acknowledgement, and structures the answer into the profile. It can never ask about something already known.
- **Null means "answered: none or don't know".**
  - A slot is `""` while unknown, a value once answered, and `null` when the farmer says none, no, not yet or don't know. That includes Arabic: لا، لا شيء، ما أعرف.
  - `null` counts as answered, so it's never asked again. `_set_null` gives sensible defaults: bed type "soil bed, open air", crop "undecided".
  - A `null` from the model is accepted only for the slot being asked. Models like to null every key they haven't heard about, which would skip those questions.
- **No loops.**
  - If the model didn't structure an answer, the farmer's own words are kept (`_keep_raw`). For example, water "from the river" or power "solar".
  - Each slot is asked at most `MAX_ASKS = 2` times. After that it becomes `null` and the conversation moves on. The counts are kept in the profile's `_asked` key.
- **Code does what small models get wrong** (`rule_fill`):
  - relative dates: "three weeks ago", "قبل أسبوعين";
  - bed counts: "two beds", "حوضين";
  - sizes: "10 by 20", "10 في 20";
  - open air or greenhouse, in English or Arabic;
  - crop names: خس → lettuce;
  - geocoding names: "Wakrah" → "Al Wakrah", الخور → Al Khor.
- **Tools:** Open-Meteo geocoding, the coming week's forecast and the past month's weather (`lookup`). The result is shown in the chat as a 🔎 line.

**The design** (`design()`) produces the plan's four outputs, plus crop suggestions:

| Output | How it's built |
|---|---|
| Farm profile | The structured conversation (without the `_`-prefixed bookkeeping keys) |
| Hardware plan | `hardware_plan()`: per bed, a sensor set (dht11, soil, light; a tank level sensor on F1 when the water comes from a tank; MQ-135 air quality only when covered) and an actuator set (drip pump; a fan when covered). It also places the master. |
| Hard limits and starting ranges | `limits_for()`: from the crop file (`knowledge.CROP_FILE`), or general values from the candidate crop's climate profile |
| Agent network | `network_for()` fills the department `TEMPLATE`:<br>• it keeps the roles the sensors can feed, and drops the rest with a reason (for example "Air quality: open-air beds, no sensor")<br>• it writes each CrewAI definition around the farm's crop, beds and problems<br>• it adds Crop Suggestion + Market as an advice-only extra |
| Crop suggestions | `crop_advice.suggest()` on the new profile |
| Farm plan & money | `farm_plan.plan()`: for bare land, its recommended zones become the farm's beds (`fields_from_plan`, one per ≤ 2,000 m², planted with the zone's crop combination). It includes the layout, crop combination, costs, profit, payback, and the budget check. It's saved as `plan.json`. |

The team reviews everything on the page. **Team approves** calls `farm.save_farm()`, which writes `farms/<id>/{profile,hardware,limits,network}.json`, makes it the active farm, and saves version 1 (the starting ranges) in `plans`.

---

## 5. Crop suggestion (`crop_advice.py`)

It's all code, so it works on day one, with no sensors and no model. For each of the 18 candidate crops in `knowledge.CANDIDATE_CROPS`, and each bed:

- **Season fit.** It checks every month of the crop's growing period, starting a week from today, against Qatar's monthly climate normals (Qatar Open Data, 2020–2024). Greenhouses are assumed about 6 °C cooler at midday. A crop is *ideal*, *tolerated* or *too hot/cold*. If it doesn't fit now, it finds the first month from which it does.
- **Money.** Yield (Qatar's 2025 open-field or greenhouse figure) × price (`farms/market_prices.json`, or Qatar's average farm-gate vegetable value, labelled "rough price guess") × the bed's area gives an estimate **per harvest from this bed**. It's gross revenue: costs aren't included, and the page says so.
- **Water and care.** Each crop has a water need (low, medium or high) and a care level. When the profile says water is limited (dry soil, a tank, salty water, and so on), thirsty crops are ranked lower.
- **Ranking.** A score combines money per month of growing, season fit, water and care. The page shows the top 3 to **plant now**, and a calendar of the best crop per coming month.

The CrewAI Crop Suggestion / Market department explains these numbers; it doesn't invent them.

---

## 5b. Farm design and finance (`farm_plan.py`)

`plan(profile)` prices three design options on the farm's land:

| Option | Growing area split |
|---|---|
| Starter | open field |
| Balanced | 75 % open field, 25 % shade-net house |
| Intensive | 60 % open field, 20 % shade-net house, 20 % cooled greenhouse |

For each option:
- **Growing area:** 70 % of bare land (the rest is paths and buildings), or all the beds of a working farm.
- **Beds:** one Hydro Monitor sensor set and actuator set per bed of up to 2,000 m².
- **Best crop combination** per zone and season (cool Oct–Apr, hot May–Sep):
  - a crop is kept only if its margin is positive (sales minus seed, water and fertilizer);
  - it's ranked by margin, with thirsty crops penalised when water is limited and official prices trusted more;
  - shares are 50 / 30 / 20 % (no crop over half a zone, so one bad price can't sink the farm);
  - where nothing pays (open field in summer), the zone rests and the soil is solarised.
- **Set-up cost (capex):**
  - structures (greenhouse, shade house);
  - drip and bed preparation;
  - pumps;
  - a water tank (about 3 days of the busier season);
  - a well (if there's no water), shed and fence (bare land only);
  - solar power with a battery (if there's no mains power);
  - the Hydro Monitor kit.
- **Running costs (opex):**
  - seeds, water and fertilizer;
  - workers (none under 1,000 m²: family labour);
  - electricity (none with solar);
  - packing and transport (a share of sales);
  - maintenance (a share of the set-up cost).
- **Result:** sales, profit, margin, payback years, and yearly water use.

**The recommended option** is the most profitable one that fits the budget; an option taking over 5 years to pay back loses to a quicker one. If nothing fits the budget, it searches for the largest part of the land a starter design can cover within the budget ("phase 1"). The shed, well and solar are fixed costs, so this is a search rather than a straight scaling. Warnings cover a loss, a payback over 5 years, and a field resting in summer.

For a working farm, `current` prices today's crop on the existing beds, so the Finance department can say what a better combination would add. The onboarding saves bare land's plan as `farms/<id>/plan.json`, with a 3-sentence explanation written by the onboarding model (`story`). Working farms get a fresh plan each time. All numbers are estimates: sales use Qatar Open Data yields and prices, and costs use `farms/costs.json`.

---

## 6. The dashboard (`app.py`)

| Page | For | Shows |
|---|---|---|
| 🌱 **My farm** (default) | The farmer | **Top of the page:** a banner, and key numbers (beds OK, profit per year, next harvest, water).<br>**Today tab:**<br>• bed cards (plain status, crop day and harvest, last watering)<br>• **Your AI farm team:** one card per department with its insight, in Arabic when translated<br>• **the big picture:** the Director's combined overview plus counts and the profit line<br>• to-dos with ✅ Done / ✏️ Correct<br>• "Get fresh advice" (hidden on bare land until sensors send data)<br>**Farm plan & money tab:** site plan drawing, set-up costs, crop-combination calendar, money in and out, design options against the budget.<br>**What to plant tab.** **Notes & questions tab.**<br>A 🏡 Farm selector and an Arabic / English switch sit in the sidebar. |
| 💬 **Set up a farm** | The farmer and the team | The onboarding chat (with the plan's example lines for rehearsals), then five review tabs: crop suggestions, profile, hardware, hard limits, agent network. Then **Team approves**. |
| 🛠️ **Developer** | The team and the judges | • live metrics and log rows<br>• the agent-network diagram<br>• Run now with live progress, and the run history<br>• the Director's plan: ranges before, Director and saved, with clamp flags and reasons<br>• every department report, and the summariser's JSON |

---

## 7. The database: `data/hydro.db` (SQLite)

It's created by `db.connect()` (schema in `db.SCHEMA`; older files get the `farm_id` columns added automatically). `python seed_demo.py --reset` empties every table (it works even while `server.py` or the dashboard has the file open) and reseeds Al Khor. The `data/` folder is in `.gitignore`.

### `log`: every reading and every actuator switch (plan 2.4)

One row per reading or per pump switch, sent by the master with `POST /log`.

| Column | Type | Meaning | Example |
|---|---|---|---|
| `id` | INTEGER PK | Row number | `25955` |
| `farm_id` | TEXT | Which farm the row belongs to. The master doesn't send it: `server.py` stamps the active farm. Every read filters by it, so two farms' F1 beds never mix. | `alkhor` |
| `field_id` | TEXT | Which field (bed) in the farm | `F1` |
| `module_id` | TEXT | Which sensor set or actuator set | `S1` (sensors), `A1` (actuators) |
| `device_id` | TEXT | Which sensor or relay on that module | `dht11`, `soil`, `light`, `level`, `mq135`, `relay1` |
| `type` | TEXT | `reading` or `action` | `reading` |
| `kind` | TEXT | What was measured or switched | `temp_air`, `humidity`, `soil_moisture`, `light`, `level`; `pump` or `fan` for actions |
| `value` | TEXT | The number, or `activated` / `deactivated` for actions | `30.6`, `activated` |
| `unit` | TEXT | `C`, `%`, `lux`, or empty for actions | `%` |
| `timestamp` | TEXT | When the master received it, ISO 8601 with the offset | `2026-09-25T19:25:00+03:00` |
| `ts` | REAL | The same moment as Unix seconds, for fast range queries (indexed with `field_id`) | `1790353500.0` |
| `received_at` | TEXT | When the laptop stored it | `2026-09-25T19:25:33+03:00` |

Pump runs are pairs of `activated` and `deactivated` rows. The summariser pairs them up to count runs and seconds.

### `plans`: every agent plan, versioned

One row per saved plan. The **latest version** is what `GET /ranges` serves. Version 1 is the starting ranges from onboarding. JSON columns are stored as text; `db.latest_plan()` parses them.

| Column | Type | Meaning |
|---|---|---|
| `version` | INTEGER PK | Plan number, increasing across all farms. The master compares it with the version on its SD card. |
| `farm_id` | TEXT | Which farm (`farms/<farm_id>/`) |
| `created_at` | TEXT | When the plan was saved |
| `valid_from` | TEXT | When the master should apply it (made at 06:00, valid from 06:05) |
| `ranges` | JSON | **The ranges file the master downloads**, already clamped: `{"version": 13, "valid_from": "…", "F1": {"soil_moisture": [30, 45], "temp_air": [24, 32], "humidity": [40, 80], "level": [20, 100], "pump_seconds": 120}, "F2": {…}}` |
| `proposed` | JSON | The Director's proposal (the ranges in force plus its changes) **before** the code check, per field. Compare it with `ranges` to see what the check changed. |
| `flags` | JSON | What the code check did: `[{"field": "F1", "setting": "pump_seconds", "type": "clamped" \| "limited" \| "rejected" \| "invalid", "proposed": 400, "used": 300, "why": "400 s is above the 300 s hard limit"}]`. **clamped:** past a hard limit. **limited:** too big a step for one plan. **rejected:** widened to the whole hard span. **invalid:** not a number, or min ≥ max. |
| `message_en` | TEXT | The Director's message to the farmer, in English |
| `message_ar` | TEXT | The same in Arabic, or NULL if no clean translation came back |
| `todos` | JSON | To-dos for the farmer, most urgent first: `["Put the shade cloth over the beds from 11:00 to 15:00", …]` |
| `todos_ar` | JSON | The to-dos in Arabic, in the same order |
| `reports` | JSON | Every department report: `{"agri_environment": {"summary", "summary_ar", "warnings", "todos", "ranges", "pump_seconds", "by", "name", "code", "status", "advice_only"}, …, "_changes": [the Director's changes with reasons]}`. `summary_ar` is the Arabic insight for the farmer board. |
| `summaries` | JSON | The summariser's per-field numbers the departments read (now/min/max/avg/trend, yesterday, pump runs, forecast, farmer note) |
| `advice` | JSON | `{"market": [market tips], "crops": crop_advice.suggest(...), "farm_plan": farm_plan.plan(...)}`. Plans saved before crop advice existed hold a plain list of market tips. |
| `made_by` | TEXT | The model that wrote the plan, e.g. `openrouter:qwen/qwen3.8-27b:free`, or `onboarding (starting ranges)` |
| `trigger` | TEXT | `schedule`, `button`, `cli` or `onboarding` |

### `runs`: every agent run, including failed ones

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Run number |
| `farm_id` | TEXT | Which farm |
| `trigger` | TEXT | `schedule`, `button` or `cli` |
| `started_at`, `finished_at` | TEXT | ISO timestamps |
| `status` | TEXT | `running`, `ok` or `failed` |
| `error` | TEXT | Why it failed (e.g. `RuntimeError: the Farm Director gave no usable plan (RateLimitError …)`), or NULL |
| `version` | INTEGER | The plan version it saved, or NULL if it failed |
| `detail` | JSON | `{"route": "openrouter", "models": {department → model}, "seconds": 180, "flags": 1, "previous_version": 1}` |

A failed run never touches `plans`, so the master keeps the last good version.

### `approvals`: the farmer's feedback (the future fine-tuning data, plan 3.5)

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `version` | INTEGER | Which plan version the feedback is about |
| `item` | TEXT | `message`, or `todo:1`, `todo:2`, … (1-based, in the plan's to-do order) |
| `decision` | TEXT | `approve` (👍 helpful), `done` (✅ the farmer did the to-do) or `correct` (✏️) |
| `correction` | TEXT | What the farmer says it should have said or done (for `correct`) |
| `created_at` | TEXT | When |

The dashboard shows the latest decision per item (`db.approvals()`). Over time, this table pairs what the agents advised with what the farmer accepted or corrected. That's the data for fine-tuning each specialist in the final product.

### `notes`: what the farmer sees

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `field_id` | TEXT | Which bed |
| `farm_id` | TEXT | Which farm |
| `text` | TEXT | E.g. `leaves on the west edge look pale` |
| `created_at` | TEXT | When |

The summariser attaches a bed's latest note from the last 3 days to that bed's summary. Crop Science and the Director read it, and code suggests a "send a photo" to-do when the note mentions pale, yellow, scorched or wilted leaves.

### Handy queries

```sql
-- the ranges file the master gets now
SELECT ranges FROM plans ORDER BY version DESC LIMIT 1;
-- what the code check changed in the last plan
SELECT flags FROM plans ORDER BY version DESC LIMIT 1;
-- pump switches today on F1
SELECT timestamp, value FROM log WHERE field_id='F1' AND type='action' AND ts > strftime('%s','now','start of day');
-- failed runs and why
SELECT id, started_at, error FROM runs WHERE status='failed' ORDER BY id DESC;
-- what farmers corrected
SELECT p.version, a.item, a.correction FROM approvals a JOIN plans p USING(version) WHERE a.decision='correct';
```

Also in `data/` is `master_sd_ranges.json`, the fake master's "SD card". The forecast cache lives in `farms/<id>/forecast_cache.json`.

---

## 8. Configuration files

| File | What to edit |
|---|---|
| `.env` | `OPENROUTER_API_KEY` (the default route), optional `TAVILY_API_KEY` (web price research), `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_DAILY_LIMIT` |
| `farms/model_routes.json` | Where each model runs. **The first route is the default** (OpenRouter). Each route has a `fallback`, a `map` (the spec's model → the real model) and an `onboarding` model. `llm.OPENROUTER_FALLBACKS` lists the models tried after a 429. |
| `farms/active.json` | Which farm the system runs |
| `farms/<id>/*.json` | The four onboarding outputs (see section 4). `network.json` is the CrewAI definitions: edit a department's `agent.backstory`, its `llm`, or its specialists there. |
| `farms/market_prices.json` | Crop prices in QR/kg, with source and confidence. **Put the farmer's real buyer prices here** (confidence `farmer`). |
| `farms/costs.json` | Set-up and running cost assumptions for farm design and finance (greenhouse per m², drip, tank, well, solar, workers, water, electricity…). **Replace them with local supplier quotes.** |
| `knowledge.py` | The crop file: ranges, pest rules, growth stages, hard limits, and the candidate crops for crop suggestion |

---

## 9. How to extend it

- **A new sensor:**
  1. Add its `kind` to `knowledge.KIND_FIELD` / `KIND_DEVICE` / `KIND_UNIT`.
  2. Add a device in the farm's `hardware.json`.
  3. Add a specialist that uses it (`range_monitor` works for any band) to a department in `network.json`. Specialists whose sensor is missing go dormant automatically (`farm.validate_network`).
- **A new specialist:** write a skill `def my_skill(agent, state) -> dict` in `agents/` that returns `finding(...)` or `done(...)`. Register it in `agents/__init__.py`, and list it under a department's `specialists`.
- **A new crop for suggestions:** add it to `knowledge.CANDIDATE_CROPS` (climate ranges, a Qatar yield, cycle days, water and care, an Arabic name). Optionally add a price to `farms/market_prices.json`.
- **A new language on the farmer board:** add a column to `farmer_view.T` and to `onboarding.QUESTIONS`.
- **Another model provider:** add it to `llm.PROVIDERS` (any OpenAI-compatible API), then use `"<provider>:<model>"` in a route.
