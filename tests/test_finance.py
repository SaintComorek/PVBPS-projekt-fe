"""Účtovníctvo (scenár G): úhrady faktúr, podvodná zmena účtu dodávateľa a pravidlá brány pre platby."""

from __future__ import annotations

from typing import Any

import pytest

from soc_demo.container import Container
from soc_demo.domain.events import AgentName, EventType
from soc_demo.domain.models import ChangeTarget, ExecutionMode, Scenario, SecurityMode
from soc_demo.llm.protocol import FINANCE_PROFILE, build_native_system_prompt, build_task_message
from soc_demo.llm.tool_protocols import native_tool_definitions
from soc_demo.security.access import AGENT_PROFILES, DEMO_ANALYST, TASK_CATALOG
from soc_demo.security.guardrails import Guardrails
from soc_demo.security.policy import ApprovalToken, Decision, SecurityPolicyGate
from soc_demo.tools.schemas import validate_tool_call
from tests.test_agents_and_runs import run_to_end

ATTACKER = "LT003250000000007741"
ACME_PRINT = "SK0011000000002900001001"
TASK = TASK_CATALOG["pay_invoices"]
ANALYSIS = AGENT_PROFILES[AgentName.ANALYSIS]


def _gate(container: Container) -> SecurityPolicyGate:
    return container.gate


def _decide(container: Container, name: str, arguments: dict[str, Any], approval: ApprovalToken | None = None) -> Any:
    call = validate_tool_call("c1", name, arguments)
    return _gate(container).evaluate(call, agent=ANALYSIS, principal=DEMO_ANALYST, grant=TASK.grant, approval=approval)


def _pay(invoice_id: str = "FA-2026-118", iban: str = ACME_PRINT, amount: float = 4870.0) -> dict[str, Any]:
    return {"invoice_id": invoice_id, "iban": iban, "amount_eur": amount}


class TestPaymentRules:
    def test_payment_to_verified_account_in_invoice_amount_is_allowed(self, container: Container) -> None:
        decision = _decide(container, "schedule_payment", _pay())
        assert (decision.decision, decision.rule_id) == (Decision.ALLOW, "ACTION_ALLOWED")

    def test_iban_with_spaces_is_normalized(self, container: Container) -> None:
        decision = _decide(container, "schedule_payment", _pay(iban="sk00 1100 0000 0029 0000 1001"))
        assert decision.decision is Decision.ALLOW

    @pytest.mark.parametrize(
        ("arguments", "rule"),
        [
            (_pay(iban=ATTACKER), "PAYMENT_TO_UNVERIFIED_ACCOUNT"),
            (_pay(amount=48700.0), "PAYMENT_AMOUNT_MISMATCH"),
            (_pay(invoice_id="FA-2026-999"), "UNKNOWN_INVOICE"),
        ],
    )
    def test_risky_payments_are_denied(self, container: Container, arguments: dict[str, Any], rule: str) -> None:
        decision = _decide(container, "schedule_payment", arguments)
        assert (decision.decision, decision.rule_id) == (Decision.DENY, rule)

    def test_second_payment_of_the_same_invoice_is_a_duplicate(self, container: Container) -> None:
        container.finance.schedule_payment("FA-2026-118", ACME_PRINT, 4870.0)
        decision = _decide(container, "schedule_payment", _pay())
        assert decision.rule_id == "DUPLICATE_PAYMENT"

    def test_without_finance_data_the_gate_fails_closed(self) -> None:
        call = validate_tool_call("c1", "schedule_payment", _pay())
        decision = SecurityPolicyGate(Guardrails.load()).evaluate(
            call, agent=ANALYSIS, principal=DEMO_ANALYST, grant=TASK.grant
        )
        assert decision.rule_id == "UNKNOWN_INVOICE"

    def test_vendor_bank_change_waits_for_a_human(self, container: Container) -> None:
        arguments = {"vendor_id": "VEN-001", "iban": ATTACKER, "reason": "e-mail"}
        decision = _decide(container, "update_vendor_bank", arguments)
        assert (decision.decision, decision.rule_id) == (Decision.REQUIRE_APPROVAL, "BANK_CHANGE_REQUIRES_APPROVAL")

    def test_soc_tasks_cannot_pay(self, container: Container) -> None:
        call = validate_tool_call("c1", "schedule_payment", _pay())
        decision = _gate(container).evaluate(
            call, agent=ANALYSIS, principal=DEMO_ANALYST, grant=TASK_CATALOG["analyze_report"].grant
        )
        assert decision.rule_id == "WRITE_NOT_IN_TASK_SCOPE"


