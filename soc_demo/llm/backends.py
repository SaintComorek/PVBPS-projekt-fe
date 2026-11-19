"""Zdroje rozhodnutí Analysis Agenta: živý LLM alebo prehrávaný syntetický skript."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from soc_demo.domain.errors import LLMToolUseError, ModelOutputError, NotFoundError
from soc_demo.domain.events import OutputSource
from soc_demo.llm.client import ChatResult, OpenAICompatibleClient
from soc_demo.llm.protocol import SOC_PROFILE, ModelTurn, PromptProfile
from soc_demo.llm.tool_protocols import ConversationProtocol, JsonToolProtocol, ToolProtocol


@dataclass(frozen=True)
class TurnResult:
    turn: ModelTurn
    raw_text: str
    model: str
    latency_ms: int
    retried: bool = False
    reasoning: str | None = None
    # ťah modelu do histórie konverzácie: dict (vlastný HTTP klient, replay) alebo AIMessage (LangChain)
    assistant_message: Any = field(default_factory=dict)


class ModelBackend(Protocol):
    source: OutputSource
    label: str
    protocol: ConversationProtocol

    async def next_turn(self, messages: list[Any], *, final: bool = False) -> TurnResult: ...


class LiveModelBackend:
    """Skutočné volania LLM. Pri neplatnom JSON (iba JSON protokol) jeden opravný pokus, potom chyba."""

    source = OutputSource.LIVE

    def __init__(self, client: OpenAICompatibleClient, model_name: str, protocol: ToolProtocol) -> None:
        self._client = client
        self.protocol = protocol
        self.label = f"Živý model: {model_name}"

    async def next_turn(self, messages: list[dict[str, Any]], *, final: bool = False) -> TurnResult:
        messages, options = self.protocol.request(messages, final=final)
        try:
            result = await self._client.chat(messages, options)
        except LLMToolUseError as exc:
            # Model vygeneroval argumenty mimo schémy a server ich odmietol: jeden nový pokus s vysvetlením.
            hint = (
                f"Tvoje posledné volanie nástroja odmietol server: {exc.detail} "
                "Zavolaj nástroj znova so správnymi argumentmi."
            )
            retry = await self._client.chat([*messages, {"role": "user", "content": hint}], options)
            return self._turn(retry, retry.latency_ms, retried=True)
        try:
            return self._turn(result, result.latency_ms)
        except ModelOutputError:
            correction = self.protocol.correction_message
            if correction is None:
                raise
            retry_messages = [
                *messages,
                {"role": "assistant", "content": result.content},
                {"role": "user", "content": correction},
            ]
            retry = await self._client.chat(retry_messages, options)
            return self._turn(retry, result.latency_ms + retry.latency_ms, retried=True)

    def _turn(self, result: ChatResult, latency_ms: int, *, retried: bool = False) -> TurnResult:
        turn, shown, message = self.protocol.parse(result)
        return TurnResult(
            turn,
            shown,
            result.model,
            latency_ms,
            retried=retried,
            reasoning=result.reasoning,
            assistant_message=message,
        )


class ReplayScript(BaseModel):
    id: str
    task_id: str
    injection_present: bool
    label: str
    turns: list[ModelTurn]


class ReplayLibrary:
    def __init__(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self._scripts = [ReplayScript.model_validate(item) for item in raw["scripts"]]

    def select(self, task_id: str, injection_present: bool) -> ReplayScript:
        for script in self._scripts:
            if script.task_id == task_id and script.injection_present == injection_present:
                return script
        raise NotFoundError(f"Pre úlohu {task_id} neexistuje replay skript.")

    def all(self) -> list[ReplayScript]:
        return list(self._scripts)


class ReplayModelBackend:
    """Vracia vopred napísané (syntetické) odpovede modelu v pevnom poradí."""

    source = OutputSource.REPLAY

    def __init__(self, script: ReplayScript, profile: PromptProfile = SOC_PROFILE) -> None:
        self.protocol: ToolProtocol = JsonToolProtocol(profile)
        self._script = script
        self._index = 0
        self.label = f"Syntetický záznam: {script.label}"

    async def next_turn(self, messages: list[dict[str, Any]], *, final: bool = False) -> TurnResult:
        if self._index >= len(self._script.turns):
            raise ModelOutputError("Replay skript nemá ďalší krok.", "")
        turn = self._script.turns[self._index]
        self._index += 1
        raw = turn.model_dump_json(indent=1, exclude={"tool_calls": {"__all__": {"id"}}})
        return TurnResult(
            turn=turn,
            raw_text=raw,
            model=f"replay:{self._script.id}",
            latency_ms=0,
            assistant_message={"role": "assistant", "content": raw},
        )
