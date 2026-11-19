"""Ako sa volania nástrojov prenášajú medzi agentom a modelom.

- `native-tools`: natívny tool calling (OpenAI `tools`, `tool_calls`, správy s rolou `tool`).
- `json-tools`: model píše volania do JSON objektu v texte (pre modely bez function callingu).
- `langchain`: LangChain `ChatOpenAI` s nástrojmi `@tool` (modul `soc_demo.llm.langchain_backend`).

Vo všetkých prípadoch je výstupom ten istý `ModelTurn` a nástroje volá LangChain cez tie isté tools,
takže brána, executor a audit sú rovnaké.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from soc_demo.llm.client import ChatResult
from soc_demo.llm.protocol import (
    CORRECTION_MESSAGE,
    SOC_PROFILE,
    ModelTurn,
    PromptProfile,
    ProposedCall,
    build_native_system_prompt,
    build_system_prompt,
    build_tool_results_message,
    parse_model_turn,
)
from soc_demo.security.access import ANALYSIS_TOOLS
from soc_demo.tools.schemas import TOOL_REGISTRY

FINAL_REPORT_REQUEST = (
    "Ďalšie volania nástrojov už nič nezmenia. Nevolaj žiadne nástroje a napíš záverečný report "
    "pre analytika podľa doterajších výsledkov."
)


@dataclass(frozen=True)
class ToolResult:
    """Výsledok jedného navrhnutého volania, ktorý sa vracia modelu."""

    model_call_id: str | None
    payload: dict[str, Any]
    message: Any = None  # ToolMessage z LangChain, ak nástroj zavolal LangChain (nie pri zamietnutej schéme)


class ConversationProtocol(Protocol):
    """Časť protokolu, ktorú používa Analysis Agent: prompt, výzva na report a odpovede nástrojov."""

    id: str

    def system_prompt(self) -> str: ...

    def final_report_request(self) -> str: ...

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]: ...


class ToolProtocol(ConversationProtocol, Protocol):
    """Protokol nad vlastným HTTP klientom: navyše tvar požiadavky a čítanie odpovede servera."""

    correction_message: str | None

    def request(self, messages: list[dict[str, Any]], *, final: bool = False) -> tuple[list[dict[str, Any]], dict]:
        """Správy a parametre požiadavky. Pri záverečnom kroku sa nástroje nedajú volať."""
        ...

    def parse(self, result: ChatResult) -> tuple[ModelTurn, str, dict[str, Any]]:
        """Vráti ťah modelu, text na zobrazenie v UI a správu asistenta do histórie."""
        ...


class JsonToolProtocol:
    id = "json-tools"
    correction_message: str | None = CORRECTION_MESSAGE

    def __init__(self, profile: PromptProfile = SOC_PROFILE) -> None:
        self._profile = profile

    def system_prompt(self) -> str:
        return build_system_prompt(self._profile)

    def request(self, messages: list[dict[str, Any]], *, final: bool = False) -> tuple[list[dict[str, Any]], dict]:
        return messages, {}

    def final_report_request(self) -> str:
        return f'{FINAL_REPORT_REQUEST} Vráť "tool_calls": [] a report daj do "final_report".'

    def parse(self, result: ChatResult) -> tuple[ModelTurn, str, dict[str, Any]]:
        turn = parse_model_turn(result.content)
        return turn, result.content, {"role": "assistant", "content": result.content}

    def tool_result_messages(self, results: list[ToolResult]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": build_tool_results_message([r.payload for r in results])}]


def native_tool_definitions(tools: tuple[str, ...] = ANALYSIS_TOOLS) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": TOOL_REGISTRY[name].description,
                "parameters": TOOL_REGISTRY[name].json_schema(),
            },
        }
        for name in tools
    ]


def _decode_arguments(raw: object) -> object:
    """Argumenty prichádzajú ako JSON reťazec. Neplatný JSON nechá tak, zamietne ho validácia schémy."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return raw
    return raw


def _raw_arguments(raw: dict[str, Any]) -> str:
    """Argumenty presne v tvare, v akom ich model poslal (do histórie konverzácie)."""
    function = raw.get("function")
    arguments = function.get("arguments") if isinstance(function, dict) else None
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments or {}, ensure_ascii=False)


class NativeToolProtocol:
    id = "native-tools"
    correction_message: str | None = None

    def __init__(self, profile: PromptProfile = SOC_PROFILE) -> None:
        self._profile = profile

    def system_prompt(self) -> str:
        return build_native_system_prompt(self._profile)

    def request(self, messages: list[dict[str, Any]], *, final: bool = False) -> tuple[list[dict[str, Any]], dict]:
        if not final:
            return messages, {"tools": native_tool_definitions(self._profile.tools), "tool_choice": "auto"}
        # Niektoré modely volajú nástroj aj pri tool_choice "none". Záverečná požiadavka preto nástroje
        # vôbec neposiela a históriu volaní prepíše na obyčajný text.
        return [_flatten(message) for message in messages], {}

    def final_report_request(self) -> str:
        return FINAL_REPORT_REQUEST

    def parse(self, result: ChatResult) -> tuple[ModelTurn, str, dict[str, Any]]:
        calls = []
        for index, raw in enumerate(result.tool_calls, 1):
            function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            calls.append(
                ProposedCall(
                    id=str(raw.get("id") or f"call_{index}"),
                    name=function.get("name"),
                    arguments=_decode_arguments(function.get("arguments")),
                )
            )
        content = result.content.strip()
        turn = ModelTurn(
            thought=content if calls else "",
            tool_calls=calls,
            final_report=content if content and not calls else None,
        )
        shown = json.dumps(
            {"content": result.content, "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in calls]},
            ensure_ascii=False,
            indent=1,
        )
        message: dict[str, Any] = {"role": "assistant", "content": result.content}
        if result.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": str(call.name), "arguments": _raw_arguments(raw)},
                }
                for call, raw in zip(calls, result.tool_calls, strict=True)
            ]
        return turn, shown, message

    def tool_result_messages(self, results: list[ToolResult]) -> list[dict[str, Any]]:
        return [
            {
                "role": "tool",
                "tool_call_id": result.model_call_id or "call_unknown",
                "content": json.dumps(result.payload, ensure_ascii=False),
            }
            for result in results
        ]


def _flatten(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("role") == "tool":
        return {"role": "user", "content": f"VÝSLEDOK NÁSTROJA ({message.get('tool_call_id')}): {message['content']}"}
    calls = message.get("tool_calls")
    if message.get("role") == "assistant" and calls:
        listed = [{"name": c["function"]["name"], "arguments": c["function"]["arguments"]} for c in calls]
        text = (message.get("content") or "").strip()
        summary = f"Volania nástrojov: {json.dumps(listed, ensure_ascii=False)}"
        return {"role": "assistant", "content": f"{text}\n{summary}".strip()}
    return message


def protocol_for(name: str, profile: PromptProfile = SOC_PROFILE) -> ToolProtocol:
    return NativeToolProtocol(profile) if name == NativeToolProtocol.id else JsonToolProtocol(profile)
