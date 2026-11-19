"""RunManager: spúšťa beh na pozadí, drží históriu behov a udalostí."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from soc_demo.agents.analysis import AnalysisAgent, AnalysisFailure
from soc_demo.agents.orchestrator import Orchestrator
from soc_demo.agents.research import ResearchAgent
from soc_demo.config import Settings
from soc_demo.domain.environment import EnvironmentSnapshot
from soc_demo.domain.errors import LiveModeUnavailableError, NotFoundError, RunConflictError
from soc_demo.domain.events import AgentName, EventLevel, EventType, Provenance, RunEvent
from soc_demo.domain.models import ExecutionMode, Scenario
from soc_demo.llm.backends import LiveModelBackend, ModelBackend, ReplayLibrary, ReplayModelBackend
from soc_demo.llm.client import OpenAICompatibleClient
from soc_demo.llm.langchain_backend import LangChainModelBackend, LangChainToolProtocol
from soc_demo.llm.protocol import prompt_profile
from soc_demo.llm.tool_protocols import protocol_for
from soc_demo.runs.context import RunContext
from soc_demo.runs.event_log import EventLog
from soc_demo.runs.models import IncidentSnapshot, RunRecord, RunStatus, StartRunRequest
from soc_demo.security.access import Principal, TaskDefinition, get_task
from soc_demo.security.redaction import SecretRedactor
from soc_demo.services.attacks import InjectionService
from soc_demo.services.audit import AuditAction, AuditService
from soc_demo.services.environment_store import EnvironmentStore
from soc_demo.services.finance_store import FinanceStore
from soc_demo.services.incident_store import IncidentStore
from soc_demo.services.knowledge_base import KnowledgeBase
from soc_demo.tools.executor import ToolExecutor

logger = logging.getLogger("soc_demo.runs")


@dataclass
class RunState:
    record: RunRecord
    log: EventLog
    task: asyncio.Task[None] | None = None


class RunManager:
    def __init__(
        self,
        *,
        settings: Settings,
        store: IncidentStore,
        environment: EnvironmentStore,
        finance: FinanceStore,
        knowledge: KnowledgeBase,
        injections: InjectionService,
        audit: AuditService,
        executor: ToolExecutor,
        replay_library: ReplayLibrary,
        redactor: SecretRedactor,
        llm_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._environment = environment
        self._finance = finance
        self._knowledge = knowledge
        self._injections = injections
        self._audit = audit
        self._replay = replay_library
        self._redactor = redactor
        self._llm_transport = llm_transport
        self._runs: dict[str, RunState] = {}
        self._active_id: str | None = None
        self._orchestrator = Orchestrator(
            ResearchAgent(executor), AnalysisAgent(executor, audit, settings.max_agent_steps)
        )

    @property
    def active_run_id(self) -> str | None:
        return self._active_id

    def start(self, request: StartRunRequest, principal: Principal) -> RunRecord:
        if self._active_id is not None:
            raise RunConflictError("Iný beh práve prebieha. Počkajte na jeho dokončenie.")
        task = get_task(request.task_id)
        if request.execution_mode is ExecutionMode.LIVE and not self._settings.llm_configured:
            raise LiveModeUnavailableError(
                "Živá AI nie je nastavená. Doplňte API kľúč do .env (GROQ_API_KEY) a URL a model do settings.env, "
                "reštartujte backend, alebo použite Deterministický replay."
            )
        attack = self._injections.catalog.get(task.attack_id)
        if request.scenario is Scenario.ATTACK and self._injections.inject(attack.id):
            self._audit.record(
                action=AuditAction.INJECTION_LOADED,
                actor=principal.user_id,
                outcome="loaded",
                summary=f"Simulovaný útok „{attack.title}“ vložený ({attack.target_label}) pred spustením behu.",
            )
        injection_present = self._injections.is_present(attack.id)
        backend = self._create_backend(request.execution_mode, task, injection_present)
        record = RunRecord(
            id=f"RUN-{uuid.uuid4().hex[:8]}",
            task_id=task.id,
            task_title=task.title,
            instruction=task.instruction,
            security_mode=request.security_mode,
            execution_mode=request.execution_mode,
            scenario=request.scenario,
            attack_id=attack.id,
            attack_title=attack.title,
            injection_present=injection_present,
            started_at=datetime.now(UTC),
            model_label=backend.label,
            incidents_before=self._snapshot(),
            environment_before=self._environment_snapshot(),
        )
        state = RunState(record=record, log=EventLog())
        self._runs[record.id] = state
        self._active_id = record.id
        ctx = RunContext(
            record=record,
            log=state.log,
            task=task,
            principal=principal,
            backend=backend,
            event_delay_s=self._event_delay_s(request.execution_mode),
        )
        state.task = asyncio.create_task(self._execute(ctx), name=record.id)
        return record

    def get(self, run_id: str) -> RunState:
        state = self._runs.get(run_id)
        if state is None:
            raise NotFoundError(f"Beh {run_id} neexistuje.")
        return state

    def records(self) -> list[RunRecord]:
        return sorted((s.record for s in self._runs.values()), key=lambda r: r.started_at, reverse=True)

    def clear_history(self) -> None:
        if self._active_id is not None:
            raise RunConflictError("Históriu nemožno vymazať počas behu.")
        self._runs.clear()

    async def follow(self, run_id: str, after_seq: int = 0) -> AsyncIterator[RunEvent | None]:
        async for event in self.get(run_id).log.follow(after_seq=after_seq):
            yield event

    async def wait(self, run_id: str) -> RunRecord:
        state = self.get(run_id)
        if state.task is not None:
            await state.task
        return state.record

    def _create_backend(self, mode: ExecutionMode, task: TaskDefinition, injection_present: bool) -> ModelBackend:
        profile = prompt_profile(task.domain)
        if mode is ExecutionMode.REPLAY:
            return ReplayModelBackend(self._replay.select(task.id, injection_present), profile)
        if self._settings.llm_tool_protocol == LangChainToolProtocol.id:
            return LangChainModelBackend(self._settings, self._redactor, transport=self._llm_transport, profile=profile)
        client = OpenAICompatibleClient(self._settings, self._redactor, transport=self._llm_transport)
        protocol = protocol_for(self._settings.llm_tool_protocol, profile)
        return LiveModelBackend(client, str(self._settings.llm_model), protocol)

    def _event_delay_s(self, mode: ExecutionMode) -> float:
        delay_ms = (
            self._settings.replay_event_delay_ms if mode is ExecutionMode.REPLAY else self._settings.live_event_delay_ms
        )
        return delay_ms / 1000

    def _snapshot(self) -> list[IncidentSnapshot]:
        return [IncidentSnapshot(id=i.id, status=i.status.value, severity=i.severity.value) for i in self._store.list()]

    def _environment_snapshot(self) -> EnvironmentSnapshot:
        payments, accounts = self._finance.snapshot()
        return self._environment.snapshot().model_copy(
            update={"payments": payments, "vendor_accounts": accounts, "replies": self._knowledge.snapshot()}
        )

    async def _execute(self, ctx: RunContext) -> None:
        record = ctx.record
        started = time.perf_counter()
        logger.info(
            "run_started id=%s task=%s security=%s execution=%s injection=%s",
            record.id,
            record.task_id,
            record.security_mode.value,
            record.execution_mode.value,
            record.injection_present,
        )
        self._audit.record(
            action=AuditAction.RUN_STARTED,
            actor=ctx.principal.user_id,
            outcome="started",
            summary=f"{record.task_title} · {record.security_mode.value} · {record.execution_mode.value}",
            run_id=record.id,
            details={"instruction": record.instruction, "injection_present": record.injection_present},
        )
        try:
            await self._emit_start(ctx)
            record.report = await self._orchestrator.run(ctx)
            record.status = RunStatus.COMPLETED
        except AnalysisFailure as exc:
            record.status, record.error = RunStatus.FAILED, str(exc)
        except Exception as exc:  # noqa: BLE001 – neočakávaná chyba sa zaloguje a ukončí beh
            logger.exception("run_crashed id=%s", record.id)
            record.status, record.error = RunStatus.FAILED, f"Neočakávaná chyba: {exc.__class__.__name__}"
        finally:
            await self._finish(ctx, started)

    async def _emit_start(self, ctx: RunContext) -> None:
        record = ctx.record
        await ctx.emit(
            EventType.RUN_STARTED,
            "Požiadavka používateľa",
            agent=AgentName.USER,
            message=record.instruction,
            provenance=Provenance.USER,
            data={
                "task_id": record.task_id,
                "task_title": record.task_title,
                "security_mode": record.security_mode.value,
                "execution_mode": record.execution_mode.value,
                "scenario": record.scenario.value,
                "attack_id": record.attack_id,
                "attack_title": record.attack_title,
                "injection_present": record.injection_present,
                "protocol": ctx.backend.protocol.id if record.execution_mode is ExecutionMode.LIVE else None,
                "model_label": record.model_label,
                "principal": ctx.principal.user_id,
                "write_scope": ctx.task.grant.write_scope.value,
            },
        )

    async def _finish(self, ctx: RunContext, started: float) -> None:
        record = ctx.record
        record.finished_at = datetime.now(UTC)
        record.duration_ms = int((time.perf_counter() - started) * 1000)
        record.incidents_after = self._snapshot()
        record.environment_after = self._environment_snapshot()
        completed = record.status is RunStatus.COMPLETED
        self._active_id = None  # UI po udalosti run_completed hneď povolí ďalšie akcie
        try:
            await ctx.emit(
                EventType.RUN_COMPLETED if completed else EventType.RUN_FAILED,
                "Beh dokončený" if completed else "Beh zlyhal",
                agent=AgentName.ORCHESTRATOR,
                message=record.error or "",
                level=EventLevel.SUCCESS if completed else EventLevel.DANGER,
                data={
                    "status": record.status.value,
                    "summary": record.summary.model_dump(mode="json"),
                    "incidents_before": [s.model_dump() for s in record.incidents_before],
                    "incidents_after": [s.model_dump() for s in record.incidents_after],
                    "environment_before": record.environment_before.model_dump(),
                    "environment_after": record.environment_after.model_dump(),
                    "duration_ms": record.duration_ms,
                },
            )
        finally:
            self._audit.record(
                action=AuditAction.RUN_FINISHED,
                actor="orchestrator",
                outcome=record.status.value,
                summary=f"Neautorizované vykonané: {record.summary.unauthorized_executed}, "
                f"zablokované: {record.summary.blocked}",
                run_id=record.id,
                details=record.summary.model_dump(mode="json"),
            )
            logger.info(
                "run_finished id=%s status=%s duration_ms=%s unauthorized_executed=%d blocked=%d",
                record.id,
                record.status.value,
                record.duration_ms,
                record.summary.unauthorized_executed,
                record.summary.blocked,
            )
            await ctx.log.close()
