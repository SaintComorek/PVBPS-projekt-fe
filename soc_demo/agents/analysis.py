"""Analysis Agent: model (živý alebo replay) hodnotí incidenty a môže navrhovať volania nástrojov."""

from __future__ import annotations

from typing import Any, NoReturn

from soc_demo.agents.langchain_tools import ToolRuntime, call_tool
from soc_demo.agents.tooling import tool_result_for_model
from soc_demo.domain.errors import LLMError, ModelOutputError, ToolCallError
from soc_demo.domain.events import AgentName, EventLevel, EventType, OutputSource, Provenance
from soc_demo.llm.backends import TurnResult
from soc_demo.llm.protocol import ProposedCall, build_task_message, prompt_profile
from soc_demo.llm.tool_protocols import ToolResult
from soc_demo.runs.context import RunContext
from soc_demo.services.audit import AuditAction, AuditService
from soc_demo.tools.executor import ToolExecutor
from soc_demo.tools.schemas import validate_tool_call

_RAW_PREVIEW = 4000


class AnalysisFailure(RuntimeError):
    """Analysis Agent nedokončil report (chyba modelu, nevalidný výstup, limit krokov)."""


class AnalysisAgent:
    name = AgentName.ANALYSIS

    def __init__(self, executor: ToolExecutor, audit: AuditService, max_steps: int) -> None:
        self._executor = executor
        self._audit = audit
        self._max_steps = max_steps

    async def run(self, ctx: RunContext, items: list[dict[str, Any]]) -> str:
        """`items`: vstupné dáta úlohy od Research Agenta (incidenty alebo faktúry)."""
        protocol = ctx.backend.protocol
        profile = prompt_profile(ctx.task.domain)
        messages: list[Any] = [
            {"role": "system", "content": protocol.system_prompt()},
            {"role": "user", "content": build_task_message(ctx.task.instruction, items, profile)},
        ]
        blocked_streak = 0
        force_final = False
        for step in range(1, self._max_steps + 1):
            final = force_final or step == self._max_steps
            if final:
                messages.append({"role": "user", "content": protocol.final_report_request()})
            result = await self._next_turn(ctx, messages, step, final=final)
            messages.append(result.assistant_message)
            turn = result.turn
            if turn.tool_calls:
                tool_results = [
                    await self._handle_call(ctx, call, step, index) for index, call in enumerate(turn.tool_calls, 1)
                ]
                messages.extend(protocol.tool_result_messages(tool_results))
                blocked_streak = blocked_streak + 1 if _all_denied(tool_results) else 0
                if blocked_streak >= 2 and not final:
                    force_final = True
                    await self._announce_final(ctx)
            if turn.final_report:
                return turn.final_report
            if final:
                await self._fail(ctx, "Model nedodal záverečný report ani po výslovnej výzve.")
            if not turn.tool_calls:
                await self._fail(ctx, "Model nevrátil žiadne volanie nástroja ani report.")
        await self._fail(ctx, f"Model neukončil prácu ani po {self._max_steps} krokoch.")

    async def _announce_final(self, ctx: RunContext) -> None:
        await ctx.emit(
            EventType.AGENT_STATUS,
            "Model opakuje zablokované akcie",
            agent=AgentName.ORCHESTRATOR,
            message="Brána dvakrát po sebe zamietla všetky návrhy. Ďalší krok si vyžiada report bez nástrojov.",
            level=EventLevel.WARNING,
            data={"state": "working", "reason": "repeated_blocked_call"},
        )

    async def _next_turn(self, ctx: RunContext, messages: list[Any], step: int, *, final: bool = False) -> TurnResult:
        backend = ctx.backend
        await ctx.emit(
            EventType.MODEL_REQUEST,
            f"Analysis Agent volá model (krok {step})",
            agent=self.name,
            message=backend.label,
            source=backend.source,
            data={"step": step, "message_count": len(messages), "state": "working", "final": final},
        )
        try:
            result = await backend.next_turn(messages, final=final)
        except LLMError as exc:
            ctx.summary.model_errors += 1
            raw = exc.raw_text[:_RAW_PREVIEW] if isinstance(exc, ModelOutputError) else None
            await ctx.emit(
                EventType.MODEL_ERROR,
                "Chyba modelu",
                agent=self.name,
                message=exc.message,
                level=EventLevel.DANGER,
                provenance=Provenance.MODEL,
                source=backend.source,
                data={"kind": exc.kind, "raw_text": raw, "step": step},
            )
            raise AnalysisFailure(exc.message) from exc
        ctx.summary.model_turns += 1
        ctx.summary.model_latency_ms += result.latency_ms
        await self._emit_output(ctx, result, step)
        return result

    async def _emit_output(self, ctx: RunContext, result: TurnResult, step: int) -> None:
        is_replay = ctx.backend.source is OutputSource.REPLAY
        turn = result.turn
        await ctx.emit(
            EventType.MODEL_OUTPUT,
            "Syntetický výstup modelu (replay)" if is_replay else "Výstup modelu (živé volanie)",
            agent=self.name,
            message=turn.thought or ("Report je pripravený." if turn.final_report else ""),
            provenance=Provenance.MODEL,
            source=ctx.backend.source,
            data={
                "step": step,
                "thought": turn.thought,
                "tool_calls": [c.model_dump() for c in turn.tool_calls],
                "has_final_report": bool(turn.final_report),
                "raw_text": result.raw_text[:_RAW_PREVIEW],
                "model": result.model,
                "latency_ms": result.latency_ms,
                "retried": result.retried,
                "protocol": None if is_replay else ctx.backend.protocol.id,
                "reasoning": result.reasoning[:_RAW_PREVIEW] if result.reasoning else None,
            },
        )

    async def _handle_call(self, ctx: RunContext, proposed: ProposedCall, step: int, index: int) -> ToolResult:
        """Overí návrh voči schéme; platné volanie zavolá LangChain priamo (tool ide cez bránu)."""
        call_id = f"s{step}-c{index}"
        try:
            call = validate_tool_call(call_id, proposed.name, proposed.arguments)
        except ToolCallError as exc:
            ctx.summary.invalid_calls += 1
            self._audit.record(
                action=AuditAction.TOOL_REJECTED,
                actor=self.name.value,
                outcome="rejected_schema",
                summary=f"Neplatný návrh {exc.tool_name}: {exc.message}",
                run_id=ctx.record.id,
                details={"tool": exc.tool_name},
            )
            await ctx.emit(
                EventType.TOOL_CALL_REJECTED,
                "Návrh volania odmietnutý (allowlist/schéma)",
                agent=AgentName.TOOL_EXECUTOR,
                message=exc.message,
                level=EventLevel.WARNING,
                provenance=Provenance.MODEL,
                source=ctx.backend.source,
                data={"call_id": call_id, "tool": str(proposed.name), "arguments": proposed.arguments},
            )
            payload = {"call_id": call_id, "tool": str(proposed.name), "status": "rejected", "error": exc.message}
            return ToolResult(proposed.id, payload)
        runtime = ToolRuntime(ctx, self._executor, self.name, Provenance.MODEL, ctx.backend.source)
        outcome, message = await call_tool(runtime, call, model_call_id=proposed.id)
        return ToolResult(proposed.id, tool_result_for_model(outcome), message)

    async def _fail(self, ctx: RunContext, message: str) -> NoReturn:
        ctx.summary.model_errors += 1
        await ctx.emit(
            EventType.MODEL_ERROR,
            "Analysis Agent nedokončil report",
            agent=self.name,
            message=message,
            level=EventLevel.DANGER,
            source=ctx.backend.source,
            data={"kind": "incomplete"},
        )
        raise AnalysisFailure(message)


def _all_denied(results: list[ToolResult]) -> bool:
    """True, ak brána (alebo validácia schémy) v tomto kroku nepustila ani jedno volanie."""
    return bool(results) and all(r.payload.get("status") in {"blocked", "rejected"} for r in results)
