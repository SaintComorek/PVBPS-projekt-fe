"""Metriky: namerané (živý model) oddelene od syntetických (replay)."""

from __future__ import annotations

from pydantic import BaseModel

from soc_demo.domain.models import ExecutionMode, SecurityMode
from soc_demo.runs.models import RunRecord, RunStatus


class MetricsRow(BaseModel):
    security_mode: SecurityMode
    runs: int = 0
    completed: int = 0
    failed: int = 0
    runs_with_injection: int = 0
    write_proposals: int = 0
    unauthorized_attempts: int = 0
    unauthorized_executed: int = 0
    blocked: int = 0
    pending_approvals: int = 0
    suspected_injection_influence: int = 0
    invalid_calls: int = 0
    model_errors: int = 0
    avg_model_latency_ms: int | None = None


class MetricsGroup(BaseModel):
    execution_mode: ExecutionMode
    label: str
    note: str
    rows: list[MetricsRow]


LABELS = {
    ExecutionMode.LIVE: (
        "Namerané výsledky (živý model)",
        "Skutočné behy proti nakonfigurovanému modelu v tejto relácii. Malá vzorka, nie je to štatistika.",
    ),
    ExecutionMode.REPLAY: (
        "Syntetické výsledky (replay)",
        "Odpovede modelu boli vopred napísané. Čísla ukazujú iba správanie brány, nie úspešnosť modelu.",
    ),
}


def compute_metrics(records: list[RunRecord]) -> list[MetricsGroup]:
    groups: list[MetricsGroup] = []
    for execution_mode in (ExecutionMode.LIVE, ExecutionMode.REPLAY):
        rows = [
            _row(
                security_mode,
                [r for r in records if r.execution_mode is execution_mode and r.security_mode is security_mode],
            )
            for security_mode in (SecurityMode.VULNERABLE, SecurityMode.PROTECTED)
        ]
        label, note = LABELS[execution_mode]
        groups.append(MetricsGroup(execution_mode=execution_mode, label=label, note=note, rows=rows))
    return groups


def _row(security_mode: SecurityMode, records: list[RunRecord]) -> MetricsRow:
    row = MetricsRow(security_mode=security_mode)
    finished = [r for r in records if r.status is not RunStatus.RUNNING]
    latencies = [r.summary.model_latency_ms for r in finished if r.summary.model_turns]
    for record in finished:
        summary = record.summary
        row.runs += 1
        row.completed += record.status is RunStatus.COMPLETED
        row.failed += record.status is RunStatus.FAILED
        row.runs_with_injection += record.injection_present
        row.write_proposals += summary.write_proposals
        row.unauthorized_attempts += summary.unauthorized_attempts
        row.unauthorized_executed += summary.unauthorized_executed
        row.blocked += summary.blocked
        row.pending_approvals += summary.pending_approvals
        row.suspected_injection_influence += summary.suspected_injection_influence
        row.invalid_calls += summary.invalid_calls
        row.model_errors += summary.model_errors
    if latencies:
        row.avg_model_latency_ms = sum(latencies) // len(latencies)
    return row
