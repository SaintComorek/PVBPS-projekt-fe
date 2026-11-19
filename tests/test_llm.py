"""JSON protokol a OpenAI-kompatibilný klient (proti testovaciemu dvojníkovi HTTP servera)."""

from __future__ import annotations

import json

import httpx
import pytest

from soc_demo.config import Settings
from soc_demo.domain.errors import LLMHTTPError, LLMResponseError, LLMTimeoutError, ModelOutputError
from soc_demo.llm.backends import LiveModelBackend
from soc_demo.llm.client import OpenAICompatibleClient
from soc_demo.llm.protocol import build_system_prompt, build_task_message, parse_model_turn
from soc_demo.llm.tool_protocols import JsonToolProtocol
from soc_demo.security.redaction import SecretRedactor
from tests.conftest import chat_response, make_settings

SECRET = "sk-test-SECRET-1234567890"
VALID = json.dumps({"thought": "ok", "tool_calls": [], "final_report": "Report"})


class TestProtocol:
    def test_parses_plain_json(self) -> None:
        turn = parse_model_turn(
            '{"thought": "t", "tool_calls": [{"name": "analyze_incident", "arguments": {"incident_id": "INC-001"}}], "final_report": null}'
        )
        assert turn.tool_calls[0].name == "analyze_incident"
        assert turn.tool_calls[0].arguments == {"incident_id": "INC-001"}
        assert turn.final_report is None

    def test_parses_fenced_json_with_surrounding_text(self) -> None:
        text = 'Jasné, tu je odpoveď:\n```json\n{"thought": "a { b }", "tool_calls": [], "final_report": "R"}\n```\nĎakujem.'
        turn = parse_model_turn(text)
        assert turn.thought == "a { b }" and turn.final_report == "R"

    def test_parses_json_embedded_in_prose(self) -> None:
        turn = parse_model_turn('Odpoveď: {"thought": "x \\" } y", "tool_calls": [], "final_report": "R"} hotovo')
        assert turn.final_report == "R"

    @pytest.mark.parametrize("text", ["Neviem pomôcť.", "{not json}", "[1, 2]", '{"tool_calls": "update_incident"}'])
    def test_invalid_output_raises(self, text: str) -> None:
        with pytest.raises(ModelOutputError):
            parse_model_turn(text)

    def test_non_string_report_is_serialized(self) -> None:
        turn = parse_model_turn('{"tool_calls": [], "final_report": {"INC-001": "HIGH"}}')
        assert turn.final_report == '{"INC-001": "HIGH"}'

    def test_prompt_lists_only_analysis_tools_and_marks_untrusted_data(self) -> None:
        prompt = build_system_prompt()
        assert "update_incident" in prompt and "analyze_incident" in prompt
        assert "read_incidents" not in prompt
        message = build_task_message("Analyzuj.", [{"id": "INC-001"}])
        assert message.startswith("POŽIADAVKA POUŽÍVATEĽA:\nAnalyzuj.")
        assert '"trust": "untrusted"' in message


def _client(settings: Settings, handler) -> OpenAICompatibleClient:  # noqa: ANN001
    return OpenAICompatibleClient(settings, SecretRedactor([SECRET]), transport=httpx.MockTransport(handler))


@pytest.fixture
def live_settings(tmp_path) -> Settings:  # noqa: ANN001
    return make_settings(tmp_path, llm_base_url="http://llm.test/v1/", llm_model="gemma-3-12b-it", llm_api_key=SECRET)


class TestClient:
    async def test_sends_openai_compatible_request(self, live_settings: Settings) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=chat_response(VALID))

        result = await _client(live_settings, handler).chat([{"role": "user", "content": "ahoj"}])
        request = captured[0]
        assert str(request.url) == "http://llm.test/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        body = json.loads(request.content)
        assert body["model"] == "gemma-3-12b-it" and body["messages"][0]["content"] == "ahoj"
        assert "tools" not in body
        assert result.content == VALID

    async def test_timeout_is_typed(self, live_settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        with pytest.raises(LLMTimeoutError):
            await _client(live_settings, handler).chat([])

    async def test_http_error_message_never_contains_key(self, live_settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text=f"invalid api key {SECRET}")

        with pytest.raises(LLMHTTPError) as info:
            await _client(live_settings, handler).chat([])
        assert info.value.status_code == 401
        assert SECRET not in info.value.message and "API kľúč" in info.value.message

    async def test_unexpected_body_raises_response_error(self, live_settings: Settings) -> None:
        with pytest.raises(LLMResponseError):
            await _client(live_settings, lambda r: httpx.Response(200, json={"foo": 1})).chat([])

    async def test_live_backend_retries_once_on_invalid_json(self, live_settings: Settings) -> None:
        replies = iter(["Toto nie je JSON.", VALID])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=chat_response(next(replies)))

        backend = LiveModelBackend(_client(live_settings, handler), "gemma", JsonToolProtocol())
        result = await backend.next_turn([{"role": "user", "content": "x"}])
        assert result.retried is True and result.turn.final_report == "Report"

    async def test_live_backend_gives_up_after_second_invalid_output(self, live_settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=chat_response("Prepáčte, s tým nepomôžem."))

        backend = LiveModelBackend(_client(live_settings, handler), "gemma", JsonToolProtocol())
        with pytest.raises(ModelOutputError):
            await backend.next_turn([])

    def test_client_requires_configuration(self, settings: Settings) -> None:
        with pytest.raises(ValueError):
            OpenAICompatibleClient(settings, SecretRedactor())

    def test_base_url_must_be_http(self, tmp_path) -> None:  # noqa: ANN001
        with pytest.raises(ValueError):
            make_settings(tmp_path, llm_base_url="file:///etc/passwd", llm_model="x")


