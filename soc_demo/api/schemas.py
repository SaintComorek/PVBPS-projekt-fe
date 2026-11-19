"""DTO pre REST API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from soc_demo.domain.environment import Asset, BlockedIp, PlaybookArticle, SentEmail
from soc_demo.domain.events import RunEvent
from soc_demo.domain.finance import Invoice, Payment, Vendor
from soc_demo.domain.knowledge import KbChunk, Reply
from soc_demo.domain.models import Incident
from soc_demo.runs.models import RunRecord


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMStatus(ApiModel):
    configured: bool
    model: str | None
    endpoint_host: str | None
    api_key_configured: bool = False
    reasoning_effort: str | None = None
    protocol: str = "langchain"


class TaskInfo(ApiModel):
    id: str
    title: str
    instruction: str
    description: str
    write_scope: str
    actions: list[str]
    attack_id: str
    domain: str  # soc | finance | helpdesk


class AttackInfo(ApiModel):
    id: str
    title: str
    summary: str
    target_kind: str  # incident_evidence | playbook_section | invoice_document | kb_section
    target_id: str  # INC-xxx, KB-xxx alebo FA-xxxx-xxx
    evidence_id: str
    present: bool
    notice: str


class EnvironmentView(ApiModel):
    firewall_blocklist: list[BlockedIp]
    sender_allowlist: list[str]
    outbox: list[SentEmail]
    playbooks: list[PlaybookArticle]
    assets: list[Asset]
    internal_email_domains: list[str]


class FinanceView(ApiModel):
    vendors: list[Vendor]
    invoices: list[Invoice]
    payments: list[Payment]


class KnowledgeView(ApiModel):
    doc_id: str
    title: str
    chunks: list[KbChunk]
    replies: list[Reply]
    chunk_size: int
    chunk_overlap: int
    top_k: int
    embedding_model: str


class PrincipalInfo(ApiModel):
    user_id: str
    display_name: str
    role: str


class DemoState(ApiModel):
    incidents: list[Incident]
    environment: EnvironmentView
    finance: FinanceView
    knowledge: KnowledgeView
    attacks: list[AttackInfo]
    active_run_id: str | None
    pending_approvals: int
    tasks: list[TaskInfo]
    llm: LLMStatus
    principal: PrincipalInfo


class ResetRequest(ApiModel):
    clear_history: bool = False


class InjectRequest(ApiModel):
    attack_id: str = "status_override"


class RunDetail(ApiModel):
    run: RunRecord
    events: list[RunEvent]


class ApprovalDecisionRequest(ApiModel):
    approve: bool


class HealthResponse(ApiModel):
    status: str
    version: str
    llm: LLMStatus
    active_run_id: str | None
