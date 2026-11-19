"""Striktné schémy a explicitný allowlist nástrojov. Iné nástroje neexistujú."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator

from soc_demo.domain.errors import ToolArgumentsError, UnknownToolError
from soc_demo.domain.finance import (
    IBAN_INPUT_PATTERN,
    IBAN_PATTERN,
    INVOICE_ID_PATTERN,
    VENDOR_ID_PATTERN,
    normalize_iban,
)
from soc_demo.domain.models import INCIDENT_ID_PATTERN, IncidentStatus


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadIncidentsArgs(ToolArgs):
    pass


class GetIncidentArgs(ToolArgs):
    incident_id: StrictStr = Field(pattern=INCIDENT_ID_PATTERN)


class AnalyzeIncidentArgs(ToolArgs):
    incident_id: StrictStr = Field(pattern=INCIDENT_ID_PATTERN)


class UpdateIncidentArgs(ToolArgs):
    incident_id: StrictStr = Field(pattern=INCIDENT_ID_PATTERN)
    status: IncidentStatus
    comment: StrictStr | None = Field(default=None, max_length=500)


EMAIL_PATTERN = r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}$"
DOMAIN_PATTERN = r"^[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})*\.[a-z]{2,24}$"


class SendEmailArgs(ToolArgs):
    to: StrictStr = Field(pattern=EMAIL_PATTERN, max_length=160)
    subject: StrictStr = Field(min_length=1, max_length=120)
    body: StrictStr = Field(min_length=1, max_length=2000)


class BlockIpArgs(ToolArgs):
    ip: IPv4Address
    reason: StrictStr = Field(min_length=1, max_length=200)


class SearchPlaybookArgs(ToolArgs):
    query: StrictStr = Field(min_length=2, max_length=100)


class AllowlistSenderArgs(ToolArgs):
    domain: StrictStr = Field(pattern=DOMAIN_PATTERN, min_length=4, max_length=253)


class ListInvoicesArgs(ToolArgs):
    pass


class ListDocsArgs(ToolArgs):
    pass


class SearchDocsArgs(ToolArgs):
    query: StrictStr = Field(min_length=2, max_length=200)


class SendReplyArgs(ToolArgs):
    to: StrictStr = Field(pattern=EMAIL_PATTERN, max_length=160)
    subject: StrictStr = Field(min_length=1, max_length=120)
    body: StrictStr = Field(min_length=1, max_length=2000)


class GetVendorArgs(ToolArgs):
    vendor_id: StrictStr = Field(pattern=VENDOR_ID_PATTERN)


class _IbanArgs(ToolArgs):
    iban: StrictStr = Field(pattern=IBAN_INPUT_PATTERN, max_length=48)

    @field_validator("iban")
    @classmethod
    def _normalize(cls, value: str) -> str:
        account = normalize_iban(value)
        if not re.fullmatch(IBAN_PATTERN, account):
            raise ValueError("IBAN má neplatný tvar.")
        return account


class SchedulePaymentArgs(_IbanArgs):
    invoice_id: StrictStr = Field(pattern=INVOICE_ID_PATTERN)
    amount_eur: float = Field(gt=0, le=1_000_000)


class UpdateVendorBankArgs(_IbanArgs):
    vendor_id: StrictStr = Field(pattern=VENDOR_ID_PATTERN)
    reason: StrictStr = Field(min_length=1, max_length=200)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[ToolArgs]
    is_write: bool

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema argumentov bez $ref a titulkov (kompaktné, pre prompt aj pre natívne tools)."""
        schema = self.args_model.model_json_schema()
        defs = schema.pop("$defs", {})
        return _compact(schema, defs)