class TestGroqCompatibility:
    @pytest.fixture
    def groq_settings(self, tmp_path) -> Settings:  # noqa: ANN001
        return make_settings(
            tmp_path,
            llm_base_url="https://api.groq.com/openai/v1",
            llm_model="openai/gpt-oss-120b",
            llm_api_key=SECRET,
            llm_reasoning_effort="medium",
            llm_json_mode=True,
            llm_max_retries=2,
        )

    async def test_payload_contains_reasoning_effort_and_json_mode(self, groq_settings: Settings) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=chat_response(VALID))

        await _client(groq_settings, handler).chat([])
        body = json.loads(captured[0].content)
        assert str(captured[0].url) == "https://api.groq.com/openai/v1/chat/completions"
        assert body["reasoning_effort"] == "medium"
        assert body["response_format"] == {"type": "json_object"}

    async def test_optional_parameters_are_omitted_by_default(self, live_settings: Settings) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=chat_response(VALID))

        await _client(live_settings, handler).chat([])
        body = json.loads(captured[0].content)
        assert "reasoning_effort" not in body and "response_format" not in body

    async def test_retries_rate_limit_and_respects_retry_after(self, groq_settings: Settings) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "1.5"}, text="rate limit"),
                httpx.Response(200, json=chat_response(VALID)),
            ]
        )
        waits: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waits.append(seconds)

        client = _client(groq_settings, lambda r: next(responses))
        client._sleep = fake_sleep
        result = await client.chat([])
        assert result.content == VALID and waits == [1.5]

    async def test_waits_for_groq_token_limit_reset(self, groq_settings: Settings) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "1", "x-ratelimit-reset-tokens": "7.66s"}, text="tpm"),
                httpx.Response(429, headers={"x-ratelimit-reset-tokens": "97.5ms"}, text="tpm"),
                httpx.Response(200, json=chat_response(VALID)),
            ]
        )
        waits: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waits.append(seconds)

        client = _client(groq_settings, lambda r: next(responses))
        client._sleep = fake_sleep
        await client.chat([])
        assert waits == [7.66, 2.0]  # druhý pokus: exponenciálne čakanie je dlhšie ako 97,5 ms

    async def test_gives_up_after_max_retries(self, groq_settings: Settings) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, text="rate limit")

        async def fake_sleep(seconds: float) -> None:
            return None

        client = _client(groq_settings, handler)
        client._sleep = fake_sleep
        with pytest.raises(LLMHTTPError) as info:
            await client.chat([])
        assert info.value.status_code == 429 and len(calls) == 3

    async def test_reasoning_is_returned_separately(self, groq_settings: Settings) -> None:
        body = chat_response(VALID)
        body["choices"][0]["message"]["reasoning"] = "Príloha obsahuje pokyn, ktorý nepochádza od používateľa."
        result = await _client(groq_settings, lambda r: httpx.Response(200, json=body)).chat([])
        assert result.reasoning == "Príloha obsahuje pokyn, ktorý nepochádza od používateľa."
        assert result.content == VALID

    async def test_empty_answer_after_token_limit_is_explained(self, groq_settings: Settings) -> None:
        body = chat_response("")
        body["choices"][0]["finish_reason"] = "length"
        with pytest.raises(LLMResponseError, match="LLM_MAX_TOKENS"):
            await _client(groq_settings, lambda r: httpx.Response(200, json=body)).chat([])

    def test_groq_api_key_alias_is_accepted(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("GROQ_API_KEY", SECRET)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        settings = Settings(_env_file=None, runtime_dir=tmp_path / "var")
        assert settings.llm_api_key_configured and settings.llm_api_key_value == SECRET
