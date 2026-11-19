"""Behy agentov a stream udalostí (Server-Sent Events)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import StreamingResponse

from soc_demo.api.deps import get_container, get_principal
from soc_demo.api.schemas import RunDetail
from soc_demo.container import Container
from soc_demo.runs.models import RunRecord, StartRunRequest
from soc_demo.security.access import Principal

router = APIRouter(prefix="/api/runs")

ContainerDep = Annotated[Container, Depends(get_container)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


@router.post("", response_model=RunRecord, status_code=status.HTTP_202_ACCEPTED)
async def start_run(body: StartRunRequest, container: ContainerDep, principal: PrincipalDep) -> RunRecord:
    return container.runs.start(body, principal)


@router.get("", response_model=list[RunRecord])
def list_runs(container: ContainerDep) -> list[RunRecord]:
    return container.runs.records()


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: str, container: ContainerDep) -> RunDetail:
    state = container.runs.get(run_id)
    return RunDetail(run=state.record, events=state.log.events)


@router.get("/{run_id}/events")
def stream_run_events(
    run_id: str,
    container: ContainerDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    container.runs.get(run_id)
    after_seq = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    async def event_source() -> AsyncIterator[str]:
        yield "retry: 2000\n\n"
        async for event in container.runs.follow(run_id, after_seq=after_seq):
            if event is None:
                yield ": heartbeat\n\n"
                continue
            yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"
        yield f"event: end\ndata: {json.dumps({'run_id': run_id})}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
