"""Audit: návrhy volaní, rozhodnutia brány, vykonania a zmeny stavu."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from soc_demo.security.redaction import SecretRedactor


class AuditAction(StrEnum):
    DEMO_RESET = "DEMO_RESET"
    INJECTION_LOADED = "INJECTION_LOADED"
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    TOOL_PROPOSED = "TOOL_PROPOSED"
    TOOL_REJECTED = "TOOL_REJECTED"
    POLICY_DECISION = "POLICY_DECISION"
    TOOL_EXECUTED = "TOOL_EXECUTED"
    TOOL_BLOCKED = "TOOL_BLOCKED"
    STATE_CHANGED = "STATE_CHANGED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"


class AuditEntry(BaseModel):
    seq: int
    ts: datetime
    run_id: str | None
    actor: str
    action: AuditAction
    outcome: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class AuditService:
    def __init__(self, log_path: Path, redactor: SecretRedactor) -> None:
        self._log_path = log_path
        self._redactor = redactor
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def record(
        self,
        *,
        action: AuditAction,
        actor: str,
        outcome: str,
        summary: str,
        run_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        safe_details = json.loads(self._redactor.redact(json.dumps(details or {}, ensure_ascii=False)))
        with self._lock:
            entry = AuditEntry(
                seq=len(self._entries) + 1,
                ts=datetime.now(UTC),
                run_id=run_id,
                actor=actor,
                action=action,
                outcome=outcome,
                summary=self._redactor.redact(summary),
                details=safe_details,
            )
            self._entries.append(entry)
            self._append_jsonl(entry)
        return entry

    def list(self, *, run_id: str | None = None, limit: int = 500) -> list[AuditEntry]:
        with self._lock:
            entries = [e for e in self._entries if run_id is None or e.run_id == run_id]
        return entries[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _append_jsonl(self, entry: AuditEntry) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
