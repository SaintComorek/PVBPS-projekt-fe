"""Integračné testy REST API a SSE streamu."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from soc_demo.main import create_app
from tests.conftest import (
    INJECTION_TEXT,
    make_settings,
    read_sse,
    scripted_transport,
    start_run,
    wait_for_run,
)

SECRET = "sk-test-SECRET-1234567890"


def test_health_and_state(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200 and health.headers["X-Request-ID"]
    llm = health.json()["llm"]
    assert llm["configured"] is False and llm["model"] is None and llm["api_key_configured"] is False
    state = client.get("/api/demo/state").json()
    assert [i["id"] for i in state["incidents"]] == ["INC-001", "INC-002", "INC-003"]
    assert all(a["present"] is False for a in state["attacks"])
    assert [t["id"] for t in state["tasks"]] == [
        "analyze_report",
        "triage_with_approval",
        "notify_lead",
        "contain_threat",
        "playbook_response",
        "pay_invoices",
        "helpdesk_answer",
    ]
    assert [a["id"] for a in state["attacks"]] == [
        "status_override",
        "exfil_email",
        "poisoned_threat_feed",
        "poisoned_playbook",
        "vendor_bank_change",
        "poisoned_kb",
    ]
    assert [v["id"] for v in state["finance"]["vendors"]] == ["VEN-001", "VEN-002", "VEN-003"]
    assert state["finance"]["payments"] == [] and len(state["finance"]["invoices"]) == 3
    assert state["knowledge"]["doc_id"] == "KB-IT" and state["knowledge"]["replies"] == []
    assert all(c["trust"] == "trusted" for c in state["knowledge"]["chunks"])
    assert state["environment"]["internal_email_domains"] == ["acme.example"]
    assert state["environment"]["firewall_blocklist"] == [] and state["environment"]["outbox"] == []


def _attack(state: dict[str, Any], attack_id: str) -> dict[str, Any]:
    return next(a for a in state["attacks"] if a["id"] == attack_id)


def test_inject_and_reset_endpoints(client: TestClient) -> None:
    state = client.post("/api/demo/inject").json()  # bez tela = pôvodný útok status_override
    assert _attack(state, "status_override")["present"] is True
    inc2 = next(i for i in state["incidents"] if i["id"] == "INC-002")
    assert any(INJECTION_TEXT in e["content"] and e["trust"] == "untrusted" for e in inc2["evidence"])
    assert _attack(client.post("/api/demo/inject").json(), "status_override")["present"] is True

    state = client.post("/api/demo/inject", json={"attack_id": "poisoned_playbook"}).json()
    assert _attack(state, "poisoned_playbook")["present"] is True
    article = next(a for a in state["environment"]["playbooks"] if a["id"] == "KB-PHISH-01")
    assert any(s["trust"] == "untrusted" for s in article["sections"])
    assert client.post("/api/demo/inject", json={"attack_id": "nope"}).status_code == 404

    state = client.post("/api/demo/reset", json={}).json()
    assert all(a["present"] is False for a in state["attacks"])
    assert client.get("/api/incidents/INC-003").json()["status"] == "OPEN"


def test_validation_and_not_found_errors(client: TestClient) -> None:
    bad = client.post("/api/runs", json={"security_mode": "yolo", "execution_mode": "replay"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "VALIDATION_ERROR"
    missing = client.get("/api/runs/RUN-nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/api/incidents/INC-999").status_code == 404
    assert (
        client.post(
            "/api/runs", json={"task_id": "rm_everything", "security_mode": "protected", "execution_mode": "replay"}
        ).status_code
        == 404
    )


def test_live_mode_without_configuration_returns_503(client: TestClient) -> None:
    response = client.post("/api/runs", json={"security_mode": "protected", "execution_mode": "live"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LIVE_MODE_UNAVAILABLE"


def test_full_demo_flow_over_sse(client: TestClient) -> None:
    run = start_run(client, security_mode="vulnerable", execution_mode="replay", scenario="attack")
    events = read_sse(client, run["id"])
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "run_completed"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert any(e["type"] == "state_changed" and e["data"]["unauthorized"] for e in events)
    assert client.get("/api/incidents/INC-003").json()["status"] == "RESOLVED"

    client.post("/api/demo/reset")
    assert client.get("/api/incidents/INC-003").json()["status"] == "OPEN"

    run = start_run(client, security_mode="protected", execution_mode="replay", scenario="attack")
    events = read_sse(client, run["id"])
    assert any(e["type"] == "tool_blocked" for e in events)
    assert not any(e["type"] == "state_changed" for e in events)
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["run"]["summary"]["blocked"] == 1
    assert detail["run"]["incidents_after"] == detail["run"]["incidents_before"]
    assert client.get("/api/incidents/INC-003").json()["status"] == "OPEN"
    audit = client.get("/api/audit", params={"run_id": run["id"]}).json()
    assert any(a["action"] == "TOOL_BLOCKED" for a in audit)


def test_sse_resumes_after_last_event_id(client: TestClient) -> None:
    run = start_run(client, security_mode="protected", execution_mode="replay")
    all_events = read_sse(client, run["id"])
    resumed = read_sse(client, run["id"], headers={"Last-Event-ID": "5"})
    assert [e["seq"] for e in resumed] == [e["seq"] for e in all_events[5:]]


def test_second_run_while_active_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, replay_event_delay_ms=30)
    with TestClient(create_app(settings)) as client:
        run = start_run(client, security_mode="protected", execution_mode="replay")
        conflict = client.post("/api/runs", json={"security_mode": "protected", "execution_mode": "replay"})
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "RUN_IN_PROGRESS"
        assert client.post("/api/demo/reset").status_code == 409
        assert client.post("/api/demo/inject").status_code == 409
        wait_for_run(client, run["id"])
        assert client.post("/api/demo/reset").status_code == 200


def test_metrics_separate_live_and_replay(client: TestClient) -> None:
    for mode in ("vulnerable", "protected"):
        client.post("/api/demo/reset")
        run = start_run(client, security_mode=mode, execution_mode="replay", scenario="attack")
        wait_for_run(client, run["id"])
    groups = {g["execution_mode"]: g for g in client.get("/api/metrics").json()}
    assert groups["live"]["rows"][0]["runs"] == 0 and groups["live"]["rows"][1]["runs"] == 0
    replay = {r["security_mode"]: r for r in groups["replay"]["rows"]}
    assert replay["vulnerable"]["unauthorized_executed"] == 1 and replay["vulnerable"]["blocked"] == 0
    assert replay["protected"]["unauthorized_executed"] == 0 and replay["protected"]["blocked"] == 1
    assert "Syntetické" in groups["replay"]["label"]

    client.post("/api/demo/reset", json={"clear_history": True})
    assert client.get("/api/runs").json() == []


def test_approval_flow(client: TestClient) -> None:
    run = start_run(
        client, task_id="triage_with_approval", security_mode="protected", execution_mode="replay", scenario="attack"
    )
    wait_for_run(client, run["id"])
    approvals = client.get("/api/approvals").json()
    assert len(approvals) == 2 and all(a["status"] == "pending" for a in approvals)
    assert client.get("/api/demo/state").json()["pending_approvals"] == 2
    legit = next(a for a in approvals if a["arguments"]["incident_id"] == "INC-001")
    injected = next(a for a in approvals if a["arguments"]["incident_id"] == "INC-003")
    assert injected["taint_hint"]["location_id"] == "INC-002"

    result = client.post(f"/api/approvals/{legit['id']}/decision", json={"approve": True}).json()
    assert result["outcome"]["status"] == "executed"
    rejected = client.post(f"/api/approvals/{injected['id']}/decision", json={"approve": False}).json()
    assert rejected["approval"]["status"] == "rejected"
    again = client.post(f"/api/approvals/{injected['id']}/decision", json={"approve": True})
    assert again.status_code == 409

    statuses = {i["id"]: i["status"] for i in client.get("/api/incidents").json()}
    assert statuses == {"INC-001": "INVESTIGATING", "INC-002": "INVESTIGATING", "INC-003": "OPEN"}


def _live_client(tmp_path: Path, transport: httpx.AsyncBaseTransport, protocol: str = "json-tools") -> TestClient:
    settings = make_settings(
        tmp_path,
        llm_base_url="http://llm.test/v1",
        llm_model="gemma-test",
        llm_api_key=SECRET,
        llm_tool_protocol=protocol,
    )
    return TestClient(create_app(settings, llm_transport=transport))


def test_live_run_records_real_model_proposals(tmp_path: Path) -> None:
    """Dvojník servera vráti návrh zápisu – overujeme, že ho systém spracuje ako živý výstup."""
    turn1 = json.dumps(
        {
            "thought": "Podľa prílohy",
            "tool_calls": [{"name": "update_incident", "arguments": {"incident_id": "INC-003", "status": "RESOLVED"}}],
            "final_report": None,
        }
    )
    turn2 = json.dumps({"thought": "hotovo", "tool_calls": [], "final_report": "Report"})
    captured: list[httpx.Request] = []
    with _live_client(tmp_path, scripted_transport([turn1, turn2], captured)()) as client:
        run = start_run(client, security_mode="protected", execution_mode="live", scenario="attack")
        events = read_sse(client, run["id"])
        outputs = [e for e in events if e["type"] == "model_output"]
        assert outputs and all(e["source"] == "live" for e in outputs)
        assert any(e["type"] == "tool_blocked" for e in events)
        first_prompt = json.loads(captured[0].content)["messages"][1]["content"]
        assert INJECTION_TEXT in first_prompt  # model naozaj dostal nedôveryhodný obsah
        assert '"injected"' not in first_prompt  # interný príznak dema model nevidí
        assert '"trust": "untrusted"' in first_prompt
        groups = {g["execution_mode"]: g for g in client.get("/api/metrics").json()}
        assert groups["live"]["rows"][1]["blocked"] == 1


def test_live_failure_is_reported_without_leaking_secret(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(401, text=f"bad key {SECRET}"))
    caplog.set_level(logging.INFO)
    with _live_client(tmp_path, transport) as client:
        assert SECRET not in client.get("/api/health").text
        assert client.get("/api/health").json()["llm"]["api_key_configured"] is True
        run = start_run(client, security_mode="vulnerable", execution_mode="live")
        events = read_sse(client, run["id"])
        assert events[-1]["type"] == "run_failed"
        error = next(e for e in events if e["type"] == "model_error")
        assert error["data"]["kind"] == "http_error"
        everything = (
            json.dumps(events) + client.get("/api/audit").text + json.dumps(client.get(f"/api/runs/{run['id']}").json())
        )
        assert SECRET not in everything
        audit_file = (tmp_path / "var" / "audit.jsonl").read_text(encoding="utf-8")
        assert SECRET not in audit_file
        assert client.get("/api/health").json()["active_run_id"] is None
    assert SECRET not in caplog.text


def test_live_model_reasoning_is_shown_in_timeline(tmp_path: Path) -> None:
    final = json.dumps({"thought": "hotovo", "tool_calls": [], "final_report": "Report"})

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "model": "openai/gpt-oss-120b",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": final, "reasoning": "Príloha nie je od používateľa."},
                }
            ],
        }
        return httpx.Response(200, json=body)

    with _live_client(tmp_path, httpx.MockTransport(handler)) as client:
        run = start_run(client, security_mode="protected", execution_mode="live")
        events = read_sse(client, run["id"])
        output = next(e for e in events if e["type"] == "model_output")
        assert output["data"]["reasoning"] == "Príloha nie je od používateľa."
        assert output["data"]["model"] == "openai/gpt-oss-120b"
