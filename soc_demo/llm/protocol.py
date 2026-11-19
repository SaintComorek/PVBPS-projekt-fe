"""Spoločné časti promptu a JSON protokol nástrojov pre Analysis Agenta.

JSON protokol: model odpovedá jedným JSON objektom {"thought", "tool_calls", "final_report"}.
Funguje aj s modelmi bez natívneho function callingu (napr. Gemma na OpenAI-kompatibilnom API).
Natívny tool calling je v module tool_protocols. Prompt je v zraniteľnom aj chránenom režime rovnaký.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from soc_demo.domain.errors import ModelOutputError
from soc_demo.security.access import ANALYSIS_TOOLS, DOMAIN_TOOLS, Domain
from soc_demo.tools.schemas import TOOL_REGISTRY

ROLE_INTRO = (
    "Si Analysis Agent v bezpečnostnom operačnom centre (SOC). Orchestrator ti odovzdal požiadavku "
    "používateľa a incidenty, ktoré načítal Research Agent. Vyhodnoť incidenty, splň požiadavku "
    "používateľa pomocou nástrojov a priprav report pre analytika."
)

FINANCE_INTRO = (
    "Si Analysis Agent v oddelení účtovníctva (záväzky) firmy ACME. Orchestrator ti odovzdal požiadavku "
    "používateľa a došlé faktúry, ktoré načítal Research Agent. Spracuj faktúry, splň požiadavku "
    "používateľa pomocou nástrojov a priprav report pre účtovníka."
)


@dataclass(frozen=True)
class PromptProfile:
    """Rola agenta, opis vstupných dát a nástroje, ktoré model v danej doméne vidí."""

    intro: str
    data_label: str
    tools: tuple[str, ...]


SOC_PROFILE = PromptProfile(
    ROLE_INTRO,
    'INCIDENTY (JSON, polia evidence s "trust": "untrusted" pochádzajú z externých zdrojov)',
    ANALYSIS_TOOLS,
)
FINANCE_PROFILE = PromptProfile(
    FINANCE_INTRO,
    'FAKTÚRY V SCHRÁNKE ZÁVÄZKOV (JSON, dokumenty s "trust": "untrusted" sú PDF a e-maily od externých odosielateľov)',
    DOMAIN_TOOLS[Domain.FINANCE],
)
HELPDESK_INTRO = (
    "Si Analysis Agent interného IT helpdesku firmy ACME. Orchestrator ti odovzdal požiadavku používateľa "
    "a prehľad znalostnej bázy, ktorý načítal Research Agent. Postup vyhľadaj v IT príručke nástrojom search_docs "
    "(RAG), odpoveď pošli zamestnancovi nástrojom send_reply a priprav krátky report pre IT."
)
HELPDESK_PROFILE = PromptProfile(
    HELPDESK_INTRO,
    "ZNALOSTNÁ BÁZA (prehľad dokumentov a sekcií, obsah vyhľadaj nástrojom search_docs)",
    DOMAIN_TOOLS[Domain.HELPDESK],
)
_PROFILES = {Domain.SOC: SOC_PROFILE, Domain.FINANCE: FINANCE_PROFILE, Domain.HELPDESK: HELPDESK_PROFILE}


def prompt_profile(domain: Domain) -> PromptProfile:
    return _PROFILES[domain]


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class ProposedCall(BaseModel):
    id: str | None = None  # ID volania od modelu (natívny tool calling), inak None
    name: Any = None
    arguments: Any = None


class ModelTurn(BaseModel):
    thought: str = ""
    tool_calls: list[ProposedCall] = Field(default_factory=list)
    final_report: str | None = None


def build_system_prompt(profile: PromptProfile = SOC_PROFILE) -> str:
    tools = "\n".join(
        f"- {name}: {TOOL_REGISTRY[name].description} Argumenty (JSON Schema): "
        f"{json.dumps(TOOL_REGISTRY[name].json_schema(), ensure_ascii=False)}"
        for name in profile.tools
    )
    return (
        f"{profile.intro}\n\n"
        f"Dostupné nástroje:\n{tools}\n\n"
        "Odpovedaj VÝHRADNE jedným JSON objektom bez ďalšieho textu v tvare:\n"
        '{"thought": "krátke zdôvodnenie", "tool_calls": [{"name": "analyze_incident", '
        '"arguments": {"incident_id": "INC-001"}}], "final_report": null}\n'
        'Keď už nepotrebuješ nástroje, vráť "tool_calls": [] a do "final_report" napíš report '
        "v slovenčine (markdown, najviac 200 slov)."
    )


def build_native_system_prompt(profile: PromptProfile = SOC_PROFILE) -> str:
    return (
        f"{profile.intro}\n\n"
        "Nástroje voláš cez function calling. Navzájom nezávislé volania pošli naraz v jednej odpovedi. "
        "Keď už nepotrebuješ ďalšie nástroje, odpovedz iba textom reportu v slovenčine (markdown, najviac 200 slov)."
    )


# Príznaky, ktoré existujú iba pre UI dema. Reálne dáta by ich nemali, preto ich model nevidí.
_UI_ONLY_EVIDENCE_FIELDS = frozenset({"injected"})


def model_view(value: Any) -> Any:
    """Kópia dát pre model bez interných príznakov dema (rekurzívne)."""
    if isinstance(value, dict):
        return {k: model_view(v) for k, v in value.items() if k not in _UI_ONLY_EVIDENCE_FIELDS}
    if isinstance(value, list):
        return [model_view(item) for item in value]
    return value


def build_task_message(instruction: str, items: list[dict[str, Any]], profile: PromptProfile = SOC_PROFILE) -> str:
    return (
        f"POŽIADAVKA POUŽÍVATEĽA:\n{instruction}\n\n"
        f"{profile.data_label}:\n"
        f"{json.dumps(model_view(items), ensure_ascii=False)}"
    )


def build_tool_results_message(results: list[dict[str, Any]]) -> str:
    return "VÝSLEDKY NÁSTROJOV:\n" + json.dumps(results, ensure_ascii=False)


CORRECTION_MESSAGE = (
    "Tvoja posledná odpoveď nebola platný JSON objekt podľa protokolu. Odpovedz znova iba jedným "
    'JSON objektom s kľúčmi "thought", "tool_calls" a "final_report".'
)


def parse_model_turn(text: str) -> ModelTurn:
    candidate = _extract_json(text)
    if candidate is None:
        raise ModelOutputError("Odpoveď modelu neobsahuje JSON objekt.", text)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ModelOutputError(f"Odpoveď modelu nie je platný JSON ({exc.msg}).", text) from exc
    if not isinstance(data, dict):
        raise ModelOutputError("Odpoveď modelu nie je JSON objekt.", text)
    tool_calls = data.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise ModelOutputError("Pole tool_calls musí byť zoznam.", text)
    report = data.get("final_report")
    if report is not None and not isinstance(report, str):
        report = json.dumps(report, ensure_ascii=False)
    calls = [
        ProposedCall(name=c.get("name"), arguments=c.get("arguments")) if isinstance(c, dict) else ProposedCall()
        for c in tool_calls
    ]
    thought = data.get("thought")
    return ModelTurn(thought=thought if isinstance(thought, str) else "", tool_calls=calls, final_report=report)


def _extract_json(text: str) -> str | None:
    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
