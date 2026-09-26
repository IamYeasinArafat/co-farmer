"""Ask the farm (an extra beyond the plan): answers the farmer's questions from the latest agent plan.

The answer rests on what the network already knows: the summariser's numbers, the department reports, the
ranges in force, the Director's message and to-dos, and the market advice. Price and market questions also
trigger a web search when a TAVILY_API_KEY is set.
"""

from __future__ import annotations

import json
import re
from typing import Any

import llm
from agents.base import resolve_model
from agents.strategy import research

SYSTEM = (
    "You are the farm assistant for a small farm in Qatar. A network of specialist agents has already analysed the farm; "
    "their findings are given to you. Answer the farmer's question in plain language, in at most 5 sentences. Use only the "
    "facts provided: quote numbers exactly, say how reliable a price is (official, estimate, proxy, farmer or unverified web), "
    "and never call gross revenue profit. If the facts don't answer the question, say what data is missing. Answer in the "
    "language of the question (Arabic or English)."
)
MARKET_WORDS = re.compile(r"price|market|sell|buy|profit|revenue|cost|qr|riyal|demand|crop|grow|plant|سعر|سوق", re.I)


def build_context(farm: dict[str, Any], plan: dict[str, Any] | None) -> str:
    profile = farm["profile"]
    lines = [f"Farm: {profile['name']}, {profile['location']}; {profile['crop']}; fields "
             + ", ".join(f"{f['field_id']} ({f.get('type')}, planted {f.get('planted')})" for f in profile["fields"]) + "."]
    if not plan:
        return "\n".join(lines + ["No agent plan yet."])
    lines.append(f"Ranges in force (version {plan['version']}, valid from {plan['valid_from']}): "
                 + json.dumps({k: v for k, v in plan["ranges"].items() if k not in ("version", "valid_from")}))
    for field, s in (plan.get("summaries") or {}).items():
        lines.append(f"Summary {field} (code): {json.dumps(s, ensure_ascii=False)}")
    for key, r in (plan.get("reports") or {}).items():
        if key.startswith("_"):
            continue
        lines.append(f"{r.get('name')} report: {r['summary']} Warnings: {'; '.join(r.get('warnings', []))}")
    lines.append(f"Director's message: {plan.get('message_en')}")
    lines.append("To-dos: " + "; ".join(plan.get("todos") or []))
    from farmer_view import market_tips

    for a in market_tips(plan):
        lines.append(f"Market advice: {a['title']}: {a['detail']} (price confidence {a['confidence']})")
    advice = plan.get("advice") if isinstance(plan.get("advice"), dict) else {}
    if advice.get("farm_plan"):
        import farm_plan

        lines.append("Farm plan and money (code): " + farm_plan.summary_text(advice["farm_plan"]).replace("\n", " "))
    if advice.get("crops"):
        import crop_advice

        lines.append("What to plant (code): " + crop_advice.summary_text(advice["crops"]).replace("\n", " "))
    return "\n".join(lines)


def answer(question: str, farm: dict[str, Any], plan: dict[str, Any] | None, route: dict[str, Any]) -> dict[str, Any]:
    """Returns {text, by, sources}."""
    context = build_context(farm, plan)
    sources: list[str] = []
    if MARKET_WORDS.search(question):
        for note in research([f"{question} Qatar"]):
            context += f"\nWeb search (unverified) for '{note['query']}': {note['answer']}"
            sources += [s["url"] for s in note["sources"][:3]]
    state = {"installed_models": llm.available_models(), "fallback_model": route["fallback"], "model_map": route["map"]}
    model = resolve_model(farm["network"]["director"]["agent"]["llm"], state)
    text = llm.chat(model, SYSTEM, f"FACTS\n{context}\n\nFARMER'S QUESTION\n{question}", temperature=0.3)
    if text:
        return {"text": text, "by": model, "sources": sources}
    return {"text": "The assistant is offline (no local model or cloud key). "
                    + (f"Latest advice: {plan['message_en']}" if plan else "No plan yet."), "by": "rules", "sources": []}
