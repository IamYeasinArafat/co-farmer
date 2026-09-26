"""The farm agent network as CrewAI crews (Hydro Monitor plan 3.3-3.4).

One CrewAI agent per department, built from its definition in network.json (role, goal, backstory, llm).
Every task replies in a fixed JSON format (a pydantic model CrewAI enforces). network.py runs three stages,
each a sequential CrewAI crew, and writes a short digest of the earlier reports into the next stage's tasks
(small models copy raw reports they get as context, so they get the digest instead):

    stage 1  Agri-Environment, Soil & Water
    stage 2  Crop Science (reads the stage-1 digest), Data & Analytics, Market & Strategy (extra)
    stage 3  Farm Director (reads every report's digest), then the Arabic translation
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from pydantic import BaseModel, Field

import llm

os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")   # nothing leaves the laptop except model calls
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
CLOUD_TIMEOUT_S = 90

from crewai import LLM, Agent, Crew, Process, Task   # noqa: E402  (after the telemetry switches)

log = logging.getLogger("crew")


# ── the fixed JSON formats ───────────────────────────────────────────────
class RangeProposal(BaseModel):
    field_id: str = Field(description="F1, F2, ...")
    kind: str = Field(description="soil_moisture, temp_air, humidity or level")
    min: float
    max: float
    reason: str


class PumpProposal(BaseModel):
    field_id: str
    seconds: int
    reason: str


class DepartmentReport(BaseModel):
    summary: str = Field(description="2-3 sentences with the key numbers")
    warnings: list[str] = Field(default_factory=list, description="risks, most serious first, at most 3")
    todos: list[str] = Field(default_factory=list, description="things a person must do, at most 3")
    ranges: list[RangeProposal] = Field(default_factory=list, description="only settings this department may change; empty if no change")
    pump_seconds: list[PumpProposal] = Field(default_factory=list, description="only if this department may change pump_seconds")


class Change(BaseModel):
    field_id: str
    setting: str = Field(description="soil_moisture, temp_air, humidity, level or pump_seconds")
    min: float | None = Field(default=None, description="new minimum (ranges only)")
    max: float | None = Field(default=None, description="new maximum (ranges only)")
    seconds: int | None = Field(default=None, description="new pump run (pump_seconds only)")
    reason: str


class DirectorPlan(BaseModel):
    changes: list[Change] = Field(default_factory=list, description="only the settings that should change; empty keeps every range")
    message: str = Field(description="to the farmer, in English: at most 4 short, plain sentences")
    todos: list[str] = Field(description="concrete tasks for the farmer, most urgent first, at most 5")


class Translation(BaseModel):
    message_ar: str
    todos_ar: list[str]
    extras_ar: list[str] = Field(default_factory=list, description="the department insights in Arabic, same order")


REPORT_RULES = (
    "Rules: use only the numbers given here and never invent readings. Propose a change only when the data gives a "
    "reason, and only inside the hard limits. Mention only sensors that exist on these beds."
)


def crew_llm(model: str, temperature: float = 0.2):
    """Our model names (llm.py) -> a CrewAI LLM. Every provider, Ollama included, speaks the OpenAI API."""
    provider, name = llm.split(model)
    if provider == "ollama":
        return LLM(model=f"openai/{name}", base_url=f"{OLLAMA_URL}/v1", api_key="ollama", temperature=temperature, max_tokens=900)
    cfg = llm.PROVIDERS[provider]
    key = os.environ.get(cfg["key_env"] or "", "ollama")
    extra = {}
    if provider == "openrouter":
        # 1. free OpenRouter models are often reasoning models: without a low effort they can spend the whole token
        #    budget thinking and never write the JSON (LengthFinishReasonError)
        # 2. "models": OpenRouter itself fails over to the next free model inside the same request when one is
        #    rate-limited upstream (429) or overloaded (503), exactly like llm.py does for plain calls
        backups = [m for m in llm.OPENROUTER_FALLBACKS if m != name]
        extra["extra_body"] = {"reasoning": {"effort": "low"}, "models": [name, *backups][:3]}
    # timeout: a free model stuck in a queue should fail fast so the next candidate (or local Ollama) takes over
    return LLM(model=f"openai/{name}", base_url=cfg["base_url"], api_key=key, temperature=temperature,
               max_tokens=llm.CLOUD_MAX_TOKENS, timeout=CLOUD_TIMEOUT_S, max_retries=1, **extra)


def _agent(definition: dict[str, Any], model: str, temperature: float = 0.2) -> Agent:
    return Agent(role=definition["role"], goal=definition["goal"], backstory=definition["backstory"],
                 llm=crew_llm(model, temperature), allow_delegation=False, max_iter=2, verbose=False)


def _safe(text: str) -> str:
    return text.replace("{", "(").replace("}", ")")   # CrewAI treats {name} in task text as a template slot


RETRYABLE = ("429", "rate limit", "ratelimit", "502", "503", "504", "overloaded", "temporarily", "timeout",
             "length limit", "lengthfinishreason",     # a model that ran out of tokens: try the next one
             "upstream error", "no choices", "connection")


def _kickoff(tasks: list[Task]) -> str | None:
    try:
        Crew(agents=[t.agent for t in tasks], tasks=tasks, process=Process.sequential, verbose=False).kickoff()
        return None
    except Exception as err:   # keep whatever finished; the caller decides what a missing result means
        log.warning("crew stopped: %s", err)
        return f"{type(err).__name__}: {err}"


def _candidates(model: str) -> list[str]:
    """What to try, in order: the model (OpenRouter already fails over between its free models inside each
    request), the model once more after a pause (free pools recover in seconds), then a local Ollama model if one
    is installed, so a demo never dies because every free cloud model is busy."""
    provider, _ = llm.split(model)
    if provider == "ollama":
        return [model]
    out = [model, model]
    local = llm.available_models()
    best = next((m for m in ("qwen2.5:7b", "qwen2.5:3b") if m in local), local[0] if local else None)
    if best:
        out.append(best)
    return out


def _run_one(make_task, model: str) -> tuple[Task | None, str | None]:
    """Build and run one task; on a rate limit or an overloaded provider, back off and try the next fallback model."""
    error = None
    for attempt, candidate in enumerate(_candidates(model)):
        provider, _ = llm.split(candidate)
        if provider != "ollama" and not llm.budget.take(provider):
            return None, f"{provider} daily request budget used up"
        task = make_task(candidate)
        error = _kickoff([task])
        if error is None and task.output is not None:
            return task, None
        if not error or not any(k in error.lower() for k in RETRYABLE):
            return task, error or "no output"
        wait = 8 * (attempt + 1)
        log.warning("%s busy (%s); trying again in %ss", candidate, error[:60], wait)
        time.sleep(wait)
    return None, error


def _parsed(task: Task | None) -> dict[str, Any] | None:
    out = task.output if task else None
    return out.pydantic.model_dump() if out is not None and out.pydantic is not None else None


def run_departments(departments: list[dict[str, Any]], models: dict[str, str]) -> tuple[dict[str, Any], str | None]:
    """departments: [{id, agent (definition), brief (task text), sets}]. One task each, run one after another."""
    reports, errors = {}, []
    for dept in departments:
        sets = dept["sets"]
        expected = ("JSON department report. " + (f"You may change only: {', '.join(sets)}." if sets else
                                                  "You change no settings: leave 'ranges' and 'pump_seconds' empty."))

        # context=[]: in a sequential crew CrewAI otherwise hands every task the previous task's output, and small
        # models then copy that report instead of writing their own
        def make(model: str, dept=dept, expected=expected) -> Task:
            return Task(description=_safe(dept["brief"] + "\n\n" + REPORT_RULES), expected_output=expected,
                        agent=_agent(dept["agent"], model), output_pydantic=DepartmentReport, context=[])

        task, error = _run_one(make, models[dept["id"]])
        reports[dept["id"]] = _parsed(task)
        if error:
            errors.append(f"{dept['name']}: {error}")
    return reports, "; ".join(errors) or None


def run_director(director: dict[str, Any], model: str, brief: str) -> tuple[dict[str, Any] | None, str | None]:
    def make(m: str) -> Task:
        return Task(description=_safe(brief), expected_output="JSON plan: changes, message, todos.",
                    agent=_agent(director["agent"], m), output_pydantic=DirectorPlan, context=[])

    task, error = _run_one(make, model)
    return _parsed(task), error


GLOSSARY = ("shade cloth = شبك التظليل; drip line = خط التنقيط; drip emitters = نقاطات الري; bed = حوض; "
            "soil moisture = رطوبة التربة; tank = الخزان; pump = المضخة; fertilizer = السماد; harvest = الحصاد; "
            "seedlings = الشتلات; pale leaves = أوراق باهتة")


ALLOWED_LATIN = {"QR", "kW", "kWh", "NPK", "Hydro", "Monitor", "EC", "pH"}


def _clean_arabic(text: str) -> bool:
    """Models sometimes slip Chinese characters, or untranslated English words ('سيدeploy'), into the Arabic."""
    if any("぀" <= ch <= "鿿" or "가" <= ch <= "힯" for ch in text):
        return False
    return not [w for w in re.findall(r"[A-Za-z]{3,}", text) if w not in ALLOWED_LATIN]


def translate(message: str, todos: list[str], model: str, extras: list[str] | None = None,
              attempts: int = 2) -> dict[str, Any] | None:
    """The farmer's message, to-dos and (optionally) the department insights in Modern Standard Arabic, or None if no
    clean translation came back."""
    source = "Message: " + message + "\nTo-dos:\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(todos))
    if extras:
        source += "\nDepartment insights (translate each; put them in extras_ar in the same order):\n" + "\n".join(
            f"{i + 1}. {t}" for i, t in enumerate(extras))
    for attempt in range(attempts):
        def make(m: str, attempt=attempt) -> Task:
            return Task(
                description=_safe("Translate this farm advice into simple Modern Standard Arabic for the farmer. Write Arabic "
                                  "script only. Keep every number, unit, time and field name (F1, F2) exactly as written.\n"
                                  "Use these farming terms: " + GLOSSARY + "\n\n" + source),
                expected_output="JSON with message_ar, todos_ar (same order as the to-dos) and extras_ar (same order as the insights).",
                agent=Agent(role="Translator for the farm messages", goal="Faithful, simple Arabic the farmer understands",
                            backstory="You translate farm advice from English into clear Modern Standard Arabic.",
                            llm=crew_llm(m, 0.1 if attempt == 0 else 0.0), allow_delegation=False, max_iter=2, verbose=False),
                output_pydantic=Translation, context=[])

        task, _ = _run_one(make, model)
        out = _parsed(task)
        if out and _clean_arabic(out["message_ar"] + "".join(out["todos_ar"]) + "".join(out.get("extras_ar") or [])):
            return out
    return None