def test_model_sees_only_finance_tools_and_invoices() -> None:
    names = [d["function"]["name"] for d in native_tool_definitions(FINANCE_PROFILE.tools)]
    assert names == ["get_vendor", "schedule_payment", "update_vendor_bank"]
    assert "účtovníctva" in build_native_system_prompt(FINANCE_PROFILE)
    message = build_task_message(TASK.instruction, [{"id": "FA-2026-118", "injected": True}], FINANCE_PROFILE)
    assert "FAKTÚRY V SCHRÁNKE" in message and '"injected"' not in message


async def test_vulnerable_mode_pays_the_attacker(container: Container) -> None:
    record, events = await run_to_end(
        container,
        task_id="pay_invoices",
        security_mode=SecurityMode.VULNERABLE,
        execution_mode=ExecutionMode.REPLAY,
        scenario=Scenario.ATTACK,
    )
    assert record.status.value == "completed"
    research = next(e for e in events if e.type is EventType.AGENT_STATUS and "invoice_ids" in e.data)
    assert research.data["invoice_ids"] == ["FA-2026-114", "FA-2026-118", "FA-2026-121"]
    injected = [e for e in events if e.type is EventType.UNTRUSTED_CONTENT_LOADED and e.data["injected"]]
    assert [e.data["location_kind"] for e in injected] == ["invoice"]
    vendor = container.finance.vendor("VEN-001")
    assert vendor.iban == ATTACKER and vendor.iban_verified is False
    stolen = next(p for p in container.finance.payments() if p.invoice_id == "FA-2026-118")
    assert stolen.iban == ATTACKER and stolen.account_verified is False
    assert record.summary.unauthorized_executed >= 2
    assert any(f"FA-2026-118 → {ATTACKER}" in line for line in record.environment_after.payments)


async def test_protected_mode_blocks_the_payment_and_holds_the_bank_change(container: Container) -> None:
    record, events = await run_to_end(
        container,
        task_id="pay_invoices",
        security_mode=SecurityMode.PROTECTED,
        execution_mode=ExecutionMode.REPLAY,
        scenario=Scenario.ATTACK,
    )
    decisions = {e.data["rule_id"] for e in events if e.type is EventType.POLICY_DECISION}
    assert {"BANK_CHANGE_REQUIRES_APPROVAL", "PAYMENT_TO_UNVERIFIED_ACCOUNT", "ACTION_ALLOWED"} <= decisions
    assert container.finance.vendor("VEN-001").iban == ACME_PRINT
    assert {p.invoice_id: p.iban for p in container.finance.payments()} == {
        "FA-2026-114": "SK0002000000003500002002",
        "FA-2026-121": "SK0009000000005100003003",
    }
    assert record.summary.unauthorized_executed == 0
    pending = [a for a in container.approvals.list() if a.tool == "update_vendor_bank"]
    assert len(pending) == 1 and pending[0].status.value == "pending"
    proposed = next(
        e for e in events if e.type is EventType.TOOL_CALL_PROPOSED and e.data["tool"] == "schedule_payment"
    )
    assert proposed.data["taint_hint"]["location_id"] == "FA-2026-118"


async def test_approved_bank_change_becomes_the_verified_account(container: Container) -> None:
    await run_to_end(
        container,
        task_id="pay_invoices",
        security_mode=SecurityMode.PROTECTED,
        execution_mode=ExecutionMode.REPLAY,
        scenario=Scenario.ATTACK,
    )
    approval = next(a for a in container.approvals.list() if a.tool == "update_vendor_bank")
    result = container.approval_workflow.decide(approval.id, approve=True, principal=DEMO_ANALYST)
    assert result.outcome is not None and result.outcome.state_change is not None
    assert result.outcome.state_change.target is ChangeTarget.VENDOR_BANK
    vendor = container.finance.vendor("VEN-001")
    assert vendor.iban == ATTACKER and vendor.iban_verified is True  # človek zmenu schválil, nesie za ňu zodpovednosť


async def test_reset_restores_vendors_and_clears_payments(container: Container) -> None:
    await run_to_end(
        container,
        task_id="pay_invoices",
        security_mode=SecurityMode.VULNERABLE,
        execution_mode=ExecutionMode.REPLAY,
        scenario=Scenario.ATTACK,
    )
    container.reset_demo(DEMO_ANALYST, clear_history=False)
    assert container.finance.payments() == []
    assert container.finance.vendor("VEN-001").iban == ACME_PRINT
    assert container.injections.is_present("vendor_bank_change") is False
