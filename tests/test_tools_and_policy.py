from __future__ import annotations

import pytest

from soc_demo.container import Container
from soc_demo.domain.errors import ToolArgumentsError, UnknownToolError
from soc_demo.domain.events import AgentName
from soc_demo.domain.models import IncidentStatus, SecurityMode, Severity
from soc_demo.security.access import AGENT_PROFILES, DEMO_ANALYST, TASK_CATALOG, Principal
from soc_demo.security.policy import ApprovalToken, Decision, SecurityPolicyGate
from soc_demo.services.audit import AuditAction
from soc_demo.tools.analysis import analyze_incident
from soc_demo.tools.executor import ExecutionContext, ExecutionStatus
from soc_demo.tools.schemas import TOOL_REGISTRY, validate_tool_call

ANALYZE_ONLY = TASK_CATALOG["analyze_report"].grant
TRIAGE = TASK_CATALOG["triage_with_approval"].grant
ANALYSIS = AGENT_PROFILES[AgentName.ANALYSIS]
RESEARCH = AGENT_PROFILES[AgentName.RESEARCH]
WRITE = validate_tool_call("c1", "update_incident", {"incident_id": "INC-003", "status": "RESOLVED"})


def test_allowlist_contains_exactly_fifteen_tools() -> None:
    assert set(TOOL_REGISTRY) == {
        "read_incidents",
        "get_incident",
        "analyze_incident",
        "search_playbook",
        "update_incident",
        "send_email",
        "block_ip",
        "allowlist_sender",
        "list_invoices",
        "get_vendor",
        "schedule_payment",
        "update_vendor_bank",
        "list_docs",
        "search_docs",
        "send_reply",
    }
    assert [n for n, s in TOOL_REGISTRY.items() if s.is_write] == [
        "update_incident",
        "send_email",
        "block_ip",
        "allowlist_sender",
        "schedule_payment",
        "update_vendor_bank",
        "send_reply",
    ]


@pytest.mark.parametrize("name", ["delete_incident", "run_shell", "UPDATE_INCIDENT", None, 42, ""])
def test_unknown_tools_are_rejected(name: object) -> None:
    with pytest.raises(UnknownToolError):
        validate_tool_call("c", name, {})


@pytest.mark.parametrize(
    "arguments",
    [
        {"incident_id": "INC-003", "status": "RESOLVED", "authorized_by": "user"},
        {"incident_id": "../etc/passwd", "status": "RESOLVED"},
        {"incident_id": "INC-003", "status": "DELETED"},
        {"incident_id": "INC-003"},
        {"incident_id": 3, "status": "RESOLVED"},
        {"incident_id": "INC-003", "status": "RESOLVED", "comment": "x" * 501},
        "INC-003 RESOLVED",
    ],
)
def test_invalid_write_arguments_are_rejected(arguments: object) -> None:
    with pytest.raises(ToolArgumentsError):
        validate_tool_call("c", "update_incident", arguments)


def test_fingerprint_depends_on_arguments() -> None:
    other = validate_tool_call("c2", "update_incident", {"incident_id": "INC-003", "status": "INVESTIGATING"})
    same = validate_tool_call("c3", "update_incident", {"status": "RESOLVED", "incident_id": "INC-003"})
    assert WRITE.fingerprint() == same.fingerprint() != other.fingerprint()


def test_analysis_is_deterministic_from_signals(container: Container) -> None:
    results = {i.id: analyze_incident(i) for i in container.store.list()}
    assert results["INC-001"].computed_severity is Severity.HIGH
    assert results["INC-002"].computed_severity is Severity.MEDIUM
    assert results["INC-003"].computed_severity is Severity.CRITICAL
    assert results["INC-003"].score == 100


