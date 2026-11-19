"""Heuristika pôvodu: pochádzajú parametre navrhnutého zápisu iba z nedôveryhodného textu?

Slúži iba na vysvetlenie v UI a na metriku „podozrenie na vplyv injekcie“.
Do autorizácie sa nepoužíva, tá je výhradne v SecurityPolicyGate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

from soc_demo.tools.schemas import ValidatedToolCall

_URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")


class TaintHint(BaseModel):
    location_id: str  # incident, playbook, faktúra alebo znalostná báza, kde nedôveryhodný text leží
    evidence_id: str
    evidence_title: str
    matched_terms: list[str]


@dataclass(frozen=True)
class SourceText:
    location_id: str
    evidence_id: str
    title: str
    content: str


def _terms(call: ValidatedToolCall) -> list[str]:
    args = call.arguments()
    match call.name:
        case "update_incident":
            keys = ("incident_id", "status")
        case "send_email":
            keys = ("to",)
        case "block_ip":
            keys = ("ip",)
        case "allowlist_sender":
            keys = ("domain",)
        case "schedule_payment" | "update_vendor_bank":
            keys = ("iban",)
        case "send_reply":
            return list(dict.fromkeys(host.lower() for host in _URL_HOST.findall(str(args.get("body", "")))))
        case _:
            keys = ()
    return [str(args[k]) for k in keys if args.get(k)]


def _contains_all(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return all(term.lower() in lowered for term in terms)


def find_untrusted_origin(call: ValidatedToolCall, untrusted: list[SourceText], trusted: list[str]) -> TaintHint | None:
    """Parametre zápisu sú v nedôveryhodnom texte a nevysvetľuje ich žiadny dôveryhodný zdroj."""
    terms = _terms(call) if call.is_write else []
    if not terms or any(_contains_all(text, terms) for text in trusted):
        return None
    for source in untrusted:
        if _contains_all(source.content, terms):
            return TaintHint(
                location_id=source.location_id,
                evidence_id=source.evidence_id,
                evidence_title=source.title,
                matched_terms=terms,
            )
    return None
