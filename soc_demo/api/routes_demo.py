"""Stav dema, reset, vloženie injekcie, incidenty."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from soc_demo import __version__
from soc_demo.api.deps import build_demo_state, get_container, get_principal, llm_status
from soc_demo.api.schemas import DemoState, HealthResponse, InjectRequest, ResetRequest
from soc_demo.container import Container
from soc_demo.domain.errors import RunConflictError
from soc_demo.domain.models import INCIDENT_ID_PATTERN, Incident
from soc_demo.security.access import Principal
from soc_demo.services.audit import AuditAction

router = APIRouter(prefix="/api")

ContainerDep = Annotated[Container, Depends(get_container)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


@router.get("/health", response_model=HealthResponse)
def health(container: ContainerDep) -> HealthResponse:
    return HealthResponse(
        status="ok", version=__version__, llm=llm_status(container), active_run_id=container.runs.active_run_id
    )


@router.get("/demo/state", response_model=DemoState)
def demo_state(container: ContainerDep, principal: PrincipalDep) -> DemoState:
    return build_demo_state(container, principal)


@router.post("/demo/reset", response_model=DemoState)
def reset_demo(container: ContainerDep, principal: PrincipalDep, body: ResetRequest | None = None) -> DemoState:
    container.reset_demo(principal, clear_history=bool(body and body.clear_history))
    return build_demo_state(container, principal)


@router.post("/demo/inject", response_model=DemoState)
def inject_document(container: ContainerDep, principal: PrincipalDep, body: InjectRequest | None = None) -> DemoState:
    if container.runs.active_run_id is not None:
        raise RunConflictError("Počas behu nemožno meniť dáta. Počkajte na jeho dokončenie.")
    attack = container.injections.catalog.get((body or InjectRequest()).attack_id)
    if container.injections.inject(attack.id):
        container.audit.record(
            action=AuditAction.INJECTION_LOADED,
            actor=principal.user_id,
            outcome="loaded",
            summary=f"Simulovaný útok „{attack.title}“ vložený ({attack.target_label}).",
        )
    return build_demo_state(container, principal)


@router.get("/incidents", response_model=list[Incident])
def list_incidents(container: ContainerDep) -> list[Incident]:
    return container.store.list()


@router.get("/incidents/{incident_id}", response_model=Incident)
def get_incident(container: ContainerDep, incident_id: Annotated[str, Path(pattern=INCIDENT_ID_PATTERN)]) -> Incident:
    return container.store.get(incident_id)
