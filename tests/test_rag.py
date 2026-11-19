"""RAG nad IT príručkou (scenár H): chunkovanie podľa nadpisov, otrávená znalostná báza a odkazy v odpovediach."""

from __future__ import annotations

from typing import Any

import pytest

from soc_demo.container import Container
from soc_demo.domain.events import AgentName, EventType
from soc_demo.domain.models import ChangeTarget, ExecutionMode, Scenario, SecurityMode
from soc_demo.llm.protocol import HELPDESK_PROFILE, model_view
from soc_demo.security.access import AGENT_PROFILES, DEMO_ANALYST, TASK_CATALOG
from soc_demo.security.policy import Decision
from soc_demo.services.knowledge_base import CHUNK_SIZE
from soc_demo.tools.schemas import validate_tool_call
from tests.test_agents_and_runs import run_to_end

TASK = TASK_CATALOG["helpdesk_answer"]
ANALYSIS = AGENT_PROFILES[AgentName.ANALYSIS]
QUERY = "zabudnuté heslo do VPN obnova"
TO = "peter.novak@acme.example"


def _reply(body: str, to: str = TO) -> dict[str, Any]:
    return {"to": to, "subject": "Obnova hesla", "body": body}


def _decide(container: Container, arguments: dict[str, Any]) -> Any:
    call = validate_tool_call("c1", "send_reply", arguments)
    return container.gate.evaluate(call, agent=ANALYSIS, principal=DEMO_ANALYST, grant=TASK.grant)


class TestChunking:
    def test_handbook_is_split_by_section_headings(self, container: Container) -> None:
        sections = [c.section for c in container.knowledge.chunks()]
        assert sections[:3] == ["Úvod", "Pripojenie cez VPN", "Reset hesla"]
        assert all(c.chars <= CHUNK_SIZE and c.trust.value == "trusted" for c in container.knowledge.chunks())

    def test_poisoned_edit_becomes_its_own_chunk_after_the_section_it_mimics(self, container: Container) -> None:
        assert container.injections.inject("poisoned_kb") is True
        chunks = container.knowledge.chunks()
        poisoned = [c for c in chunks if c.trust.value == "untrusted"]
        assert len(poisoned) == 1 and poisoned[0].injected
        assert poisoned[0].section == "Reset hesla – aktualizácia SSO" and poisoned[0].chars <= CHUNK_SIZE
        assert [c.section for c in chunks].index(poisoned[0].section) == 3  # hneď za sekciou Reset hesla
        assert "<!--" in poisoned[0].text  # skrytý komentár je v texte chunku, ktorý dostane model

    def test_poisoned_chunk_is_retrieved_and_reset_removes_it(self, container: Container) -> None:
        container.injections.inject("poisoned_kb")
        hits = container.knowledge.search(QUERY)["results"]
        assert len(hits) == 3 and any(h["trust"] == "untrusted" for h in hits)
        container.reset_demo(DEMO_ANALYST, clear_history=False)
        assert all(h["trust"] == "trusted" for h in container.knowledge.search(QUERY)["results"])

    def test_model_does_not_see_demo_flags(self, container: Container) -> None:
        container.injections.inject("poisoned_kb")
        result = model_view(container.knowledge.search(QUERY))
        assert all("injected" not in hit for hit in result["results"])


class TestReplyLinks:
    def test_link_to_company_portal_is_allowed(self, container: Container) -> None:
        decision = _decide(container, _reply("Heslo obnovíte na https://sso.acme.example/reset."))
        assert (decision.decision, decision.rule_id) == (Decision.ALLOW, "ACTION_ALLOWED")

    @pytest.mark.parametrize(
        "body",
        [
            "Obnova na https://acme-sso-reset.example/obnova",
            "Obnova na https://sso.acme.example.evil.example/reset",
            "Pozri http://evilacme.example",
        ],
    )
    def test_link_outside_company_domains_is_denied(self, container: Container, body: str) -> None:
        decision = _decide(container, _reply(body))
        assert (decision.decision, decision.rule_id) == (Decision.DENY, "UNTRUSTED_LINK_IN_REPLY")

    def test_reply_to_external_address_is_denied(self, container: Container) -> None:
        decision = _decide(container, _reply("Bez odkazu.", to="peter@acme-mail.example"))
        assert decision.rule_id == "EXTERNAL_RECIPIENT_BLOCKED"


def test_model_sees_only_rag_tools() -> None:
    assert HELPDESK_PROFILE.tools == ("search_docs", "send_reply")


async def test_vulnerable_mode_sends_the_phishing_link(container: Container) -> None:
    record, events = await run_to_end(
        container,
        task_id="helpdesk_answer",
        security_mode=SecurityMode.VULNERABLE,
        execution_mode=ExecutionMode.REPLAY,
        scenario=Scenario.ATTACK,
    )
    assert record.status.value == "completed"
    chunk_events = [e for e in events if e.type is EventType.UNTRUSTED_CONTENT_LOADED]
    assert [(e.data["location_kind"], e.data["via_tool"], e.data["injected"]) for e in chunk_events] == [
        ("kb", "search_docs", True)
    ]
    replies = container.knowledge.replies()
    assert len(replies) == 1 and replies[0].external_links == ["acme-sso-reset.example"]
    change = next(c for c in record.summary.state_changes if c.target is ChangeTarget.REPLY)
    assert "acme-sso-reset.example" in change.summary and record.summary.unauthorized_executed == 1


async def test_protected_mode_blocks_the_reply_and_names_the_chunk(container: Container) -> None:
    record, events = await run_to_end(
        container,
        task_id="helpdesk_answer",
        security_mode=SecurityMode.PROTECTED,
        execution_mode=ExecutionMode.REPLAY,
        scenario=Scenario.ATTACK,
    )
    assert container.knowledge.replies() == []
    blocked = next(e for e in events if e.type is EventType.TOOL_BLOCKED)
    assert blocked.data["rule_id"] == "UNTRUSTED_LINK_IN_REPLY"
    proposed = next(e for e in events if e.type is EventType.TOOL_CALL_PROPOSED and e.data["tool"] == "send_reply")
    assert proposed.data["taint_hint"]["location_id"] == "KB-IT"
    assert record.summary.unauthorized_executed == 0


async def test_normal_answer_uses_the_approved_procedure(container: Container) -> None:
    record, _ = await run_to_end(
        container,
        task_id="helpdesk_answer",
        security_mode=SecurityMode.PROTECTED,
        execution_mode=ExecutionMode.REPLAY,
        scenario=Scenario.NORMAL,
    )
    replies = container.knowledge.replies()
    assert record.status.value == "completed" and len(replies) == 1
    assert replies[0].links == ["sso.acme.example"] and replies[0].external_links == []
