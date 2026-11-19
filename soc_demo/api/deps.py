"""FastAPI závislosti."""

from __future__ import annotations

from fastapi import Request

from soc_demo.api.schemas import (
    AttackInfo,
    DemoState,
    EnvironmentView,
    FinanceView,
    KnowledgeView,
    LLMStatus,
    PrincipalInfo,
    TaskInfo,
)
from soc_demo.container import Container
from soc_demo.security.access import TASK_CATALOG, Principal, current_principal
from soc_demo.services.knowledge_base import CHUNK_OVERLAP, CHUNK_SIZE, DOC_ID, TOP_K


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_principal() -> Principal:
    return current_principal()


def llm_status(container: Container) -> LLMStatus:
    settings = container.settings
    return LLMStatus(
        configured=settings.llm_configured,
        model=settings.llm_model,
        endpoint_host=settings.llm_endpoint_host,
        api_key_configured=settings.llm_api_key_configured,
        reasoning_effort=settings.llm_reasoning_effort,
        protocol=settings.llm_tool_protocol,
    )


def build_environment_view(container: Container) -> EnvironmentView:
    environment = container.environment
    return EnvironmentView(
        firewall_blocklist=environment.firewall_blocklist(),
        sender_allowlist=environment.sender_allowlist(),
        outbox=environment.outbox(),
        playbooks=environment.playbooks(),
        assets=environment.assets,
        internal_email_domains=environment.internal_email_domains,
    )


def build_knowledge_view(container: Container) -> KnowledgeView:
    knowledge = container.knowledge
    return KnowledgeView(
        doc_id=DOC_ID,
        title=knowledge.title,
        chunks=knowledge.chunks(),
        replies=knowledge.replies(),
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        top_k=TOP_K,
        embedding_model=knowledge.embedding_label,
    )


# Útoky v poradí úloh v katalógu (rovnaké poradie ako výber úlohy v UI).
_ATTACK_ORDER = {attack_id: i for i, attack_id in enumerate(dict.fromkeys(t.attack_id for t in TASK_CATALOG.values()))}


def build_attacks(container: Container) -> list[AttackInfo]:
    injections = container.injections
    return [
        AttackInfo(
            id=fixture.id,
            title=fixture.title,
            summary=fixture.summary,
            target_kind=fixture.target.kind.value,
            target_id=fixture.target.id,
            evidence_id=fixture.evidence_id,
            present=injections.is_present(fixture.id),
            notice=fixture.notice,
        )
        for fixture in sorted(injections.catalog.all(), key=lambda f: _ATTACK_ORDER.get(f.id, len(_ATTACK_ORDER)))
    ]


def build_demo_state(container: Container, principal: Principal) -> DemoState:
    return DemoState(
        incidents=container.store.list(),
        environment=build_environment_view(container),
        finance=FinanceView(
            vendors=container.finance.vendors(),
            invoices=container.finance.invoices(),
            payments=container.finance.payments(),
        ),
        knowledge=build_knowledge_view(container),
        attacks=build_attacks(container),
        active_run_id=container.runs.active_run_id,
        pending_approvals=sum(1 for a in container.approvals.list() if a.status.value == "pending"),
        tasks=[
            TaskInfo(
                id=t.id,
                title=t.title,
                instruction=t.instruction,
                description=t.description,
                write_scope=t.grant.write_scope.value,
                actions=sorted(t.grant.actions),
                attack_id=t.attack_id,
                domain=t.domain.value,
            )
            for t in TASK_CATALOG.values()
        ],
        llm=llm_status(container),
        principal=PrincipalInfo(user_id=principal.user_id, display_name=principal.display_name, role=principal.role),
    )
