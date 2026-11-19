"""Natívny tool calling (OpenAI `tools` / `tool_calls`) cez simulovaný server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from soc_demo.llm.client import ChatResult
from soc_demo.llm.tool_protocols import NativeToolProtocol, ToolResult, native_tool_definitions
from soc_demo.main import create_app
from soc_demo.security.access import ANALYSIS_TOOLS
from tests.conftest import make_settings, read_sse, start_run


def _tool_call(call_id: str, name: str, arguments: Any) -> dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


def _response(content: str | None, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "model": "openai/gpt-oss-120b",
        "choices": [{"finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
    }


def _chat(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> ChatResult:
    return ChatResult(content=content, model="m", latency_ms=1, finish_reason=None, tool_calls=tool_calls or [])


class TestNativeProtocol:
    def test_tool_definitions_cover_analysis_tools_without_refs(self) -> None:
        tools = native_tool_definitions()
        assert [t["function"]["name"] for t in tools] == list(ANALYSIS_TOOLS)
        assert "$defs" not in json.dumps(tools) and "$ref" not in json.dumps(tools)
        update = next(t for t in tools if t["function"]["name"] == "update_incident")
        assert update["function"]["parameters"]["properties"]["status"]["enum"] == ["OPEN", "INVESTIGATING", "RESOLVED"]

    def test_parse_tool_calls_keeps_ids_and_decodes_arguments(self) -> None:
        result = _chat(
            tool_calls=[
                _tool_call("call_a", "analyze_incident", {"incident_id": "INC-001"}),
                _tool_call("call_b", "update_incident", "{not json"),
            ]
        )
        turn, shown, message = NativeToolProtocol().parse(result)
        assert [(c.id, c.name) for c in turn.tool_calls] == [
            ("call_a", "analyze_incident"),
            ("call_b", "update_incident"),
        ]
        assert turn.tool_calls[0].arguments == {"incident_id": "INC-001"}
        assert turn.tool_calls[1].arguments == "{not json"  # neplatný JSON zamietne až validácia schémy
        assert turn.final_report is None and "analyze_incident" in shown
        assert [c["id"] for c in message["tool_calls"]] == ["call_a", "call_b"]
        assert message["tool_calls"][1]["function"]["arguments"] == "{not json"

    def test_text_without_tool_calls_is_the_final_report(self) -> None:
        turn, _, message = NativeToolProtocol().parse(_chat("## Report"))
        assert turn.final_report == "## Report" and turn.tool_calls == []
        assert "tool_calls" not in message

    def test_tool_results_are_sent_as_tool_messages(self) -> None:
        messages = NativeToolProtocol().tool_result_messages([ToolResult("call_a", {"status": "blocked"})])
        assert messages == [{"role": "tool", "tool_call_id": "call_a", "content": '{"status": "blocked"}'}]


def _live_native(tmp_path: Path, transport: httpx.AsyncBaseTransport) -> TestClient:
    settings = make_settings(
        tmp_path,
        llm_base_url="http://llm.test/v1",
        llm_model="openai/gpt-oss-120b",
        llm_api_key="sk-test-native-1234567890",
        llm_json_mode=True,
        llm_tool_protocol="native-tools",
    )
    return TestClient(create_app(settings, llm_transport=transport))


def test_live_run_with_native_tool_calling(tmp_path: Path) -> None:
    captured: list[dict[str, Any]] = []
    replies = [
        _response(
            None,
            [
                _tool_call("call_a", "update_incident", {"incident_id": "INC-003", "status": "RESOLVED"}),
                _tool_call("call_b", "analyze_incident", {"incident_id": "INC-001"}),
                _tool_call("call_c", "update_incident", "incident_id=INC-003"),
            ],
        ),
        _response("## Report\nHotovo."),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(captured) - 1])

    with _live_native(tmp_path, httpx.MockTransport(handler)) as client:
        assert client.get("/api/health").json()["llm"]["protocol"] == "native-tools"
        run = start_run(client, security_mode="protected", execution_mode="live", scenario="attack")
        events = read_sse(client, run["id"])
        detail = client.get(f"/api/runs/{run['id']}").json()["run"]

    first, second = captured
    assert [t["function"]["name"] for t in first["tools"]] == list(ANALYSIS_TOOLS)
    assert first["tool_choice"] == "auto" and "response_format" not in first
    assert "JSON objektom" not in first["messages"][0]["content"]  # natívny prompt, nie JSON protokol
    assistant = second["messages"][2]
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_a", "call_b", "call_c"]
    tool_messages = [m for m in second["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_a", "call_b", "call_c"]
    statuses = [json.loads(m["content"])["status"] for m in tool_messages]
    assert statuses == ["blocked", "executed", "rejected"]

    assert detail["status"] == "completed" and detail["report"].startswith("## Report")
    assert any(e["type"] == "tool_blocked" for e in events)
    assert any(e["type"] == "tool_call_rejected" for e in events)
    output = next(e for e in events if e["type"] == "model_output")
    assert output["data"]["protocol"] == "native-tools" and output["source"] == "live"


def _tool_use_failed() -> httpx.Response:
    error = {
        "message": "Tool call validation failed: parameters for tool update_incident did not match schema",
        "type": "invalid_request_error",
        "code": "tool_use_failed",
    }
    return httpx.Response(400, json={"error": error})


def test_server_side_tool_validation_error_gets_one_retry(tmp_path: Path) -> None:
    captured: list[dict[str, Any]] = []
    replies = [_tool_use_failed(), httpx.Response(200, json=_response("## Report"))]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return replies[len(captured) - 1]

    with _live_native(tmp_path, httpx.MockTransport(handler)) as client:
        run = start_run(client, security_mode="protected", execution_mode="live")
        events = read_sse(client, run["id"])
    assert events[-1]["type"] == "run_completed"
    assert "odmietol server" in captured[1]["messages"][-1]["content"]
    assert next(e for e in events if e["type"] == "model_output")["data"]["retried"] is True


def test_repeated_tool_validation_error_fails_the_run_cleanly(tmp_path: Path) -> None:
    with _live_native(tmp_path, httpx.MockTransport(lambda r: _tool_use_failed())) as client:
        run = start_run(client, security_mode="protected", execution_mode="live")
        events = read_sse(client, run["id"])
    assert events[-1]["type"] == "run_failed"
    assert next(e for e in events if e["type"] == "model_error")["data"]["kind"] == "invalid_tool_call"


def test_model_repeating_a_blocked_call_is_asked_for_a_final_report(tmp_path: Path) -> None:
    """Živý model v chránenom režime opakoval zablokovaný zápis, kým neminul limit krokov."""
    captured: list[dict[str, Any]] = []
    blocked = _tool_call("call_x", "update_incident", {"incident_id": "INC-003", "status": "RESOLVED"})

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "tools" not in body:  # záverečná požiadavka bez nástrojov
            return httpx.Response(200, json=_response("## Report bez zmien"))
        return httpx.Response(200, json=_response(None, [blocked]))

    with _live_native(tmp_path, httpx.MockTransport(handler)) as client:
        run = start_run(client, security_mode="protected", execution_mode="live", scenario="attack")
        events = read_sse(client, run["id"])
    assert [c.get("tool_choice") for c in captured] == ["auto", "auto", None]
    final_messages = captured[-1]["messages"]
    assert "Nevolaj žiadne nástroje" in final_messages[-1]["content"]
    assert all(m["role"] != "tool" and "tool_calls" not in m for m in final_messages)  # história ako text
    assert events[-1]["type"] == "run_completed"
    assert sum(e["type"] == "tool_blocked" for e in events) == 2
    assert any(e["type"] == "agent_status" and e["data"].get("reason") == "repeated_blocked_call" for e in events)
