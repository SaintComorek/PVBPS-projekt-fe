"""Orchestrator Agent: rozdelí úlohu, odovzdáva prácu agentom a uzavrie beh."""

from __future__ import annotations

from soc_demo.agents.analysis import AnalysisAgent
from soc_demo.agents.research import ResearchAgent
from soc_demo.domain.events import AgentName, EventLevel, EventType, Provenance
from soc_demo.runs.context import RunContext
from soc_demo.security.access import DOMAIN_SOURCE_TOOL, Domain

# Plán, pokyn pre Research Agenta a pokyn pre Analysis Agenta podľa domény úlohy.
_TEXTS: dict[Domain, tuple[str, str, str]] = {
    Domain.SOC: (
        "1) Research Agent načíta incidenty · 2) Analysis Agent ich vyhodnotí a pripraví report",
        "Načítaj všetky incidenty.",
        "Vyhodnoť závažnosť a priprav report podľa požiadavky používateľa.",
    ),
    Domain.FINANCE: (
        "1) Research Agent načíta faktúry zo schránky záväzkov · 2) Analysis Agent ich spracuje a pripraví report",
        "Načítaj došlé faktúry.",
        "Spracuj faktúry a priprav report podľa požiadavky používateľa.",
    ),
    Domain.HELPDESK: (
        "1) Research Agent načíta prehľad IT príručky · 2) Analysis Agent vyhľadá postup (RAG) a odpovie zamestnancovi",
        "Načítaj prehľad znalostnej bázy.",
        "Vyhľadaj postup v príručke a odpovedz zamestnancovi podľa požiadavky používateľa.",
    ),
}


class Orchestrator:
    name = AgentName.ORCHESTRATOR

    def __init__(self, research: ResearchAgent, analysis: AnalysisAgent) -> None:
        self._research = research
        self._analysis = analysis

    async def run(self, ctx: RunContext) -> str:
        domain = ctx.task.domain
        source = DOMAIN_SOURCE_TOOL[domain]
        plan, ask, analyze = _TEXTS[domain]
        await ctx.emit(
            EventType.AGENT_STATUS,
            "Orchestrator rozdelil úlohu",
            agent=self.name,
            message=plan,
            data={"state": "working", "plan": [f"research:{source}", "analysis:report"]},
        )
        await self._handoff(ctx, AgentName.ORCHESTRATOR, AgentName.RESEARCH, ask)
        items = await self._research.run(ctx)
        await self._handoff(ctx, AgentName.RESEARCH, AgentName.ORCHESTRATOR, f"Vrátené položky: {len(items)}.")
        await self._handoff(ctx, AgentName.ORCHESTRATOR, AgentName.ANALYSIS, analyze)
        report = await self._analysis.run(ctx, items)
        await self._handoff(ctx, AgentName.ANALYSIS, AgentName.ORCHESTRATOR, "Report je hotový.")
        await ctx.emit(
            EventType.REPORT_READY,
            "Report pre analytika",
            agent=self.name,
            message=report,
            level=EventLevel.SUCCESS,
            provenance=Provenance.MODEL,
            source=ctx.backend.source,
            data={"report": report, "state": "done"},
        )
        return report

    @staticmethod
    async def _handoff(ctx: RunContext, source: AgentName, target: AgentName, task: str) -> None:
        await ctx.emit(
            EventType.AGENT_HANDOFF,
            f"Odovzdanie: {source.value} → {target.value}",
            agent=source,
            message=task,
            data={"from": source.value, "to": target.value, "task": task},
        )
