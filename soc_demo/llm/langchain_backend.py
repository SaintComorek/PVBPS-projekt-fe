"""Živý model cez LangChain: `ChatOpenAI` s nástrojmi `@tool` pripojenými cez `bind_tools`.

Model navrhne volania (`AIMessage.tool_calls`), Analysis Agent ich nechá LangChain zavolať priamo
(`soc_demo.agents.langchain_tools.call_tool`) a odpovede nástrojov vracia ako `ToolMessage`. Endpoint, model,
kľúč a limity sú tie isté ako pri vlastnom HTTP klientovi (OpenAI-kompatibilné API: Groq, Ollama, vLLM, ...).
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import httpx2
import openai
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage, convert_to_messages
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI

from soc_demo.agents.langchain_tools import tools_for
from soc_demo.config import Settings
from soc_demo.domain.errors import LLMError, LLMHTTPError, LLMResponseError, LLMTimeoutError, LLMToolUseError
from soc_demo.domain.events import OutputSource
from soc_demo.llm.backends import TurnResult
from soc_demo.llm.client import describe_http_error
from soc_demo.llm.protocol import SOC_PROFILE, ModelTurn, PromptProfile, ProposedCall, build_native_system_prompt
from soc_demo.llm.tool_protocols import FINAL_REPORT_REQUEST, ToolResult
from soc_demo.security.redaction import SecretRedactor

_NO_KEY = "bez-kluca"  # lokálne endpointy (Ollama) kľúč nepotrebujú, openai SDK ho však vyžaduje


class LangChainToolProtocol:
    """Konverzácia v správach LangChain: AIMessage s tool_calls a ToolMessage od zavolaných toolov."""

    id = "langchain"

    def __init__(self, profile: PromptProfile = SOC_PROFILE) -> None:
        self._profile = profile

    def system_prompt(self) -> str:
        return build_native_system_prompt(self._profile)

    def final_report_request(self) -> str:
        return FINAL_REPORT_REQUEST

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]:
        return [
            result.message
            if isinstance(result.message, ToolMessage)
            else ToolMessage(
                content=json.dumps(result.payload, ensure_ascii=False, default=str),
                tool_call_id=result.model_call_id or "call_unknown",
            )
            for result in results
        ]


class _ChatOpenAIWithReasoning(ChatOpenAI):
    """Groq a ďalšie OpenAI-kompatibilné API vracajú uvažovanie modelu v poli `reasoning`, ktoré LangChain nečíta.

    Prenesie ho do `additional_kwargs["reasoning_content"]`, aby ho časová os ukázala ako pri vlastnom klientovi.
    """

    def _create_chat_result(self, response: Any, generation_info: dict | None = None) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump(warnings=False)
        for generation, choice in zip(result.generations, data.get("choices") or [], strict=False):
            message = choice.get("message") or {}
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                generation.message.additional_kwargs.setdefault("reasoning_content", reasoning)
        return result


class _HttpxBridge(httpx2.AsyncBaseTransport):
    """Podsunutý transport httpx (testovací dvojník servera) pre openai SDK, ktoré používa httpx2."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        forwarded = httpx.Request(request.method, str(request.url), headers=request.headers.multi_items(), content=body)
        response = await self._inner.handle_async_request(forwarded)
        content = await response.aread()
        return httpx2.Response(response.status_code, headers=response.headers.multi_items(), content=content)