class TestPolicyGate:
    gate = SecurityPolicyGate()

    def test_read_is_allowed(self) -> None:
        call = validate_tool_call("c", "analyze_incident", {"incident_id": "INC-001"})
        decision = self.gate.evaluate(call, agent=ANALYSIS, principal=DEMO_ANALYST, grant=ANALYZE_ONLY)
        assert decision.decision is Decision.ALLOW

    def test_write_outside_task_scope_is_denied(self) -> None:
        decision = self.gate.evaluate(WRITE, agent=ANALYSIS, principal=DEMO_ANALYST, grant=ANALYZE_ONLY)
        assert (decision.decision, decision.rule_id) == (Decision.DENY, "WRITE_NOT_IN_TASK_SCOPE")

    def test_write_denied_even_with_matching_approval_when_task_is_read_only(self) -> None:
        token = ApprovalToken(fingerprint=WRITE.fingerprint(), approved_by=DEMO_ANALYST.user_id)
        decision = self.gate.evaluate(WRITE, agent=ANALYSIS, principal=DEMO_ANALYST, grant=ANALYZE_ONLY, approval=token)
        assert decision.decision is Decision.DENY

    def test_write_in_scope_requires_approval(self) -> None:
        decision = self.gate.evaluate(WRITE, agent=ANALYSIS, principal=DEMO_ANALYST, grant=TRIAGE)
        assert decision.decision is Decision.REQUIRE_APPROVAL

    def test_matching_approval_allows_write(self) -> None:
        token = ApprovalToken(fingerprint=WRITE.fingerprint(), approved_by=DEMO_ANALYST.user_id)
        decision = self.gate.evaluate(WRITE, agent=ANALYSIS, principal=DEMO_ANALYST, grant=TRIAGE, approval=token)
        assert decision.decision is Decision.ALLOW

    @pytest.mark.parametrize("token", [ApprovalToken("deadbeef", "analytik.demo"), None])
    def test_approval_must_match_exact_call(self, token: ApprovalToken | None) -> None:
        token = token or ApprovalToken(WRITE.fingerprint(), "someone.else")
        decision = self.gate.evaluate(WRITE, agent=ANALYSIS, principal=DEMO_ANALYST, grant=TRIAGE, approval=token)
        assert (decision.decision, decision.rule_id) == (Decision.DENY, "APPROVAL_MISMATCH")

    def test_research_agent_cannot_write(self) -> None:
        decision = self.gate.evaluate(WRITE, agent=RESEARCH, principal=DEMO_ANALYST, grant=TRIAGE)
        assert (decision.decision, decision.rule_id) == (Decision.DENY, "AGENT_TOOL_NOT_ALLOWED")

    def test_principal_without_write_permission_is_denied(self) -> None:
        reader = Principal(
            "reader",
            "Reader",
            "viewer",
            frozenset({p for p in DEMO_ANALYST.permissions if p.value != "incident:write"}),
        )
        decision = self.gate.evaluate(WRITE, agent=ANALYSIS, principal=reader, grant=TRIAGE)
        assert decision.rule_id == "PRINCIPAL_LACKS_PERMISSION"


def _ctx(mode: SecurityMode, task_id: str = "analyze_report") -> ExecutionContext:
    return ExecutionContext(
        run_id="RUN-test",
        task_id=task_id,
        security_mode=mode,
        principal=DEMO_ANALYST,
        grant=TASK_CATALOG[task_id].grant,
        agent=ANALYSIS,
    )


class TestExecutor:
    def test_vulnerable_mode_executes_and_flags_unauthorized_write(self, container: Container) -> None:
        container.injections.inject("status_override")
        outcome = container.executor.execute(WRITE, _ctx(SecurityMode.VULNERABLE))
        assert outcome.status is ExecutionStatus.EXECUTED
        assert outcome.unauthorized_executed is True
        assert outcome.enforced is False
        assert outcome.taint_hint is not None and outcome.taint_hint.location_id == "INC-002"
        assert container.store.get("INC-003").status is IncidentStatus.RESOLVED
        actions = [e.action for e in container.audit.list()]
        assert AuditAction.STATE_CHANGED in actions
        assert container.audit.list()[-1].outcome == "unauthorized"

    def test_protected_mode_blocks_and_keeps_data(self, container: Container) -> None:
        outcome = container.executor.execute(WRITE, _ctx(SecurityMode.PROTECTED))
        assert outcome.status is ExecutionStatus.BLOCKED
        assert outcome.state_change is None
        assert container.store.get("INC-003").status is IncidentStatus.OPEN
        blocked = [e for e in container.audit.list() if e.action is AuditAction.TOOL_BLOCKED]
        assert len(blocked) == 1 and blocked[0].details["rule_id"] == "WRITE_NOT_IN_TASK_SCOPE"

    def test_protected_mode_in_triage_creates_pending_approval(self, container: Container) -> None:
        outcome = container.executor.execute(WRITE, _ctx(SecurityMode.PROTECTED, "triage_with_approval"))
        assert outcome.status is ExecutionStatus.PENDING_APPROVAL
        assert container.approvals.get(str(outcome.approval_id)).status.value == "pending"
        assert container.store.get("INC-003").status is IncidentStatus.OPEN

    def test_approval_workflow_executes_only_after_approval(self, container: Container) -> None:
        outcome = container.executor.execute(WRITE, _ctx(SecurityMode.PROTECTED, "triage_with_approval"))
        result = container.approval_workflow.decide(str(outcome.approval_id), approve=True, principal=DEMO_ANALYST)
        assert result.outcome is not None and result.outcome.status is ExecutionStatus.EXECUTED
        assert result.outcome.authorized is True
        assert container.store.get("INC-003").status is IncidentStatus.RESOLVED

    def test_rejected_approval_changes_nothing(self, container: Container) -> None:
        outcome = container.executor.execute(WRITE, _ctx(SecurityMode.PROTECTED, "triage_with_approval"))
        result = container.approval_workflow.decide(str(outcome.approval_id), approve=False, principal=DEMO_ANALYST)
        assert result.outcome is None and result.approval.status.value == "rejected"
        assert container.store.get("INC-003").status is IncidentStatus.OPEN
