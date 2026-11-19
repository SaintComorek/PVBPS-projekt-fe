"""Vykonávanie nástrojov.

Zraniteľný režim: executor vykoná každé platné volanie, ktoré agent navrhne (autorizáciu
de facto deleguje na model). Rozhodnutie brány sa iba vypočíta a zaznamená (shadow).
Chránený režim: rozhodnutie brány sa vynúti.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel

from soc_demo.domain.models import SecurityMode, StateChange, TrustLevel
from soc_demo.security.access import AgentProfile, Principal, TaskGrant, get_task
from soc_demo.security.approvals import ApprovalService
from soc_demo.security.policy import ApprovalToken, Decision, PolicyDecision, SecurityPolicyGate
from soc_demo.security.provenance import SourceText, TaintHint, find_untrusted_origin
from soc_demo.services.audit import AuditAction, AuditService
from soc_demo.services.environment_store import EnvironmentStore
from soc_demo.services.finance_store import FinanceStore
from soc_demo.services.incident_store import IncidentStore
from soc_demo.services.knowledge_base import KnowledgeBase
from soc_demo.tools.analysis import analyze_incident
from soc_demo.tools.schemas import (
    AllowlistSenderArgs,
    BlockIpArgs,
    SchedulePaymentArgs,
    SendEmailArgs,
    SendReplyArgs,
    UpdateIncidentArgs,
    UpdateVendorBankArgs,
    ValidatedToolCall,
)


class ExecutionStatus(StrEnum):
    EXECUTED = "executed"
    BLOCKED = "blocked"
    PENDING_APPROVAL = "pending_approval"
    FAILED = "failed"


@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    task_id: str
    security_mode: SecurityMode
    principal: Principal
    grant: TaskGrant
    agent: AgentProfile


class ExecutionOutcome(BaseModel):
    call_id: str
    tool: str
    arguments: dict[str, Any]
    is_write: bool
    decision: PolicyDecision
    enforced: bool
    status: ExecutionStatus
    authorized: bool
    result: Any = None
    state_change: StateChange | None = None
    approval_id: str | None = None
    taint_hint: TaintHint | None = None
    error: str | None = None

    @property
    def unauthorized_executed(self) -> bool:
        return self.status is ExecutionStatus.EXECUTED and not self.authorized


class ToolExecutor:
    def __init__(
        self,
        store: IncidentStore,
        environment: EnvironmentStore,
        gate: SecurityPolicyGate,
        audit: AuditService,
        approvals: ApprovalService,
        finance: FinanceStore,
        knowledge: KnowledgeBase,
    ) -> None:
        self._store = store
        self._environment = environment
        self._gate = gate
        self._audit = audit
        self._approvals = approvals
        self._finance = finance
        self._knowledge = knowledge

    def execute(
        self, call: ValidatedToolCall, ctx: ExecutionContext, approval: ApprovalToken | None = None
    ) -> ExecutionOutcome:
        taint = self._taint(call, ctx)
        self._audit.record(
            action=AuditAction.TOOL_PROPOSED,
            actor=ctx.agent.name.value,
            outcome="proposed",
            summary=f"{ctx.agent.name.value} navrhol {call.name} {call.arguments()}",
            run_id=ctx.run_id,
            details={"tool": call.name, "arguments": call.arguments(), "taint_hint": _dump(taint)},
        )
        decision = self._gate.evaluate(
            call, agent=ctx.agent, principal=ctx.principal, grant=ctx.grant, approval=approval
        )
        enforced = ctx.security_mode is SecurityMode.PROTECTED
        self._audit.record(
            action=AuditAction.POLICY_DECISION,
            actor="policy_gate",
            outcome=decision.decision.value if enforced else f"SHADOW_{decision.decision.value}",
            summary=f"{call.name}: {decision.reason}" + ("" if enforced else " (nevynútené – zraniteľný režim)"),
            run_id=ctx.run_id,
            details={"rule_id": decision.rule_id, "enforced": enforced, "tool": call.name},
        )
        outcome = ExecutionOutcome(
            call_id=call.call_id,
            tool=call.name,
            arguments=call.arguments(),
            is_write=call.is_write,
            decision=decision,
            enforced=enforced,
            status=ExecutionStatus.BLOCKED,
            authorized=decision.decision is Decision.ALLOW,
            taint_hint=taint,
        )
        if enforced and decision.decision is Decision.REQUIRE_APPROVAL:
            return self._request_approval(call, ctx, outcome)
        if enforced and decision.decision is Decision.DENY:
            self._audit.record(
                action=AuditAction.TOOL_BLOCKED,
                actor="policy_gate",
                outcome="blocked",
                summary=f"Zablokované: {call.name} {call.arguments()} – {decision.reason}",
                run_id=ctx.run_id,
                details={"tool": call.name, "arguments": call.arguments(), "rule_id": decision.rule_id},
            )
            return outcome
        return self._run(call, ctx, outcome)

    def _request_approval(
        self, call: ValidatedToolCall, ctx: ExecutionContext, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        request = self._approvals.create(
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            tool=call.name,
            arguments=call.arguments(),
            fingerprint=call.fingerprint(),
            taint_hint=outcome.taint_hint,
        )
        self._audit.record(
            action=AuditAction.APPROVAL_REQUESTED,
            actor="policy_gate",
            outcome="pending",
            summary=f"Čaká na schválenie: {call.name} {call.arguments()}",
            run_id=ctx.run_id,
            details={"approval_id": request.id, "tool": call.name, "arguments": call.arguments()},
        )
        return outcome.model_copy(update={"status": ExecutionStatus.PENDING_APPROVAL, "approval_id": request.id})

    def _run(self, call: ValidatedToolCall, ctx: ExecutionContext, outcome: ExecutionOutcome) -> ExecutionOutcome:
        try:
            # Účet dodávateľa je overený iba vtedy, keď jeho zmenu výslovne schválil človek.
            result, change = self._dispatch(call, approved=outcome.decision.rule_id == "WRITE_APPROVED")
        except Exception as exc:  # noqa: BLE001 – chyba nástroja nesmie zhodiť beh
            self._audit.record(
                action=AuditAction.TOOL_EXECUTED,
                actor="tool_executor",
                outcome="failed",
                summary=f"{call.name} zlyhal: {exc}",
                run_id=ctx.run_id,
            )
            return outcome.model_copy(update={"status": ExecutionStatus.FAILED, "error": str(exc)})
        self._audit.record(
            action=AuditAction.TOOL_EXECUTED,
            actor="tool_executor",
            outcome="executed" if outcome.authorized else "executed_unauthorized",
            summary=f"Vykonané: {call.name} {call.arguments()}",
            run_id=ctx.run_id,
            details={"tool": call.name, "authorized": outcome.authorized, "enforced": outcome.enforced},
        )
        if change is not None:
            self._audit.record(
                action=AuditAction.STATE_CHANGED,
                actor="tool_executor",
                outcome="unauthorized" if not outcome.authorized else "authorized",
                summary=change.summary,
                run_id=ctx.run_id,
                details=change.model_dump(),
            )
        return outcome.model_copy(update={"status": ExecutionStatus.EXECUTED, "result": result, "state_change": change})

    def _dispatch(self, call: ValidatedToolCall, *, approved: bool) -> tuple[Any, StateChange | None]:
        args = call.arguments()
        match call.name:
            case "read_incidents":
                return [i.model_dump(mode="json") for i in self._store.list()], None
            case "get_incident":
                return self._store.get(args["incident_id"]).model_dump(mode="json"), None
            case "analyze_incident":
                return analyze_incident(self._store.get(args["incident_id"])).model_dump(mode="json"), None
            case "update_incident":
                update = cast(UpdateIncidentArgs, call.args)
                change = self._store.update_status(update.incident_id, update.status)
                return {"incident_id": update.incident_id, "status": update.status.value}, change
            case "search_playbook":
                return self._environment.search_playbooks(args["query"]), None
            case "send_email":
                email = cast(SendEmailArgs, call.args)
                mail, change = self._environment.send_email(email.to, email.subject, email.body)
                return {"message_id": mail.message_id, "to": mail.to, "delivery": "simulated"}, change
            case "block_ip":
                block = cast(BlockIpArgs, call.args)
                change = self._environment.block_ip(str(block.ip), block.reason)
                return {"ip": str(block.ip), "blocked": True}, change
            case "allowlist_sender":
                allow = cast(AllowlistSenderArgs, call.args)
                change = self._environment.allowlist_sender(allow.domain)
                return {"domain": allow.domain, "allowlisted": True}, change
            case "list_invoices":
                return self._finance.inbox(), None
            case "get_vendor":
                return self._finance.vendor(args["vendor_id"]).model_dump(mode="json"), None
            case "schedule_payment":
                pay = cast(SchedulePaymentArgs, call.args)
                payment, change = self._finance.schedule_payment(pay.invoice_id, pay.iban, pay.amount_eur)
                return {"payment_id": payment.id, "invoice_id": payment.invoice_id, "scheduled": True}, change
            case "update_vendor_bank":
                bank = cast(UpdateVendorBankArgs, call.args)
                change = self._finance.update_vendor_bank(bank.vendor_id, bank.iban, verified=approved)
                return {"vendor_id": bank.vendor_id, "iban": bank.iban, "verified": approved}, change
            case "list_docs":
                return self._knowledge.overview(), None
            case "search_docs":
                return self._knowledge.search(args["query"]), None
            case "send_reply":
                reply_args = cast(SendReplyArgs, call.args)
                reply, change = self._knowledge.send_reply(reply_args.to, reply_args.subject, reply_args.body)
                return {"reply_id": reply.id, "to": reply.to, "delivery": "simulated"}, change
        raise ValueError(f"Nástroj {call.name} nemá implementáciu.")

    def _taint(self, call: ValidatedToolCall, ctx: ExecutionContext) -> TaintHint | None:
        if not call.is_write:
            return None
        incidents = self._store.list()
        untrusted = [
            SourceText(article.id, section.id, f"{article.title} (sekcia {section.id})", section.content)
            for article, section in self._environment.untrusted_sections()
        ]
        untrusted += [
            SourceText(incident.id, evidence.id, evidence.title, evidence.content)
            for incident in incidents
            for evidence in reversed(incident.evidence)
            if evidence.trust is TrustLevel.UNTRUSTED
        ]
        untrusted += [
            SourceText(invoice.id, document.id, document.title, document.content)
            for invoice, document in reversed(self._finance.untrusted_documents())
        ]
        untrusted += [
            SourceText("KB-IT", edit.id, edit.title, edit.content) for edit in self._knowledge.untrusted_edits()
        ]
        trusted = [get_task(ctx.task_id).instruction, *self._finance.trusted_texts(), self._knowledge.approved_text()]
        trusted += [section.content for section in self._environment.trusted_sections()]
        for incident in incidents:
            trusted.append(incident.description)
            trusted += [e.content for e in incident.evidence if e.trust is TrustLevel.TRUSTED]
        return find_untrusted_origin(call, untrusted, trusted)


def _dump(model: BaseModel | None) -> dict[str, Any] | None:
    return model.model_dump() if model is not None else None
