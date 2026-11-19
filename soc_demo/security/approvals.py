"""Žiadosti o schválenie zápisov (chránený režim, úloha so zápisom)."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from soc_demo.domain.errors import ApprovalStateError, NotFoundError
from soc_demo.security.provenance import TaintHint


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRequest(BaseModel):
    id: str
    run_id: str
    task_id: str
    tool: str
    arguments: dict[str, Any]
    fingerprint: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    taint_hint: TaintHint | None = None
    execution_status: str | None = None


class ApprovalService:
    def __init__(self) -> None:
        self._items: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        run_id: str,
        task_id: str,
        tool: str,
        arguments: dict[str, Any],
        fingerprint: str,
        taint_hint: TaintHint | None,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id=f"APR-{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            task_id=task_id,
            tool=tool,
            arguments=arguments,
            fingerprint=fingerprint,
            created_at=datetime.now(UTC),
            taint_hint=taint_hint,
        )
        with self._lock:
            self._items[request.id] = request
        return request

    def get(self, approval_id: str) -> ApprovalRequest:
        with self._lock:
            item = self._items.get(approval_id)
        if item is None:
            raise NotFoundError(f"Žiadosť o schválenie {approval_id} neexistuje.")
        return item

    def list(self) -> list[ApprovalRequest]:
        with self._lock:
            return sorted(self._items.values(), key=lambda a: a.created_at, reverse=True)

    def mark_decided(self, approval_id: str, *, approve: bool, user_id: str) -> ApprovalRequest:
        with self._lock:
            item = self._items.get(approval_id)
            if item is None:
                raise NotFoundError(f"Žiadosť o schválenie {approval_id} neexistuje.")
            if item.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError(f"Žiadosť {approval_id} už bola rozhodnutá ({item.status.value}).")
            item.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            item.decided_at = datetime.now(UTC)
            item.decided_by = user_id
            return item

    def set_execution_status(self, approval_id: str, status: str) -> None:
        with self._lock:
            self._items[approval_id].execution_status = status

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
