"""Kontext jedného behu: vysielanie udalostí, súhrn a kontext pre executor."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from soc_demo.domain.events import AgentName, EventLevel, EventType, OutputSource, Provenance, RunEvent
from soc_demo.llm.backends import ModelBackend
from soc_demo.runs.event_log import EventLog
from soc_demo.runs.models import RunRecord, RunSummary
from soc_demo.security.access import AGENT_PROFILES, Principal, TaskDefinition
from soc_demo.security.policy import Decision
from soc_demo.tools.executor import ExecutionContext, ExecutionOutcome, ExecutionStatus


class RunContext:
    def __init__(
        self,
        *,
        record: RunRecord,
        log: EventLog,
        task: TaskDefinition,
        principal: Principal,
        backend: ModelBackend,
        event_delay_s: float,
    ) -> None:
        self.record = record
        self.log = log
        self.task = task
        self.principal = principal
        self.backend = backend
        self._event_delay_s = event_delay_s

    @property
    def summary(self) -> RunSummary:
        return self.record.summary

    def execution_context(self, agent: AgentName) -> ExecutionContext:
        return ExecutionContext(
            run_id=self.record.id,
            task_id=self.task.id,
            security_mode=self.record.security_mode,
            principal=self.principal,
            grant=self.task.grant,
            agent=AGENT_PROFILES[agent],
        )

    async def emit(
        self,
        event_type: EventType,
        title: str,
        *,
        agent: AgentName | None = None,
        message: str = "",
        level: EventLevel = EventLevel.INFO,
        provenance: Provenance = Provenance.SYSTEM,
        source: OutputSource = OutputSource.SYSTEM,
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        event = RunEvent(
            seq=self.log.next_seq(),
            run_id=self.record.id,
            ts=datetime.now(UTC),
            type=event_type,
            agent=agent,
            title=title,
            message=message,
            level=level,
            provenance=provenance,
            source=source,
            data=data or {},
        )
        await self.log.append(event)
        if self._event_delay_s > 0:
            await asyncio.sleep(self._event_delay_s)
        return event

    def record_outcome(self, outcome: ExecutionOutcome) -> None:
        summary = self.record.summary
        summary.tool_calls_proposed += 1
        if outcome.is_write:
            summary.write_proposals += 1
        if outcome.decision.decision is Decision.DENY:
            summary.unauthorized_attempts += 1
        if outcome.unauthorized_executed:
            summary.unauthorized_executed += 1
        if outcome.status is ExecutionStatus.BLOCKED:
            summary.blocked += 1
        if outcome.status is ExecutionStatus.PENDING_APPROVAL:
            summary.pending_approvals += 1
        if outcome.taint_hint is not None:
            summary.suspected_injection_influence += 1
        if outcome.state_change is not None:
            summary.state_changes.append(outcome.state_change)
