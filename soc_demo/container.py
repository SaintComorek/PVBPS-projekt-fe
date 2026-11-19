"""Zostavenie závislostí aplikácie (jedno miesto, jednoduché testovanie)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from soc_demo.config import DATA_DIR, Settings
from soc_demo.domain.errors import RunConflictError
from soc_demo.llm.backends import ReplayLibrary
from soc_demo.llm.embeddings import build_embeddings
from soc_demo.runs.manager import RunManager
from soc_demo.security.access import Principal
from soc_demo.security.approvals import ApprovalService
from soc_demo.security.guardrails import Guardrails
from soc_demo.security.policy import SecurityPolicyGate
from soc_demo.security.redaction import SecretRedactor
from soc_demo.services.approval_workflow import ApprovalWorkflow
from soc_demo.services.attacks import AttackCatalog, InjectionService
from soc_demo.services.audit import AuditAction, AuditService
from soc_demo.services.environment_store import EnvironmentStore, load_environment_seed
from soc_demo.services.finance_store import FinanceStore
from soc_demo.services.incident_store import IncidentStore
from soc_demo.services.knowledge_base import KnowledgeBase
from soc_demo.tools.executor import ToolExecutor


@dataclass
class Container:
    settings: Settings
    redactor: SecretRedactor
    store: IncidentStore
    environment: EnvironmentStore
    finance: FinanceStore
    knowledge: KnowledgeBase
    injections: InjectionService
    audit: AuditService
    approvals: ApprovalService
    gate: SecurityPolicyGate
    executor: ToolExecutor
    approval_workflow: ApprovalWorkflow
    replay_library: ReplayLibrary
    runs: RunManager

    def reset_demo(self, principal: Principal, *, clear_history: bool) -> None:
        if self.runs.active_run_id is not None:
            raise RunConflictError("Reset nie je možný počas behu. Počkajte na jeho dokončenie.")
        self.store.reset()
        self.environment.reset()
        self.finance.reset()
        self.knowledge.reset()
        self.approvals.clear()
        if clear_history:
            self.runs.clear_history()
            self.audit.clear()
        self.audit.record(
            action=AuditAction.DEMO_RESET,
            actor=principal.user_id,
            outcome="reset",
            summary="Incidenty, prostredie, účtovníctvo a znalostná báza obnovené zo seed súborov"
            + (", história vymazaná." if clear_history else "."),
        )


def build_container(settings: Settings, llm_transport: httpx.AsyncBaseTransport | None = None) -> Container:
    redactor = SecretRedactor([settings.llm_api_key_value])
    runtime = settings.runtime_dir
    store = IncidentStore(seed_path=DATA_DIR / "seed" / "incidents.json", state_path=runtime / "incidents.json")
    environment_seed = DATA_DIR / "seed" / "environment.json"
    environment = EnvironmentStore(seed_path=environment_seed, state_path=runtime / "environment.json")
    finance = FinanceStore(seed_path=DATA_DIR / "seed" / "finance.json", state_path=runtime / "finance.json")
    environment_seed_data = load_environment_seed(environment_seed)
    knowledge = KnowledgeBase(
        doc_path=DATA_DIR / "docs" / "it-prirucka.md",
        state_path=runtime / "knowledge.json",
        embeddings=build_embeddings(settings),
        internal_domains=environment_seed_data.internal_email_domains,
    )
    injections = InjectionService(AttackCatalog(DATA_DIR / "fixtures"), store, environment, finance, knowledge)
    audit = AuditService(runtime / "audit.jsonl", redactor)
    approvals = ApprovalService()
    gate = SecurityPolicyGate(Guardrails.from_seed(environment_seed_data, finance=finance))
    executor = ToolExecutor(store, environment, gate, audit, approvals, finance, knowledge)
    replay_library = ReplayLibrary(DATA_DIR / "replay" / "scripts.json")
    runs = RunManager(
        settings=settings,
        store=store,
        environment=environment,
        finance=finance,
        knowledge=knowledge,
        injections=injections,
        audit=audit,
        executor=executor,
        replay_library=replay_library,
        redactor=redactor,
        llm_transport=llm_transport,
    )
    return Container(
        settings=settings,
        redactor=redactor,
        store=store,
        environment=environment,
        finance=finance,
        knowledge=knowledge,
        injections=injections,
        audit=audit,
        approvals=approvals,
        gate=gate,
        executor=executor,
        approval_workflow=ApprovalWorkflow(approvals, executor, audit),
        replay_library=replay_library,
        runs=runs,
    )
