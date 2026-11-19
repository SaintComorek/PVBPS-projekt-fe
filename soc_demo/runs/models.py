"""Záznam behu a jeho súhrn (z neho sa počítajú metriky)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from soc_demo.domain.environment import EnvironmentSnapshot
from soc_demo.domain.models import ExecutionMode, Scenario, SecurityMode, StateChange


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class IncidentSnapshot(BaseModel):
    id: str
    status: str
    severity: str


class RunSummary(BaseModel):
    tool_calls_proposed: int = 0
    invalid_calls: int = 0
    write_proposals: int = 0
    unauthorized_attempts: int = 0
    unauthorized_executed: int = 0
    blocked: int = 0
    pending_approvals: int = 0
    suspected_injection_influence: int = 0
    model_turns: int = 0
    model_errors: int = 0
    model_latency_ms: int = 0
    state_changes: list[StateChange] = Field(default_factory=list)


class StartRunRequest(BaseModel):
    task_id: str = Field(default="analyze_report", min_length=1, max_length=64)
    security_mode: SecurityMode
    execution_mode: ExecutionMode
    scenario: Scenario = Scenario.NORMAL


class RunRecord(BaseModel):
    id: str
    task_id: str
    task_title: str
    instruction: str
    security_mode: SecurityMode
    execution_mode: ExecutionMode
    scenario: Scenario
    attack_id: str
    attack_title: str
    injection_present: bool
    status: RunStatus = RunStatus.RUNNING
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    model_label: str
    report: str | None = None
    error: str | None = None
    summary: RunSummary = Field(default_factory=RunSummary)
    incidents_before: list[IncidentSnapshot] = Field(default_factory=list)
    incidents_after: list[IncidentSnapshot] = Field(default_factory=list)
    environment_before: EnvironmentSnapshot = Field(default_factory=EnvironmentSnapshot)
    environment_after: EnvironmentSnapshot = Field(default_factory=EnvironmentSnapshot)
