"""Nástroje agentov ako LangChain tools (`@tool`).

Model ich dostane cez `bind_tools` a LangChain ich zavolá priamo: `tool.ainvoke(tool_call)` vráti `ToolMessage`
pre model. Telo každého toolu ide cez bezpečnostnú bránu a executor (`execute_and_report`), takže priame
volanie nástroja neobíde chránený režim, schválenie ani audit. Schémy argumentov a opisy sú tie isté ako
v `TOOL_REGISTRY`, model teda vidí rovnaké nástroje ako pri ostatných protokoloch.

Artefakt `ToolMessage` je `ExecutionOutcome` (stav, rozhodnutie brány, výsledok); obsah správy je
JSON pre model z `tool_result_for_model`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from soc_demo.agents.tooling import execute_and_report, tool_result_for_model
from soc_demo.domain.events import AgentName, OutputSource, Provenance
from soc_demo.domain.models import IncidentStatus
from soc_demo.tools.executor import ExecutionOutcome, ToolExecutor
from soc_demo.tools.schemas import (
    TOOL_REGISTRY,
    AllowlistSenderArgs,
    AnalyzeIncidentArgs,
    BlockIpArgs,
    GetIncidentArgs,
    GetVendorArgs,
    ListDocsArgs,
    ListInvoicesArgs,
    ReadIncidentsArgs,
    SchedulePaymentArgs,
    SearchDocsArgs,
    SearchPlaybookArgs,
    SendEmailArgs,
    SendReplyArgs,
    UpdateIncidentArgs,
    UpdateVendorBankArgs,
    ValidatedToolCall,
)

if TYPE_CHECKING:
    from soc_demo.runs.context import RunContext

_RUNTIME = "soc_runtime"
_CALL_ID = "call_id"


@dataclass(frozen=True)
class ToolRuntime:
    """Kontext behu, v ktorom LangChain tool beží (ktorý agent volá, odkiaľ pochádza návrh)."""

    ctx: RunContext
    executor: ToolExecutor
    agent: AgentName
    provenance: Provenance
    source: OutputSource


async def _gated(name: str, config: RunnableConfig, **fields: Any) -> tuple[str, ExecutionOutcome]:
    """Spoločné telo toolov: návrh → brána → vykonanie → udalosti; vráti JSON pre model a výsledok."""
    configurable = config.get("configurable") or {}
    runtime: ToolRuntime = configurable[_RUNTIME]
    spec = TOOL_REGISTRY[name]
    call = ValidatedToolCall(call_id=configurable[_CALL_ID], spec=spec, args=spec.args_model.model_validate(fields))
    outcome = await execute_and_report(
        runtime.ctx,
        runtime.executor,
        call,
        agent=runtime.agent,
        provenance=runtime.provenance,
        source=runtime.source,
    )
    return json.dumps(tool_result_for_model(outcome), ensure_ascii=False, default=str), outcome


def _description(name: str) -> str:
    return TOOL_REGISTRY[name].description


@tool(
    "read_incidents",
    args_schema=ReadIncidentsArgs,
    description=_description("read_incidents"),
    response_format="content_and_artifact",
)
async def read_incidents(*, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("read_incidents", config)


@tool(
    "get_incident",
    args_schema=GetIncidentArgs,
    description=_description("get_incident"),
    response_format="content_and_artifact",
)
async def get_incident(incident_id: str, *, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("get_incident", config, incident_id=incident_id)


@tool(
    "analyze_incident",
    args_schema=AnalyzeIncidentArgs,
    description=_description("analyze_incident"),
    response_format="content_and_artifact",
)
async def analyze_incident(incident_id: str, *, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("analyze_incident", config, incident_id=incident_id)


@tool(
    "update_incident",
    args_schema=UpdateIncidentArgs,
    description=_description("update_incident"),
    response_format="content_and_artifact",
)
async def update_incident(
    incident_id: str, status: IncidentStatus, comment: str | None = None, *, config: RunnableConfig
) -> tuple[str, ExecutionOutcome]:
    return await _gated("update_incident", config, incident_id=incident_id, status=status, comment=comment)


@tool(
    "search_playbook",
    args_schema=SearchPlaybookArgs,
    description=_description("search_playbook"),
    response_format="content_and_artifact",
)
async def search_playbook(query: str, *, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("search_playbook", config, query=query)


@tool(
    "send_email",
    args_schema=SendEmailArgs,
    description=_description("send_email"),
    response_format="content_and_artifact",
)
async def send_email(to: str, subject: str, body: str, *, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("send_email", config, to=to, subject=subject, body=body)


@tool(
    "block_ip",
    args_schema=BlockIpArgs,
    description=_description("block_ip"),
    response_format="content_and_artifact",
)
async def block_ip(ip: IPv4Address, reason: str, *, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("block_ip", config, ip=ip, reason=reason)


@tool(
    "allowlist_sender",
    args_schema=AllowlistSenderArgs,
    description=_description("allowlist_sender"),
    response_format="content_and_artifact",
)
async def allowlist_sender(domain: str, *, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("allowlist_sender", config, domain=domain)


@tool(
    "list_invoices",
    args_schema=ListInvoicesArgs,
    description=_description("list_invoices"),
    response_format="content_and_artifact",
)
async def list_invoices(*, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("list_invoices", config)


@tool(
    "get_vendor",
    args_schema=GetVendorArgs,
    description=_description("get_vendor"),
    response_format="content_and_artifact",
)
async def get_vendor(vendor_id: str, *, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("get_vendor", config, vendor_id=vendor_id)


@tool(
    "schedule_payment",
    args_schema=SchedulePaymentArgs,
    description=_description("schedule_payment"),
    response_format="content_and_artifact",
)
async def schedule_payment(
    invoice_id: str, iban: str, amount_eur: float, *, config: RunnableConfig
) -> tuple[str, ExecutionOutcome]:
    return await _gated("schedule_payment", config, invoice_id=invoice_id, iban=iban, amount_eur=amount_eur)


@tool(
    "update_vendor_bank",
    args_schema=UpdateVendorBankArgs,
    description=_description("update_vendor_bank"),
    response_format="content_and_artifact",
)
async def update_vendor_bank(
    vendor_id: str, iban: str, reason: str, *, config: RunnableConfig
) -> tuple[str, ExecutionOutcome]:
    return await _gated("update_vendor_bank", config, vendor_id=vendor_id, iban=iban, reason=reason)


@tool(
    "list_docs",
    args_schema=ListDocsArgs,
    description=_description("list_docs"),
    response_format="content_and_artifact",
)
async def list_docs(*, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("list_docs", config)


@tool(
    "search_docs",
    args_schema=SearchDocsArgs,
    description=_description("search_docs"),
    response_format="content_and_artifact",
)
async def search_docs(query: str, *, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("search_docs", config, query=query)


@tool(
    "send_reply",
    args_schema=SendReplyArgs,
    description=_description("send_reply"),
    response_format="content_and_artifact",
)
async def send_reply(to: str, subject: str, body: str, *, config: RunnableConfig) -> tuple[str, ExecutionOutcome]:
    return await _gated("send_reply", config, to=to, subject=subject, body=body)


LANGCHAIN_TOOLS: dict[str, BaseTool] = {
    t.name: t
    for t in (
        read_incidents,
        get_incident,
        analyze_incident,
        update_incident,
        search_playbook,
        send_email,
        block_ip,
        allowlist_sender,
        list_invoices,
        get_vendor,
        schedule_payment,
        update_vendor_bank,
        list_docs,
        search_docs,
        send_reply,
    )
}


def tools_for(names: Iterable[str]) -> list[BaseTool]:
    """LangChain tools v poradí profilu agenta (napr. pre `bind_tools`)."""
    return [LANGCHAIN_TOOLS[name] for name in names]


async def call_tool(
    runtime: ToolRuntime, call: ValidatedToolCall, *, model_call_id: str | None = None
) -> tuple[ExecutionOutcome, ToolMessage]:
    """LangChain zavolá tool priamo s overenými argumentmi; vráti výsledok vykonania a `ToolMessage` pre model."""
    message = await LANGCHAIN_TOOLS[call.name].ainvoke(
        {"type": "tool_call", "name": call.name, "args": call.arguments(), "id": model_call_id or call.call_id},
        config={"configurable": {_RUNTIME: runtime, _CALL_ID: call.call_id}},
    )
    return message.artifact, message
