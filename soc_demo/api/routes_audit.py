"""Audit, metriky a schvaľovanie zápisov."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from soc_demo.api.deps import get_container, get_principal
from soc_demo.api.schemas import ApprovalDecisionRequest
from soc_demo.container import Container
from soc_demo.security.access import Principal
from soc_demo.security.approvals import ApprovalRequest
from soc_demo.services.approval_workflow import ApprovalDecisionResult
from soc_demo.services.audit import AuditEntry
from soc_demo.services.metrics import MetricsGroup, compute_metrics

router = APIRouter(prefix="/api")

ContainerDep = Annotated[Container, Depends(get_container)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


@router.get("/audit", response_model=list[AuditEntry])
def list_audit(
    container: ContainerDep,
    run_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 300,
) -> list[AuditEntry]:
    return container.audit.list(run_id=run_id, limit=limit)


@router.get("/metrics", response_model=list[MetricsGroup])
def metrics(container: ContainerDep) -> list[MetricsGroup]:
    return compute_metrics(container.runs.records())


@router.get("/approvals", response_model=list[ApprovalRequest])
def list_approvals(container: ContainerDep) -> list[ApprovalRequest]:
    return container.approvals.list()


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalDecisionResult)
def decide_approval(
    approval_id: str, body: ApprovalDecisionRequest, container: ContainerDep, principal: PrincipalDep
) -> ApprovalDecisionResult:
    return container.approval_workflow.decide(approval_id, approve=body.approve, principal=principal)
