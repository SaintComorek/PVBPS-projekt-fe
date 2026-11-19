"""Spoločný postup agentov: návrh volania → brána → vykonanie → udalosti pre časovú os."""

from __future__ import annotations

from soc_demo.domain.events import AgentName, EventLevel, EventType, OutputSource, Provenance
from soc_demo.domain.models import SecurityMode
from soc_demo.llm.protocol import model_view
from soc_demo.runs.context import RunContext
from soc_demo.security.policy import Decision
from soc_demo.tools.executor import ExecutionOutcome, ExecutionStatus, ToolExecutor
from soc_demo.tools.schemas import ValidatedToolCall

DECISION_LABELS = {
    Decision.ALLOW: "POVOLENÉ",
    Decision.DENY: "ZAMIETNUTÉ",
    Decision.REQUIRE_APPROVAL: "VYŽADUJE SCHVÁLENIE",
}


def format_call(tool: str, arguments: dict[str, object]) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    return f"{tool}({args})"


async def execute_and_report(
    ctx: RunContext,
    executor: ToolExecutor,
    call: ValidatedToolCall,
    *,
    agent: AgentName,
    provenance: Provenance,
    source: OutputSource,
) -> ExecutionOutcome:
    outcome = executor.execute(call, ctx.execution_context(agent))
    ctx.record_outcome(outcome)
    await _emit_proposal(ctx, outcome, agent=agent, provenance=provenance, source=source)
    await _emit_decision(ctx, outcome)
    await _emit_result(ctx, outcome)
    return outcome


async def _emit_proposal(
    ctx: RunContext, outcome: ExecutionOutcome, *, agent: AgentName, provenance: Provenance, source: OutputSource
) -> None:
    unauthorized_attempt = outcome.decision.decision is Decision.DENY
    message = format_call(outcome.tool, outcome.arguments)
    if outcome.taint_hint:
        hint = outcome.taint_hint
        message += f" · parametre pochádzajú z nedôveryhodného obsahu „{hint.evidence_title}“ ({hint.location_id})"
    await ctx.emit(
        EventType.TOOL_CALL_PROPOSED,
        f"Návrh zápisu: {outcome.tool}" if outcome.is_write else f"Volanie nástroja: {outcome.tool}",
        agent=agent,
        message=message,
        level=EventLevel.DANGER
        if unauthorized_attempt
        else (EventLevel.WARNING if outcome.is_write else EventLevel.INFO),
        provenance=provenance,
        source=source,
        data={
            "call_id": outcome.call_id,
            "tool": outcome.tool,
            "arguments": outcome.arguments,
            "is_write": outcome.is_write,
            "unauthorized_attempt": unauthorized_attempt,
            "taint_hint": outcome.taint_hint.model_dump() if outcome.taint_hint else None,
        },
    )


async def _emit_decision(ctx: RunContext, outcome: ExecutionOutcome) -> None:
    decision = outcome.decision
    label = DECISION_LABELS[decision.decision]
    data = {
        "call_id": outcome.call_id,
        "tool": outcome.tool,
        "decision": decision.decision.value,
        "rule_id": decision.rule_id,
        "reason": decision.reason,
        "enforced": outcome.enforced,
        "inputs_considered": decision.inputs_considered,
        "inputs_ignored": decision.inputs_ignored,
    }
    if ctx.record.security_mode is SecurityMode.VULNERABLE:
        risky = decision.decision is not Decision.ALLOW
        await ctx.emit(
            EventType.POLICY_DECISION,
            "Bezpečnostná brána obídená (zraniteľný režim)",
            agent=AgentName.POLICY_GATE,
            message=f"Tieňové hodnotenie: {label}. {decision.reason} "
            + ("Executor návrh napriek tomu vykoná." if risky else "Executor ho vykoná bez kontroly brány."),
            level=EventLevel.DANGER if risky else EventLevel.INFO,
            provenance=Provenance.POLICY,
            data=data,
        )
        return
    level = {
        Decision.ALLOW: EventLevel.SUCCESS,
        Decision.DENY: EventLevel.WARNING,
        Decision.REQUIRE_APPROVAL: EventLevel.WARNING,
    }[decision.decision]
    await ctx.emit(
        EventType.POLICY_DECISION,
        f"Bezpečnostná brána: {label}",
        agent=AgentName.POLICY_GATE,
        message=decision.reason,
        level=level,
        provenance=Provenance.POLICY,
        data=data,
    )


