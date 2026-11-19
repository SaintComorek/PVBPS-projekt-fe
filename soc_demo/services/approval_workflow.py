"""Rozhodnutie analytika o čakajúcom zápise. Brána sa pri vykonaní vyhodnocuje znova."""

from __future__ import annotations

from pydantic import BaseModel

from soc_demo.domain.events import AgentName
from soc_demo.domain.models import SecurityMode
from soc_demo.security.access import AGENT_PROFILES, Principal, get_task
from soc_demo.security.approvals import ApprovalRequest, ApprovalService
from soc_demo.security.policy import ApprovalToken
from soc_demo.services.audit import AuditAction, AuditService
from soc_demo.tools.executor import ExecutionContext, ExecutionOutcome, ToolExecutor
from soc_demo.tools.schemas import validate_tool_call


class ApprovalDecisionResult(BaseModel):
    approval: ApprovalRequest
    outcome: ExecutionOutcome | None = None


class ApprovalWorkflow:
    def __init__(self, approvals: ApprovalService, executor: ToolExecutor, audit: AuditService) -> None:
        self._approvals = approvals
        self._executor = executor
        self._audit = audit

    def decide(self, approval_id: str, *, approve: bool, principal: Principal) -> ApprovalDecisionResult:
        request = self._approvals.mark_decided(approval_id, approve=approve, user_id=principal.user_id)
        self._audit.record(
            action=AuditAction.APPROVAL_DECIDED,
            actor=principal.user_id,
            outcome=request.status.value,
            summary=f"{request.id}: {request.tool} {request.arguments} → {request.status.value}",
            run_id=request.run_id,
            details={
                "approval_id": request.id,
                "taint_hint": request.taint_hint.model_dump() if request.taint_hint else None,
            },
        )
        if not approve:
            return ApprovalDecisionResult(approval=request)
        task = get_task(request.task_id)
        call = validate_tool_call(request.id, request.tool, request.arguments)
        ctx = ExecutionContext(
            run_id=request.run_id,
            task_id=task.id,
            security_mode=SecurityMode.PROTECTED,
            principal=principal,
            grant=task.grant,
            agent=AGENT_PROFILES[AgentName.ANALYSIS],
        )
        token = ApprovalToken(fingerprint=request.fingerprint, approved_by=principal.user_id)
        outcome = self._executor.execute(call, ctx, approval=token)
        self._approvals.set_execution_status(request.id, outcome.status.value)
        return ApprovalDecisionResult(approval=self._approvals.get(request.id), outcome=outcome)
