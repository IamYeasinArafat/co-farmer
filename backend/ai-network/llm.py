"""LLM access for every agent: local Ollama or any OpenAI-compatible cloud router.

A model name picks its provider:
    "qwen2.5:3b"                                  -> local Ollama
    "openrouter:qwen/qwen3.8-27b:free"            -> OpenRouter   (key in OPENROUTER_API_KEY)
    "groq:llama-3.1-8b-instant"                   -> Groq         (key in GROQ_API_KEY)
    "gemini:gemini-2.5-flash"                     -> Google AI    (key in GEMINI_API_KEY)
    "ollama-api:qwen2.5:3b"                       -> local Ollama through its OpenAI endpoint (for testing)

Cloud calls are rate-limited per provider (per minute and per day) so a free tier is never
exhausted by accident. Every call degrades to None, and the agents then keep their rule-based text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from typing import Any

TIMEOUT_S = 180
log = logging.getLogger("llm")
ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str = os.path.join(ROOT, ".env")) -> None:
    """KEY=value lines from arm/.env, so API keys never go in the JSON configs. Real env vars win."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# per_minute / per_day are safety caps, not quotas: set them at or below your plan's real limits
# (override the daily cap with the *_DAILY_LIMIT env var). Only OpenRouter's numbers were checked.
PROVIDERS: dict[str, dict[str, Any]] = {
    # free models: 20 req/min; 50 req/day, or 1,000 req/day after buying $10 of credits (OpenRouter docs, Sep 2026)
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
                   "per_minute": 20, "per_day": 50, "day_env": "OPENROUTER_DAILY_LIMIT"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "per_minute": 30, "per_day": 1000, "day_env": "GROQ_DAILY_LIMIT"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY",
               "per_minute": 10, "per_day": 250, "day_env": "GEMINI_DAILY_LIMIT"},
    "ollama-api": {"base_url": "http://localhost:11434/v1", "key_env": None,
                   "per_minute": 1000, "per_day": 100000, "day_env": None},
}

CLOUD_MAX_TOKENS = 4000   # room for reasoning models to think and still answer (they bill output tokens only)

OPENROUTER_FALLBACKS = [
    "qwen/qwen3.8-27b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "dots-studio/dots-3-note-preview:free",
]