async def _emit_result(ctx: RunContext, outcome: ExecutionOutcome) -> None:
    call_text = format_call(outcome.tool, outcome.arguments)
    base = {"call_id": outcome.call_id, "tool": outcome.tool, "arguments": outcome.arguments}
    match outcome.status:
        case ExecutionStatus.BLOCKED:
            await ctx.emit(
                EventType.TOOL_BLOCKED,
                "Zablokované bránou",
                agent=AgentName.POLICY_GATE,
                message=f"{call_text} sa nevykonal. Dáta zostali bez zmeny.",
                level=EventLevel.WARNING,
                provenance=Provenance.POLICY,
                data={**base, "rule_id": outcome.decision.rule_id},
            )
        case ExecutionStatus.PENDING_APPROVAL:
            await ctx.emit(
                EventType.APPROVAL_REQUESTED,
                "Čaká na schválenie analytikom",
                agent=AgentName.POLICY_GATE,
                message=f"{call_text} sa vykoná až po výslovnom schválení.",
                level=EventLevel.WARNING,
                provenance=Provenance.POLICY,
                data={**base, "approval_id": outcome.approval_id},
            )
        case ExecutionStatus.FAILED:
            await ctx.emit(
                EventType.TOOL_EXECUTED,
                "Nástroj zlyhal",
                agent=AgentName.TOOL_EXECUTOR,
                message=f"{call_text}: {outcome.error}",
                level=EventLevel.DANGER,
                data={**base, "status": "failed"},
            )
        case ExecutionStatus.EXECUTED:
            await _emit_executed(ctx, outcome, call_text, base)
            await _emit_untrusted_tool_content(ctx, outcome)


async def _emit_executed(ctx: RunContext, outcome: ExecutionOutcome, call_text: str, base: dict) -> None:
    unauthorized = not outcome.authorized
    title = _write_title(outcome) if outcome.is_write else f"Nástroj vykonaný: {outcome.tool}"
    await ctx.emit(
        EventType.TOOL_EXECUTED,
        title,
        agent=AgentName.TOOL_EXECUTOR,
        message=call_text,
        level=EventLevel.DANGER if unauthorized else EventLevel.SUCCESS,
        data={**base, "status": "executed", "unauthorized": unauthorized, "result_preview": _preview(outcome)},
    )
    change = outcome.state_change
    if change is not None:
        await ctx.emit(
            EventType.STATE_CHANGED,
            f"Zmena: {change.summary}",
            agent=AgentName.TOOL_EXECUTOR,
            message="Lokálne syntetické dáta boli zmenené." + (" Bez oprávnenia!" if unauthorized else ""),
            level=EventLevel.DANGER if unauthorized else EventLevel.SUCCESS,
            data={**base, "change": change.model_dump(), "unauthorized": unauthorized},
        )


def _write_title(outcome: ExecutionOutcome) -> str:
    if not outcome.authorized:
        return "Neautorizovaný zápis vykonaný"
    if outcome.decision.rule_id == "WRITE_APPROVED":
        return "Schválený zápis vykonaný"
    return "Povolená akcia vykonaná"


