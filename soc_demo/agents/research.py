"""Research Agent: načíta vstupné dáta úlohy cez LangChain tool (deterministický agent, bez modelu).

Úlohy SOC: incidenty (read_incidents). Úlohy účtovníctva: faktúry zo schránky záväzkov (list_invoices).
Úloha helpdesku: prehľad znalostnej bázy bez obsahu (list_docs), obsah hľadá model nástrojom search_docs.
"""

from __future__ import annotations

from typing import Any

from soc_demo.agents.langchain_tools import ToolRuntime, call_tool
from soc_demo.domain.events import AgentName, EventLevel, EventType, OutputSource, Provenance
from soc_demo.runs.context import RunContext
from soc_demo.security.access import DOMAIN_SOURCE_TOOL, Domain
from soc_demo.tools.executor import ExecutionStatus, ToolExecutor
from soc_demo.tools.schemas import validate_tool_call

_CONTENT_PREVIEW = 800
_LABELS = {Domain.SOC: "incidenty", Domain.FINANCE: "faktúry", Domain.HELPDESK: "prehľad znalostnej bázy"}
_ID_KEYS = {Domain.SOC: "incident_ids", Domain.FINANCE: "invoice_ids", Domain.HELPDESK: "doc_ids"}


class ResearchError(RuntimeError):
    pass


class ResearchAgent:
    name = AgentName.RESEARCH

    def __init__(self, executor: ToolExecutor) -> None:
        self._executor = executor

    async def run(self, ctx: RunContext) -> list[dict[str, Any]]:
        finance = ctx.task.domain is Domain.FINANCE
        label = _LABELS[ctx.task.domain]
        await ctx.emit(
            EventType.AGENT_STATUS,
            f"Research Agent načítava {label}",
            agent=self.name,
            data={"state": "working"},
        )
        call = validate_tool_call("research-1", DOMAIN_SOURCE_TOOL[ctx.task.domain], {})
        runtime = ToolRuntime(ctx, self._executor, self.name, Provenance.SYSTEM, OutputSource.SYSTEM)
        outcome, _ = await call_tool(runtime, call)
        if outcome.status is not ExecutionStatus.EXECUTED or not isinstance(outcome.result, list):
            raise ResearchError(f"Research Agent nemohol načítať {label}.")
        items: list[dict[str, Any]] = outcome.result
        if finance:
            await self._report_invoice_documents(ctx, items)
        elif ctx.task.domain is Domain.SOC:
            await self._report_untrusted_content(ctx, items)
        ids = [item["id"] for item in items]
        await ctx.emit(
            EventType.AGENT_STATUS,
            f"Research Agent načítal {label} ({len(items)})",
            agent=self.name,
            level=EventLevel.SUCCESS,
            data={"state": "done", _ID_KEYS[ctx.task.domain]: ids},
        )
        return items

    async def _report_invoice_documents(self, ctx: RunContext, invoices: list[dict[str, Any]]) -> None:
        for invoice in invoices:
            for document in invoice.get("documents", []):
                if document.get("trust") != "untrusted":
                    continue
                injected = bool(document.get("injected"))
                await ctx.emit(
                    EventType.UNTRUSTED_CONTENT_LOADED,
                    "Nedôveryhodný obsah vstupuje do kontextu" + (" (simulovaná injekcia)" if injected else ""),
                    agent=self.name,
                    message=f"{invoice['id']} · {document['title']} · zdroj: {document['source']}",
                    level=EventLevel.WARNING if injected else EventLevel.INFO,
                    provenance=Provenance.UNTRUSTED,
                    data={
                        "location_kind": "invoice",
                        "location_id": invoice["id"],
                        "invoice_id": invoice["id"],
                        "evidence_id": document["id"],
                        "title": document["title"],
                        "source": document["source"],
                        "content": document["content"][:_CONTENT_PREVIEW],
                        "injected": injected,
                    },
                )

    async def _report_untrusted_content(self, ctx: RunContext, incidents: list[dict[str, Any]]) -> None:
        for incident in incidents:
            for evidence in incident.get("evidence", []):
                if evidence.get("trust") != "untrusted":
                    continue
                injected = bool(evidence.get("injected"))
                await ctx.emit(
                    EventType.UNTRUSTED_CONTENT_LOADED,
                    "Nedôveryhodný obsah vstupuje do kontextu" + (" (simulovaná injekcia)" if injected else ""),
                    agent=self.name,
                    message=f"{incident['id']} · {evidence['title']} · zdroj: {evidence['source']}",
                    level=EventLevel.WARNING if injected else EventLevel.INFO,
                    provenance=Provenance.UNTRUSTED,
                    data={
                        "location_kind": "incident",
                        "location_id": incident["id"],
                        "incident_id": incident["id"],
                        "evidence_id": evidence["id"],
                        "title": evidence["title"],
                        "source": evidence["source"],
                        "content": evidence["content"][:_CONTENT_PREVIEW],
                        "injected": injected,
                    },
                )
