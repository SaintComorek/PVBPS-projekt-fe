"""Syntetické účtovníctvo (záväzky): dodávatelia, došlé faktúry a naplánované úhrady.

Všetky IBAN v deme sú fiktívne s kontrolnými číslicami 00, ktoré žiadny platný IBAN nemá.
"""

from __future__ import annotations

import re

from pydantic import Field

from soc_demo.domain.models import EvidenceItem, StrictModel

VENDOR_ID_PATTERN = r"^VEN-\d{3}$"
INVOICE_ID_PATTERN = r"^FA-\d{4}-\d{3}$"
# Pri vstupe od modelu sú medzery povolené (IBAN sa píše po štvoriciach), uloží sa bez nich.
IBAN_INPUT_PATTERN = r"^[A-Za-z]{2}[0-9]{2}[A-Za-z0-9 ]{11,40}$"
IBAN_PATTERN = r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$"


def normalize_iban(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


class Vendor(StrictModel):
    """Kmeňové údaje dodávateľa v účtovnom systéme (dôveryhodné, spravuje ich človek)."""

    id: str = Field(pattern=VENDOR_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    iban: str = Field(pattern=IBAN_PATTERN)
    # Účet overený človekom (napr. telefonátom na známe číslo dodávateľa). Zmena bez schválenia overenie zruší.
    iban_verified: bool = True
    contact: str = Field(min_length=3, max_length=160)


class Invoice(StrictModel):
    """Faktúra zaevidovaná v schránke záväzkov. Dokumenty (PDF, e-maily) prišli zvonka, sú nedôveryhodné."""

    id: str = Field(pattern=INVOICE_ID_PATTERN)
    vendor_id: str = Field(pattern=VENDOR_ID_PATTERN)
    vendor_name: str = Field(min_length=1, max_length=120)
    amount_eur: float = Field(gt=0, le=1_000_000)
    due_date: str
    iban: str = Field(pattern=IBAN_PATTERN)  # účet uvedený na faktúre
    paid: bool = False
    documents: list[EvidenceItem] = Field(default_factory=list)


class Payment(StrictModel):
    id: str
    invoice_id: str
    vendor_id: str
    iban: str
    amount_eur: float
    account_verified: bool  # platba ide na účet overený človekom


class FinanceSeed(StrictModel):
    """Obsah súboru data/seed/finance.json."""

    vendors: list[Vendor]
    invoices: list[Invoice]
