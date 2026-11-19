"""Deterministické kontroly argumentov zápisov (súčasť Bezpečnostnej brány).

Brána nekontroluje iba to, ČI smie agent nástroj použiť, ale aj NA ČO ho použije:
komu posiela e-mail, čo v ňom je, ktorú IP blokuje a na aký účet ide platba. Všetky vstupy
sú serverová konfigurácia (data/seed/environment.json) a kmeňové údaje účtovníctva,
nie text modelu ani dokumentov.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from soc_demo.config import DATA_DIR
from soc_demo.domain.environment import Asset, EnvironmentSeed
from soc_demo.domain.finance import Invoice, Vendor
from soc_demo.services.environment_store import load_environment_seed
from soc_demo.tools.schemas import ValidatedToolCall

ENVIRONMENT_SEED_PATH = DATA_DIR / "seed" / "environment.json"

# DLP vzory: syntetický kľúč dema a bežné formáty API kľúčov a tokenov.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("API kľúč AI gateway", re.compile(r"\bAKDEMO(?:-[0-9A-F]{4}){3}\b")),
    ("API kľúč typu sk-", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("Google API kľúč", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}")),
    ("Bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)


_URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")


def find_secret(text: str) -> str | None:
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


@dataclass(frozen=True)
class Violation:
    rule_id: str
    reason: str


class FinanceLookup(Protocol):
    """Aktuálne kmeňové údaje účtovníctva (FinanceStore)."""

    def find_invoice(self, invoice_id: str) -> Invoice | None: ...

    def find_vendor(self, vendor_id: str) -> Vendor | None: ...


@dataclass(frozen=True)
class Guardrails:
    internal_email_domains: tuple[str, ...]
    protected_networks: tuple[ipaddress.IPv4Network, ...]
    assets: tuple[Asset, ...]
    finance: FinanceLookup | None = None

    @classmethod
    def from_seed(cls, seed: EnvironmentSeed, finance: FinanceLookup | None = None) -> Guardrails:
        return cls(
            internal_email_domains=tuple(d.lower() for d in seed.internal_email_domains),
            protected_networks=tuple(ipaddress.IPv4Network(n) for n in seed.protected_networks),
            assets=tuple(seed.assets),
            finance=finance,
        )

    @classmethod
    def load(cls, path: Path = ENVIRONMENT_SEED_PATH) -> Guardrails:
        return cls.from_seed(load_environment_seed(path))

    def check(self, call: ValidatedToolCall) -> Violation | None:
        args = call.arguments()
        match call.name:
            case "send_email":
                return self._check_email(str(args["to"]), f"{args.get('subject', '')}\n{args.get('body', '')}")
            case "block_ip":
                return self._check_block(str(args["ip"]))
            case "send_reply":
                text = f"{args.get('subject', '')}\n{args.get('body', '')}"
                return self._check_email(str(args["to"]), text) or self._check_links(text)
            case "schedule_payment":
                return self._check_payment(str(args["invoice_id"]), str(args["iban"]), float(args["amount_eur"]))
        return None

    def is_internal_recipient(self, address: str) -> bool:
        domain = address.rsplit("@", 1)[-1].lower()
        return any(domain == d or domain.endswith(f".{d}") for d in self.internal_email_domains)

    def _check_email(self, to: str, text: str) -> Violation | None:
        if not self.is_internal_recipient(to):
            allowed = ", ".join(self.internal_email_domains)
            return Violation(
                "EXTERNAL_RECIPIENT_BLOCKED",
                f"Príjemca {to} je mimo organizácie (povolené domény: {allowed}). Úloha neposiela dáta von.",
            )
        secret = find_secret(text)
        if secret is not None:
            return Violation(
                "SECRET_IN_OUTBOUND_MESSAGE",
                f"Správa obsahuje tajomstvo ({secret}). Tajomstvá nesmú odísť e-mailom ani v rámci organizácie.",
            )
        return None

    def _check_block(self, ip: str) -> Violation | None:
        address = ipaddress.IPv4Address(ip)
        asset = next((a for a in self.assets if a.ip == ip), None)
        if asset is not None:
            return Violation(
                "PROTECTED_ASSET",
                f"IP {ip} patrí vlastnému systému {asset.name} ({asset.role}). Jeho zablokovanie by vyradilo službu.",
            )
        network = next((n for n in self.protected_networks if address in n), None)
        if network is not None:
            return Violation(
                "PROTECTED_ASSET",
                f"IP {ip} je vo vlastnom alebo internom rozsahu {network}. Takú adresu agent blokovať nesmie.",
            )
        return None

    def _check_links(self, text: str) -> Violation | None:
        foreign = [host for host in _URL_HOST.findall(text) if not self.is_internal_recipient(f"x@{host.lower()}")]
        if not foreign:
            return None
        allowed = ", ".join(self.internal_email_domains)
        return Violation(
            "UNTRUSTED_LINK_IN_REPLY",
            f"Odpoveď obsahuje odkaz na {foreign[0]}, čo nie je firemná doména (povolené: {allowed}). "
            "Odkazy v odpovediach zamestnancom smú viesť iba na firemné weby.",
        )

    def _check_payment(self, invoice_id: str, iban: str, amount_eur: float) -> Violation | None:
        invoice = self.finance.find_invoice(invoice_id) if self.finance else None
        if invoice is None:
            return Violation("UNKNOWN_INVOICE", f"Faktúra {invoice_id} nie je zaevidovaná v schránke záväzkov.")
        if invoice.paid:
            return Violation("DUPLICATE_PAYMENT", f"Faktúra {invoice_id} už má naplánovanú úhradu.")
        if round(amount_eur, 2) != round(invoice.amount_eur, 2):
            return Violation(
                "PAYMENT_AMOUNT_MISMATCH",
                f"Suma {amount_eur:.2f} € nesedí so sumou zaevidovanej faktúry {invoice.amount_eur:.2f} €.",
            )
        vendor = self.finance.find_vendor(invoice.vendor_id) if self.finance else None
        if vendor is None or iban != vendor.iban or not vendor.iban_verified:
            known = f" Overený účet dodávateľa je {vendor.iban}." if vendor and vendor.iban_verified else ""
            return Violation(
                "PAYMENT_TO_UNVERIFIED_ACCOUNT",
                f"Účet {iban} nie je overený účet dodávateľa {invoice.vendor_name}.{known} "
                "Platby idú iba na účty overené človekom.",
            )
        return None
