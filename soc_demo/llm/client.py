"""Minimálny klient pre OpenAI-kompatibilné Chat Completions API (Groq, Ollama, vLLM, LM Studio, ...)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from soc_demo.config import Settings
from soc_demo.domain.errors import LLMHTTPError, LLMResponseError, LLMTimeoutError, LLMToolUseError
from soc_demo.security.redaction import SecretRedactor

logger = logging.getLogger("soc_demo.llm")


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    latency_ms: int
    finish_reason: str | None
    reasoning: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # natívny tool calling (OpenAI formát)


RETRYABLE_STATUS = frozenset({429, 500, 502, 503})
MAX_RETRY_WAIT_S = 30.0
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


class OpenAICompatibleClient:
    """Volá výhradne `{LLM_BASE_URL}/chat/completions`. Adresa sa nikdy neberie z dát ani z modelu."""

    def __init__(
        self,
        settings: Settings,
        redactor: SecretRedactor,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.llm_configured:
            raise ValueError("LLM nie je nakonfigurovaný (LLM_BASE_URL, LLM_MODEL).")
        self._settings = settings
        self._redactor = redactor
        self._transport = transport
        self._sleep = asyncio.sleep

    async def chat(self, messages: list[dict[str, Any]], options: dict[str, Any] | None = None) -> ChatResult:
        """`options` pridá do požiadavky napr. `tools` a `tool_choice` pre natívny tool calling."""
        settings = self._settings
        started = time.perf_counter()
        attempt = 0
        payload = self._payload(messages, options or {})
        while True:
            response = await self._post(payload)
            if response.status_code not in RETRYABLE_STATUS or attempt >= settings.llm_max_retries:
                break
            attempt += 1
            wait_s = _retry_delay(response, attempt)
            logger.warning("llm_retry status=%d attempt=%d wait_s=%.1f", response.status_code, attempt, wait_s)
            await self._sleep(wait_s)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            tool_error = self._tool_use_failure(response)
            if tool_error is not None:
                raise LLMToolUseError(f"Server odmietol volanie nástroja od modelu: {tool_error}", tool_error)
            raise LLMHTTPError(self._describe_http_error(response), response.status_code)
        result = self._parse(response, latency_ms)
        logger.info("llm_call model=%s latency_ms=%d finish=%s", result.model, latency_ms, result.finish_reason)
        return result

    def _payload(self, messages: list[dict[str, Any]], options: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "stream": False,
        }
        if settings.llm_reasoning_effort:
            payload["reasoning_effort"] = settings.llm_reasoning_effort
        if settings.llm_json_mode and "tools" not in options:
            payload["response_format"] = {"type": "json_object"}
        payload.update(options)
        return payload

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        settings = self._settings
        headers = {"Content-Type": "application/json"}
        api_key = settings.llm_api_key_value
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(
                base_url=str(settings.llm_base_url),
                timeout=settings.llm_timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                return await client.post("/chat/completions", json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"LLM neodpovedal do {settings.llm_timeout_seconds:.0f} s (timeout).") from exc
        except httpx.HTTPError as exc:
            raise LLMHTTPError(
                self._redactor.redact(f"Spojenie s LLM endpointom zlyhalo: {exc.__class__.__name__}"), 0
            ) from exc

    def _tool_use_failure(self, response: httpx.Response) -> str | None:
        """Groq overuje argumenty nástrojov voči schéme sám a vráti HTTP 400 s kódom tool_use_failed."""
        if response.status_code != 400:
            return None
        try:
            error = response.json().get("error") or {}
        except (ValueError, AttributeError):
            return None
        if not isinstance(error, dict) or error.get("code") != "tool_use_failed":
            return None
        return self._redactor.redact(str(error.get("message", ""))[:300])

    def _describe_http_error(self, response: httpx.Response) -> str:
        return describe_http_error(response.status_code, response.text, self._redactor)

    def _parse(self, response: httpx.Response, latency_ms: int) -> ChatResult:
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            tool_calls = message.get("tool_calls") or []
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMResponseError("LLM endpoint vrátil odpoveď v neočakávanom formáte.") from exc
        if not isinstance(content, str):
            raise LLMResponseError("Obsah odpovede modelu nie je text.")
        if not isinstance(tool_calls, list) or not all(isinstance(c, dict) for c in tool_calls):
            raise LLMResponseError("Pole tool_calls v odpovedi modelu má neočakávaný formát.")
        finish_reason = choice.get("finish_reason")
        if not content.strip() and not tool_calls and finish_reason == "length":
            raise LLMResponseError(
                "Model vyčerpal limit tokenov skôr, ako napísal odpoveď. Zvýšte LLM_MAX_TOKENS "
                "alebo znížte LLM_REASONING_EFFORT."
            )
        return ChatResult(
            content=content,
            model=str(body.get("model") or self._settings.llm_model),
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            reasoning=reasoning if isinstance(reasoning, str) and reasoning.strip() else None,
            tool_calls=tool_calls,
        )


HTTP_ERROR_HINTS = {
    400: "endpoint odmietol požiadavku, skontrolujte model a parametre",
    401: "neplatný alebo chýbajúci API kľúč",
    403: "prístup zamietnutý",
    404: "nesprávna LLM_BASE_URL alebo neznámy model",
    413: "požiadavka je pre model príliš veľká",
    429: "prekročený limit požiadaviek, skúste o chvíľu znova",
}


def describe_http_error(status_code: int, body: str, redactor: SecretRedactor) -> str:
    """Chyba LLM endpointu pre UI: kód, nápoveda a začiatok odpovede bez tajomstiev."""
    hint = HTTP_ERROR_HINTS.get(status_code, "chyba servera")
    return f"LLM endpoint vrátil HTTP {status_code} ({hint}). Odpoveď: {redactor.redact(body[:300])}"


def _parse_duration(value: str | None) -> float | None:
    """`1.5` (sekundy) alebo trvanie v štýle Groq hlavičiek: `7.66s`, `1m2.5s`, `97.5ms`."""
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        parts = _DURATION_PART.findall(value)
        return sum(float(number) * _DURATION_UNITS[unit] for number, unit in parts) if parts else None


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Najdlhšia z: Retry-After, reset limitu tokenov/požiadaviek (Groq) a exponenciálneho čakania."""
    hints = [
        _parse_duration(response.headers.get(name))
        for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests")
    ]
    wait_s = max([h for h in hints if h is not None] + [2.0 ** (attempt - 1)])
    return max(0.0, min(wait_s, MAX_RETRY_WAIT_S))