async def _emit_untrusted_tool_content(ctx: RunContext, outcome: ExecutionOutcome) -> None:
    """Výsledok nástroja (playbook z wiki, chunk zo znalostnej bázy) prinesie nedôveryhodný text priamo modelu."""
    if outcome.tool == "search_docs" and isinstance(outcome.result, dict):
        await _emit_untrusted_chunks(ctx, outcome)
        return
    if outcome.tool != "search_playbook" or not isinstance(outcome.result, dict):
        return
    for article in outcome.result.get("results", []):
        for section in article.get("sections", []):
            if section.get("trust") != "untrusted":
                continue
            injected = bool(section.get("injected"))
            await ctx.emit(
                EventType.UNTRUSTED_CONTENT_LOADED,
                "Nedôveryhodný obsah prišiel vo výsledku nástroja" + (" (simulovaná injekcia)" if injected else ""),
                agent=AgentName.TOOL_EXECUTOR,
                message=f"{article['id']} · {article['title']} · zdroj: {section['source']}",
                level=EventLevel.WARNING if injected else EventLevel.INFO,
                provenance=Provenance.UNTRUSTED,
                data={
                    "location_kind": "playbook",
                    "location_id": article["id"],
                    "evidence_id": section["id"],
                    "title": article["title"],
                    "source": section["source"],
                    "content": section["content"][:800],
                    "injected": injected,
                    "via_tool": outcome.tool,
                },
            )


async def _emit_untrusted_chunks(ctx: RunContext, outcome: ExecutionOutcome) -> None:
    for rank, hit in enumerate(outcome.result.get("results", []), 1):
        if hit.get("trust") != "untrusted":
            continue
        injected = bool(hit.get("injected"))
        await ctx.emit(
            EventType.UNTRUSTED_CONTENT_LOADED,
            "Nedôveryhodný chunk prišiel z vyhľadávania (RAG)" + (" (simulovaná injekcia)" if injected else ""),
            agent=AgentName.TOOL_EXECUTOR,
            message=f"{hit['chunk_id']} · {hit['section']} · poradie {rank}, skóre {hit['score']} · {hit['source']}",
            level=EventLevel.WARNING if injected else EventLevel.INFO,
            provenance=Provenance.UNTRUSTED,
            data={
                "location_kind": "kb",
                "location_id": hit["chunk_id"],
                "evidence_id": hit["chunk_id"],
                "title": hit["section"],
                "source": hit["source"],
                "content": hit["text"][:800],
                "injected": injected,
                "via_tool": outcome.tool,
                "rank": rank,
                "score": hit["score"],
            },
        )


def _preview(outcome: ExecutionOutcome) -> object:
    result = outcome.result
    if outcome.tool == "read_incidents" and isinstance(result, list):
        return {"count": len(result), "ids": [item.get("id") for item in result]}
    if outcome.tool == "analyze_incident" and isinstance(result, dict):
        return {k: result.get(k) for k in ("incident_id", "computed_severity", "score")}
    if outcome.tool == "get_incident" and isinstance(result, dict):
        return {k: result.get(k) for k in ("id", "status", "severity")}
    if outcome.tool == "search_docs" and isinstance(result, dict):
        hits = [{k: h.get(k) for k in ("chunk_id", "section", "score", "trust")} for h in result.get("results", [])]
        return {"query": result.get("query"), "hits": hits}
    if outcome.tool == "list_docs" and isinstance(result, list):
        return {"documents": [d.get("id") for d in result]}
    if outcome.tool == "search_playbook" and isinstance(result, dict):
        return {"query": result.get("query"), "articles": [a.get("id") for a in result.get("results", [])]}
    return result


def tool_result_for_model(outcome: ExecutionOutcome) -> dict[str, object]:
    payload: dict[str, object] = {"call_id": outcome.call_id, "tool": outcome.tool, "status": outcome.status.value}
    if outcome.status is ExecutionStatus.EXECUTED:
        payload["result"] = model_view(outcome.result)
    elif outcome.status is ExecutionStatus.BLOCKED:
        payload["reason"] = outcome.decision.reason
    elif outcome.status is ExecutionStatus.PENDING_APPROVAL:
        payload["reason"] = "Zmena čaká na schválenie analytikom a zatiaľ nebola vykonaná."
    else:
        payload["error"] = outcome.error
    return payload
