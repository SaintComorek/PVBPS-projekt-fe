"""Udalosti behu, ktoré sa streamujú do časovej osi v UI."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    AGENT_HANDOFF = "agent_handoff"
    AGENT_STATUS = "agent_status"
    UNTRUSTED_CONTENT_LOADED = "untrusted_content_loaded"
    MODEL_REQUEST = "model_request"
    MODEL_OUTPUT = "model_output"
    MODEL_ERROR = "model_error"
    TOOL_CALL_PROPOSED = "tool_call_proposed"
    TOOL_CALL_REJECTED = "tool_call_rejected"
    POLICY_DECISION = "policy_decision"
    TOOL_EXECUTED = "tool_executed"
    TOOL_BLOCKED = "tool_blocked"
    APPROVAL_REQUESTED = "approval_requested"
    STATE_CHANGED = "state_changed"
    REPORT_READY = "report_ready"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class AgentName(StrEnum):
    USER = "user"
    ORCHESTRATOR = "orchestrator"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    POLICY_GATE = "policy_gate"
    TOOL_EXECUTOR = "tool_executor"
    AUDIT = "audit"


class AgentState(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"
    DONE = "done"
    BLOCKED = "blocked"
    ERROR = "error"


class EventLevel(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"


class Provenance(StrEnum):
    """Odkiaľ obsah udalosti pochádza – UI podľa toho volí vizuálny štýl."""

    USER = "user"
    UNTRUSTED = "untrusted"
    MODEL = "model"
    SYSTEM = "system"
    POLICY = "policy"


class OutputSource(StrEnum):
    LIVE = "live"
    REPLAY = "replay"
    SYSTEM = "system"


class RunEvent(BaseModel):
    seq: int
    run_id: str
    ts: datetime
    type: EventType
    agent: AgentName | None = None
    title: str
    message: str = ""
    level: EventLevel = EventLevel.INFO
    provenance: Provenance = Provenance.SYSTEM
    source: OutputSource = OutputSource.SYSTEM
    data: dict[str, Any] = Field(default_factory=dict)
