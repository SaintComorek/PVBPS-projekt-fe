"""LangChain: nástroje `@tool`, priame volanie toolov a živý model cez ChatOpenAI na simulovanom serveri."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from soc_demo.agents.langchain_tools import LANGCHAIN_TOOLS, tools_for
from soc_demo.main import create_app
from soc_demo.security.access import ANALYSIS_TOOLS
from soc_demo.tools.schemas import TOOL_REGISTRY
from tests.conftest import make_settings, read_sse, start_run

SECRET = "sk-test-langchain-1234567890"


def _tool_call(call_id: str, name: str, arguments: Any) -> dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


def _response(content: str | None, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-lc",
        "object": "chat.completion",
        "created": 0,
        "model": "openai/gpt-oss-120b",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
    }


def _live_langchain(tmp_path: Path, transport: httpx.AsyncBaseTransport) -> TestClient:
    settings = make_settings(
        tmp_path,
        llm_base_url="http://llm.test/v1",
        llm_model="openai/gpt-oss-120b",
        llm_api_key=SECRET,
        llm_max_retries=0,
    )
    assert settings.llm_tool_protocol == "langchain"  # predvolený protokol
    return TestClient(create_app(settings, llm_transport=transport))


class TestTools:
    def test_every_registry_tool_is_a_langchain_tool_with_the_same_schema(self) -> None:
        assert set(LANGCHAIN_TOOLS) == set(TOOL_REGISTRY)
        for name, spec in TOOL_REGISTRY.items():
            lc_tool = LANGCHAIN_TOOLS[name]
            assert isinstance(lc_tool, BaseTool)
            assert lc_tool.args_schema is spec.args_model
            assert lc_tool.description == spec.description

    def test_model_sees_only_analysis_tools_and_never_the_injected_config(self) -> None:
        definitions = [convert_to_openai_tool(t) for t in tools_for(ANALYSIS_TOOLS)]
        assert [d["function"]["name"] for d in definitions] == list(ANALYSIS_TOOLS)
        for definition in definitions:
            assert "config" not in definition["function"]["parameters"].get("properties", {})


def test_live_run_calls_langchain_tools_through_the_gate(tmp_path: Path) -> None:
    captured: list[dict[str, Any]] = []
    replies = [
        _response(
            None,
            [
                _tool_call("call_a", "update_incident", {"incident_id": "INC-003", "status": "RESOLVED"}),
                _tool_call("call_b", "analyze_incident", {"incident_id": "INC-001"}),
                _tool_call("call_c", "block_ip", {"ip": "999.1.1.1", "reason": "x"}),
            ],
        ),
        _response("## Report\nHotovo."),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(captured) - 1])

    with _live_langchain(tmp_path, httpx.MockTransport(handler)) as client:
        assert client.get("/api/health").json()["llm"]["protocol"] == "langchain"
        run = start_run(client, security_mode="protected", execution_mode="live", scenario="attack")
        events = read_sse(client, run["id"])
        detail = client.get(f"/api/runs/{run['id']}").json()["run"]
        incident = next(i for i in client.get("/api/incidents").json() if i["id"] == "INC-003")

    first, second = captured
    assert [t["function"]["name"] for t in first["tools"]] == list(ANALYSIS_TOOLS)
    assert first["tool_choice"] == "auto"
    tool_messages = [m for m in second["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_a", "call_b", "call_c"]
    statuses = [json.loads(m["content"])["status"] for m in tool_messages]
    assert statuses == ["blocked", "executed", "rejected"]  # brána je vnútri toolu, zlá IP neprejde schémou

    assert detail["status"] == "completed" and detail["report"].startswith("## Report")
    assert incident["status"] == "OPEN"
    assert any(e["type"] == "tool_blocked" for e in events)
    assert any(e["type"] == "tool_call_rejected" for e in events)
    output = next(e for e in events if e["type"] == "model_output")
    assert output["data"]["protocol"] == "langchain" and output["source"] == "live"


def test_vulnerable_mode_executes_the_same_call_without_authorization(tmp_path: Path) -> None:
    replies = [
        _response(None, [_tool_call("call_a", "update_incident", {"incident_id": "INC-003", "status": "RESOLVED"})]),
        _response("## Report"),
    ]
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=replies[len(calls) - 1])

    with _live_langchain(tmp_path, httpx.MockTransport(handler)) as client:
        run = start_run(client, security_mode="vulnerable", execution_mode="live", scenario="attack")
        events = read_sse(client, run["id"])
        incident = next(i for i in client.get("/api/incidents").json() if i["id"] == "INC-003")
    executed = next(e for e in events if e["type"] == "tool_executed" and e["data"]["tool"] == "update_incident")
    assert executed["data"]["unauthorized"] is True
    assert incident["status"] == "RESOLVED"


def test_server_side_tool_validation_error_gets_one_retry(tmp_path: Path) -> None:
    error = {"message": "parameters for tool update_incident did not match schema", "code": "tool_use_failed"}
    captured: list[dict[str, Any]] = []
    replies = [httpx.Response(400, json={"error": error}), httpx.Response(200, json=_response("## Report"))]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return replies[len(captured) - 1]

    with _live_langchain(tmp_path, httpx.MockTransport(handler)) as client:
        run = start_run(client, security_mode="protected", execution_mode="live")
        events = read_sse(client, run["id"])
    assert events[-1]["type"] == "run_completed"
    assert "odmietol server" in captured[1]["messages"][-1]["content"]
    assert next(e for e in events if e["type"] == "model_output")["data"]["retried"] is True


@pytest.mark.parametrize("status", [401, 500])
def test_http_error_fails_the_run_without_leaking_the_key(tmp_path: Path, status: int) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, text=f"bad key {SECRET}"))
    with _live_langchain(tmp_path, transport) as client:
        run = start_run(client, security_mode="protected", execution_mode="live")
        events = read_sse(client, run["id"])
        everything = json.dumps(events) + client.get("/api/audit").text
    assert events[-1]["type"] == "run_failed"
    assert next(e for e in events if e["type"] == "model_error")["data"]["kind"] == "http_error"
    assert SECRET not in everything


def test_final_request_sends_no_tools_and_flattens_history(tmp_path: Path) -> None:
    captured: list[dict[str, Any]] = []
    blocked = _tool_call("call_x", "update_incident", {"incident_id": "INC-003", "status": "RESOLVED"})

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "tools" not in body:
            return httpx.Response(200, json=_response("## Report bez zmien"))
        return httpx.Response(200, json=_response(None, [blocked]))

    with _live_langchain(tmp_path, httpx.MockTransport(handler)) as client:
        run = start_run(client, security_mode="protected", execution_mode="live", scenario="attack")
        events = read_sse(client, run["id"])
    assert [c.get("tool_choice") for c in captured] == ["auto", "auto", None]
    final_messages = captured[-1]["messages"]
    assert all(m["role"] != "tool" and "tool_calls" not in m for m in final_messages)
    assert events[-1]["type"] == "run_completed"


def test_model_reasoning_reaches_the_timeline(tmp_path: Path) -> None:
    reply = _response("## Report")
    reply["choices"][0]["message"]["reasoning"] = "Najprv overím incidenty."
    with _live_langchain(tmp_path, httpx.MockTransport(lambda r: httpx.Response(200, json=reply))) as client:
        run = start_run(client, security_mode="protected", execution_mode="live")
        events = read_sse(client, run["id"])
    output = next(e for e in events if e["type"] == "model_output")
    assert output["data"]["reasoning"] == "Najprv overím incidenty."