class LangChainModelBackend:
    """Skutočné volania LLM cez LangChain. Pri `tool_use_failed` (Groq) jeden nový pokus s vysvetlením."""

    source = OutputSource.LIVE

    def __init__(
        self,
        settings: Settings,
        redactor: SecretRedactor,
        transport: httpx.AsyncBaseTransport | None = None,
        profile: PromptProfile = SOC_PROFILE,
    ) -> None:
        if not settings.llm_configured:
            raise ValueError("LLM nie je nakonfigurovaný (LLM_BASE_URL, LLM_MODEL).")
        self.protocol = LangChainToolProtocol(profile)
        self.label = f"Živý model: {settings.llm_model}"
        self._model_name = str(settings.llm_model)
        self._redactor = redactor
        self._llm = _ChatOpenAIWithReasoning(
            model=self._model_name,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key_value or _NO_KEY,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            reasoning_effort=settings.llm_reasoning_effort,
            http_async_client=httpx2.AsyncClient(transport=_HttpxBridge(transport)) if transport else None,
        )
        self._with_tools = self._llm.bind_tools(tools_for(profile.tools), tool_choice="auto")

    async def next_turn(self, messages: list[Any], *, final: bool = False) -> TurnResult:
        history = convert_to_messages(messages)
        if final:
            # Záverečná požiadavka nástroje vôbec neposiela (niektoré modely ich volajú aj pri tool_choice
            # "none") a históriu volaní prepíše na obyčajný text.
            history = [_flatten(message) for message in history]
        model = self._llm if final else self._with_tools
        started = time.perf_counter()
        retried = False
        try:
            reply = await self._invoke(model, history)
        except LLMToolUseError as exc:
            hint = (
                f"Tvoje posledné volanie nástroja odmietol server: {exc.detail} "
                "Zavolaj nástroj znova so správnymi argumentmi."
            )
            reply = await self._invoke(model, [*history, HumanMessage(hint)])
            retried = True
        return self._turn(reply, int((time.perf_counter() - started) * 1000), retried=retried)

    async def _invoke(self, model: Any, history: list[BaseMessage]) -> AIMessage:
        try:
            reply = await model.ainvoke(history)
        except openai.APITimeoutError as exc:
            raise LLMTimeoutError("LLM neodpovedal v časovom limite (timeout).") from exc
        except openai.APIStatusError as exc:
            raise self._status_error(exc) from exc
        except openai.APIConnectionError as exc:
            message = f"Spojenie s LLM endpointom zlyhalo: {exc.__class__.__name__}"
            raise LLMHTTPError(self._redactor.redact(message), 0) from exc
        if not isinstance(reply, AIMessage):
            raise LLMResponseError("LLM vrátil odpoveď v neočakávanom formáte.")
        return reply

    def _status_error(self, exc: openai.APIStatusError) -> LLMError:
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error", body) if isinstance(body.get("error", body), dict) else {}
        if exc.status_code == 400 and error.get("code") == "tool_use_failed":
            detail = self._redactor.redact(str(error.get("message", ""))[:300])
            return LLMToolUseError(f"Server odmietol volanie nástroja od modelu: {detail}", detail)
        text = exc.response.text if exc.response is not None else str(exc)
        return LLMHTTPError(describe_http_error(exc.status_code, text, self._redactor), exc.status_code)

    def _turn(self, reply: AIMessage, latency_ms: int, *, retried: bool) -> TurnResult:
        calls = [ProposedCall(id=call["id"], name=call["name"], arguments=call["args"]) for call in reply.tool_calls]
        # Argumenty, ktoré nie sú platný JSON, dá LangChain bokom; schéma ich zamietne a model dostane chybu.
        calls += [
            ProposedCall(id=call.get("id"), name=call.get("name"), arguments=call.get("args"))
            for call in reply.invalid_tool_calls
        ]
        content = reply.text.strip()
        finish_reason = reply.response_metadata.get("finish_reason")
        if not content and not calls and finish_reason == "length":
            raise LLMResponseError(
                "Model vyčerpal limit tokenov skôr, ako napísal odpoveď. Zvýšte LLM_MAX_TOKENS "
                "alebo znížte LLM_REASONING_EFFORT."
            )
        turn = ModelTurn(
            thought=content if calls else "",
            tool_calls=calls,
            final_report=content if content and not calls else None,
        )
        shown = json.dumps(
            {"content": reply.text, "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in calls]},
            ensure_ascii=False,
            indent=1,
            default=str,
        )
        reasoning = reply.additional_kwargs.get("reasoning_content")
        return TurnResult(
            turn,
            shown,
            str(reply.response_metadata.get("model_name") or self._model_name),
            latency_ms,
            retried=retried,
            reasoning=reasoning if isinstance(reasoning, str) and reasoning.strip() else None,
            assistant_message=reply,
        )


def _flatten(message: BaseMessage) -> BaseMessage:
    if isinstance(message, ToolMessage):
        return HumanMessage(f"VÝSLEDOK NÁSTROJA ({message.tool_call_id}): {message.text}")
    if isinstance(message, AIMessage) and (message.tool_calls or message.invalid_tool_calls):
        listed = [{"name": c["name"], "arguments": c["args"]} for c in message.tool_calls]
        listed += [{"name": c.get("name"), "arguments": c.get("args")} for c in message.invalid_tool_calls]
        summary = f"Volania nástrojov: {json.dumps(listed, ensure_ascii=False, default=str)}"
        return AIMessage(f"{message.text.strip()}\n{summary}".strip())
    return message