class _Budget:
    """Per-provider request limiter: spaces calls to the per-minute limit and stops at the daily cap."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last: dict[str, float] = {}
        self.day: dict[str, tuple[date, int]] = {}

    @staticmethod
    def cap(provider: str) -> int:
        cfg = PROVIDERS[provider]
        return int(os.environ.get(cfg["day_env"] or "", cfg["per_day"]))

    def take(self, provider: str) -> bool:
        with self.lock:
            today, used = self.day.get(provider, (date.today(), 0))
            if today != date.today():
                today, used = date.today(), 0
            if used >= self.cap(provider):
                return False
            wait = 60 / PROVIDERS[provider]["per_minute"] - (time.monotonic() - self.last.get(provider, -1e9))
            if wait > 0:
                time.sleep(wait)
            self.last[provider] = time.monotonic()
            self.day[provider] = (today, used + 1)
            return True

    def usage(self) -> dict[str, str]:
        return {p: f"{used}/{self.cap(p)} today" for p, (_, used) in self.day.items()}


budget = _Budget()


def local_name(model: str) -> str:
    """CrewAI-style 'ollama/gemma2:2b' (as in the network definitions) -> the Ollama name 'gemma2:2b'."""
    return model[len("ollama/"):] if model.startswith("ollama/") else model


def split(model: str) -> tuple[str, str]:
    """'openrouter:qwen/qwen3.8-27b:free' -> ('openrouter', 'qwen/qwen3.8-27b:free'); plain or 'ollama/' names -> ('ollama', name)."""
    model = local_name(model)
    prefix, _, rest = model.partition(":")
    return (prefix, rest) if prefix in PROVIDERS and rest else ("ollama", model)


def provider_ready(model: str) -> bool:
    """True for a cloud model whose API key is set (local models are checked against the installed list)."""
    provider, _ = split(model)
    if provider == "ollama":
        return False
    key_env = PROVIDERS[provider]["key_env"]
    return key_env is None or bool(os.environ.get(key_env))


def _extract_json(text: str) -> str:
    """Models without a JSON mode often wrap the object in prose or ``` fences."""
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0) if match else text


def _cloud_chat(provider: str, model: str, messages: list[dict], as_json: bool, temperature: float) -> str | None:
    cfg = PROVIDERS[provider]
    if cfg["key_env"] and not os.environ.get(cfg["key_env"]):
        log.warning("%s: %s is not set; keeping rule-based text", provider, cfg["key_env"])
        return None
    if not budget.take(provider):
        log.warning("%s daily request budget used up; keeping rule-based text", provider)
        return None
    candidates = [model]
    if provider == "openrouter":
        candidates = [model, *[candidate for candidate in OPENROUTER_FALLBACKS if candidate != model]]
        candidates = candidates[:3]
    body: dict[str, Any] = {"messages": messages, "temperature": temperature, "max_tokens": CLOUD_MAX_TOKENS}
    if provider == "openrouter":
        body["models"] = candidates
        body["reasoning"] = {"effort": "low"}   # free models are often reasoning models: keep the thinking short
    else:
        body["model"] = candidates[0]
    if as_json:
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if cfg["key_env"]:
        headers["Authorization"] = f"Bearer {os.environ[cfg['key_env']]}"
    if provider == "openrouter":
        headers["X-Title"] = "QU-ARS amaranth agent network"
    for attempt in range(3):
        for json_attempt in (1, 2):
            request = urllib.request.Request(f"{cfg['base_url']}/chat/completions", json.dumps(body).encode(), headers)
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                    data = json.loads(response.read())
                text = (data["choices"][0]["message"].get("content") or "").strip()
                return _extract_json(text) if as_json else text
            except urllib.error.HTTPError as err:
                detail = err.read()[:200].decode(errors="replace")
                if err.code == 400 and "response_format" in body and json_attempt == 1:
                    body.pop("response_format")  # some models do not support JSON mode
                    continue
                retryable = provider == "openrouter" and err.code in (429, 500, 502, 503, 504)
                if retryable and attempt < 2:
                    delay = 2 ** (attempt + 1)
                    log.warning("%s fallback chain -> HTTP %s; retrying in %ss", provider, err.code, delay)
                    time.sleep(delay)
                    break
                log.warning("%s -> HTTP %s %s", provider, err.code, detail)
                return None
            except Exception as err:
                log.warning("%s request failed: %s", provider, err)
                return None
    return None


def chat(model: str, system: str, prompt: str, as_json: bool = False, temperature: float = 0.2) -> str | None:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    provider, name = split(model)
    if provider != "ollama":
        return _cloud_chat(provider, name, messages, as_json, temperature)
    try:
        import ollama

        response = ollama.Client(timeout=TIMEOUT_S).chat(
            model=name, messages=messages, format="json" if as_json else "", options={"temperature": temperature},
        )
        return response["message"]["content"].strip()
    except Exception:
        return None


def chat_json(model: str, system: str, prompt: str, temperature: float = 0.2) -> dict[str, Any] | None:
    text = chat(model, system, prompt, as_json=True, temperature=temperature)
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def load_routes(path: str = os.path.join(ROOT, "farms", "model_routes.json")) -> dict[str, dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def default_route() -> str:
    """The first route in farms/model_routes.json is the default (OpenRouter)."""
    return next(iter(load_routes()))


def available_models() -> list[str]:
    """Installed Ollama models (cloud models are checked with provider_ready)."""
    try:
        import ollama

        return [m.model for m in ollama.list().models]
    except Exception:
        return []