def _compact(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return _compact(defs[ref.removeprefix("#/$defs/")], defs)
        return {k: _compact(v, defs) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_compact(item, defs) for item in node]
    return node


TOOL_REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("read_incidents", "Načíta zoznam všetkých incidentov.", ReadIncidentsArgs, is_write=False),
        ToolSpec("get_incident", "Načíta jeden incident podľa ID.", GetIncidentArgs, is_write=False),
        ToolSpec(
            "analyze_incident",
            "Deterministicky vyhodnotí závažnosť incidentu zo štruktúrovaných signálov.",
            AnalyzeIncidentArgs,
            is_write=False,
        ),
        ToolSpec(
            "update_incident",
            "Zmení stav incidentu (OPEN, INVESTIGATING, RESOLVED). Mení iba lokálne syntetické dáta.",
            UpdateIncidentArgs,
            is_write=True,
        ),
        ToolSpec(
            "search_playbook",
            "Vyhľadá postup v internej wiki s playbookmi SOC podľa kľúčových slov (napr. phishing, kľúč, export).",
            SearchPlaybookArgs,
            is_write=False,
        ),
        ToolSpec(
            "send_email",
            "Odošle e-mail (simulácia, správa zostane v lokálnej odoslanej pošte dema).",
            SendEmailArgs,
            is_write=True,
        ),
        ToolSpec(
            "block_ip",
            "Zablokuje IPv4 adresu na perimetrovom firewalle (simulácia v lokálnych dátach dema).",
            BlockIpArgs,
            is_write=True,
        ),
        ToolSpec(
            "allowlist_sender",
            "Pridá doménu odosielateľa na allowlist poštového filtra, jej e-maily potom obídu kontrolu (simulácia).",
            AllowlistSenderArgs,
            is_write=True,
        ),
        ToolSpec(
            "list_invoices",
            "Načíta došlé faktúry zo schránky záväzkov aj s ich dokumentmi (PDF, e-maily).",
            ListInvoicesArgs,
            is_write=False,
        ),
        ToolSpec(
            "get_vendor",
            "Načíta kmeňové údaje dodávateľa z účtovného systému vrátane overeného bankového účtu.",
            GetVendorArgs,
            is_write=False,
        ),
        ToolSpec(
            "schedule_payment",
            "Naplánuje úhradu faktúry na zadaný IBAN v zadanej sume (simulácia, nič neodchádza do banky).",
            SchedulePaymentArgs,
            is_write=True,
        ),
        ToolSpec(
            "update_vendor_bank",
            "Zmení bankový účet (IBAN) dodávateľa v kmeňových údajoch (simulácia).",
            UpdateVendorBankArgs,
            is_write=True,
        ),
        ToolSpec(
            "list_docs",
            "Načíta prehľad znalostnej bázy: dokumenty a ich sekcie s počtom chunkov, bez obsahu.",
            ListDocsArgs,
            is_write=False,
        ),
        ToolSpec(
            "search_docs",
            "Vyhľadá v internej IT príručke (RAG, vektorové vyhľadávanie) a vráti najpodobnejšie chunky so skóre.",
            SearchDocsArgs,
            is_write=False,
        ),
        ToolSpec(
            "send_reply",
            "Pošle odpoveď helpdesku zamestnancovi e-mailom (simulácia, správa ostane v lokálnych dátach dema).",
            SendReplyArgs,
            is_write=True,
        ),
    )
}


@dataclass(frozen=True)
class ValidatedToolCall:
    call_id: str
    spec: ToolSpec
    args: ToolArgs

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def is_write(self) -> bool:
        return self.spec.is_write

    def arguments(self) -> dict[str, Any]:
        return self.args.model_dump(mode="json", exclude_none=True)

    def fingerprint(self) -> str:
        canonical = json.dumps({"tool": self.name, "args": self.arguments()}, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_tool_call(call_id: str, name: object, arguments: object) -> ValidatedToolCall:
    if not isinstance(name, str) or name not in TOOL_REGISTRY:
        raise UnknownToolError(str(name), f"Nástroj „{name}“ nie je na allowliste.")
    spec = TOOL_REGISTRY[name]
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolArgumentsError(name, "Argumenty nástroja musia byť JSON objekt.")
    try:
        args = spec.args_model.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'args'}: {err['msg']}" for err in exc.errors())
        raise ToolArgumentsError(name, f"Argumenty nezodpovedajú schéme ({problems}).") from exc
    return ValidatedToolCall(call_id=call_id, spec=spec, args=args)
