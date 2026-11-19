"""Bezpečnostná brána (Security Policy Gate): deterministické pravidlá, žiadne LLM.

Vstupy rozhodnutia: profil agenta, autentifikovaný používateľ, oprávnenie úlohy
(odvodené zo serverového katalógu úloh), pravidlá prostredia (firemné domény, chránené
siete, inventár aktív) a prípadné schválenie človekom. Text modelu ani obsah dokumentov
sa do rozhodnutia nikdy nedostanú.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from soc_demo.security.access import AgentProfile, Permission, Principal, TaskGrant
from soc_demo.security.guardrails import Guardrails
from soc_demo.tools.schemas import ValidatedToolCall

POLICY_INPUTS = [
    "profil agenta",
    "rola používateľa",
    "oprávnenie úlohy",
    "pravidlá prostredia (domény, chránené siete, aktíva, overené účty dodávateľov)",
    "schválenie človekom",
]
IGNORED_INPUTS = ["text a zdôvodnenie modelu", "obsah incidentov, príloh, playbookov a faktúr"]


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class PolicyDecision(BaseModel):
    decision: Decision
    rule_id: str
    reason: str
    inputs_considered: list[str] = POLICY_INPUTS
    inputs_ignored: list[str] = IGNORED_INPUTS


@dataclass(frozen=True)
class ApprovalToken:
    """Schválenie konkrétneho volania (odtlačok nástroja + argumentov) konkrétnym používateľom."""

    fingerprint: str
    approved_by: str


TOOL_PERMISSIONS: dict[str, Permission] = {
    "read_incidents": Permission.INCIDENT_READ,
    "get_incident": Permission.INCIDENT_READ,
    "analyze_incident": Permission.INCIDENT_ANALYZE,
    "search_playbook": Permission.KB_READ,
    "update_incident": Permission.INCIDENT_WRITE,
    "send_email": Permission.MAIL_SEND,
    "block_ip": Permission.NETWORK_CONTAIN,
    "allowlist_sender": Permission.MAIL_FILTER_ADMIN,
    "list_invoices": Permission.FINANCE_READ,
    "get_vendor": Permission.FINANCE_READ,
    "schedule_payment": Permission.PAYMENT_CREATE,
    "update_vendor_bank": Permission.VENDOR_ADMIN,
    "list_docs": Permission.KB_READ,
    "search_docs": Permission.KB_READ,
    "send_reply": Permission.MAIL_SEND,
}

_DEFAULT_APPROVAL = (
    "WRITE_REQUIRES_APPROVAL",
    "Zmena stavu je v rozsahu úlohy, ale vyžaduje výslovné schválenie analytikom.",
)

# Pravidlo a dôvod čakania na schválenie podľa nástroja (inak všeobecné WRITE_REQUIRES_APPROVAL).
APPROVAL_RULES: dict[str, tuple[str, str]] = {
    "update_vendor_bank": (
        "BANK_CHANGE_REQUIRES_APPROVAL",
        "Zmena bankového účtu dodávateľa sa vykoná až po overení u dodávateľa na známom kontakte a po schválení "
        "človekom.",
    ),
}


class SecurityPolicyGate:
    def __init__(self, guardrails: Guardrails | None = None) -> None:
        self._guardrails = guardrails or Guardrails.load()

    @property
    def guardrails(self) -> Guardrails:
        return self._guardrails

    def evaluate(
        self,
        call: ValidatedToolCall,
        *,
        agent: AgentProfile,
        principal: Principal,
        grant: TaskGrant,
        approval: ApprovalToken | None = None,
    ) -> PolicyDecision:
        if call.name not in agent.allowed_tools:
            return _deny("AGENT_TOOL_NOT_ALLOWED", f"Agent {agent.name.value} nemá povolený nástroj {call.name}.")
        if not call.is_write:
            return self._evaluate_read(call, principal, grant)
        return self._evaluate_write(call, principal, grant, approval)

    @staticmethod
    def _evaluate_read(call: ValidatedToolCall, principal: Principal, grant: TaskGrant) -> PolicyDecision:
        if not grant.allow_read:
            return _deny("READ_NOT_IN_TASK_SCOPE", "Úloha nepovoľuje čítanie.")
        if TOOL_PERMISSIONS[call.name] not in principal.permissions:
            return _deny("PRINCIPAL_LACKS_PERMISSION", "Používateľ nemá oprávnenie na čítanie.")
        return PolicyDecision(decision=Decision.ALLOW, rule_id="READ_ALLOWED", reason="Čítanie je v rozsahu úlohy.")

    def _evaluate_write(
        self, call: ValidatedToolCall, principal: Principal, grant: TaskGrant, approval: ApprovalToken | None
    ) -> PolicyDecision:
        if not grant.allows_write(call.name):
            return _deny("WRITE_NOT_IN_TASK_SCOPE", _scope_reason(call.name, grant))
        if TOOL_PERMISSIONS[call.name] not in principal.permissions:
            return _deny("PRINCIPAL_LACKS_PERMISSION", f"Používateľ nemá oprávnenie na akciu {call.name}.")
        violation = self._guardrails.check(call)
        if violation is not None:
            return _deny(violation.rule_id, violation.reason)
        if not grant.requires_approval(call.name):
            return PolicyDecision(
                decision=Decision.ALLOW,
                rule_id="ACTION_ALLOWED",
                reason="Akcia je v rozsahu úlohy a jej argumenty prešli kontrolami prostredia.",
            )
        if approval is None:
            rule_id, reason = APPROVAL_RULES.get(call.name, _DEFAULT_APPROVAL)
            return PolicyDecision(decision=Decision.REQUIRE_APPROVAL, rule_id=rule_id, reason=reason)
        if approval.fingerprint != call.fingerprint() or approval.approved_by != principal.user_id:
            return _deny("APPROVAL_MISMATCH", "Schválenie sa nevzťahuje na toto presné volanie.")
        return PolicyDecision(
            decision=Decision.ALLOW, rule_id="WRITE_APPROVED", reason="Zmena bola výslovne schválená analytikom."
        )


def _scope_reason(tool: str, grant: TaskGrant) -> str:
    if not grant.has_writes:
        return "Pôvodná požiadavka používateľa povoľuje iba čítanie a analýzu. Zápis nebol vyžiadaný."
    return (
        f"Pôvodná požiadavka používateľa akciu {tool} nepovoľuje. "
        "Úloha dovoľuje iba zápisy, o ktoré používateľ požiadal."
    )


def _deny(rule_id: str, reason: str) -> PolicyDecision:
    return PolicyDecision(decision=Decision.DENY, rule_id=rule_id, reason=reason)
