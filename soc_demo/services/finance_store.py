"""Simulované účtovníctvo: kmeňové údaje dodávateľov, schránka faktúr a naplánované úhrady.

Nič neodchádza do banky ani do siete. Stav sa drží v pamäti a v `var/finance.json`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from soc_demo.domain.errors import NotFoundError
from soc_demo.domain.finance import FinanceSeed, Invoice, Payment, Vendor, normalize_iban
from soc_demo.domain.models import ChangeTarget, EvidenceItem, StateChange, TrustLevel
from soc_demo.services.persistence import write_json_atomic


def load_finance_seed(path: Path) -> FinanceSeed:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    return FinanceSeed.model_validate(raw)


def _eur(amount: float) -> str:
    return f"{amount:,.2f} €".replace(",", " ").replace(".", ",")


class FinanceStore:
    def __init__(self, seed_path: Path, state_path: Path) -> None:
        self._seed_path = seed_path
        self._state_path = state_path
        self._lock = threading.RLock()
        self._vendors: dict[str, Vendor] = {}
        self._invoices: dict[str, Invoice] = {}
        self._payments: list[Payment] = []
        self.reset()

    def reset(self) -> None:
        with self._lock:
            seed = load_finance_seed(self._seed_path)
            self._vendors = {vendor.id: vendor.model_copy() for vendor in seed.vendors}
            self._invoices = {invoice.id: invoice.model_copy(deep=True) for invoice in seed.invoices}
            self._payments = []
            self._persist()

    # --- čítanie -------------------------------------------------------------------------------

    def vendors(self) -> list[Vendor]:
        with self._lock:
            return [vendor.model_copy() for vendor in self._vendors.values()]

    def vendor(self, vendor_id: str) -> Vendor:
        with self._lock:
            vendor = self._vendors.get(vendor_id)
            if vendor is None:
                raise NotFoundError(f"Dodávateľ {vendor_id} neexistuje.")
            return vendor.model_copy()

    def find_vendor(self, vendor_id: str) -> Vendor | None:
        with self._lock:
            vendor = self._vendors.get(vendor_id)
            return vendor.model_copy() if vendor else None

    def invoices(self) -> list[Invoice]:
        with self._lock:
            return [invoice.model_copy(deep=True) for invoice in self._invoices.values()]

    def find_invoice(self, invoice_id: str) -> Invoice | None:
        with self._lock:
            invoice = self._invoices.get(invoice_id)
            return invoice.model_copy(deep=True) if invoice else None

    def payments(self) -> list[Payment]:
        with self._lock:
            return [payment.model_copy() for payment in self._payments]

    def inbox(self) -> list[dict[str, Any]]:
        """Schránka záväzkov pre agenta: faktúry s dokumentmi, bez interných stavov účtovníctva."""
        return [invoice.model_dump(mode="json", exclude={"paid"}) for invoice in self.invoices()]

    def untrusted_documents(self) -> list[tuple[Invoice, EvidenceItem]]:
        with self._lock:
            return [
                (invoice, document)
                for invoice in self._invoices.values()
                for document in invoice.documents
                if document.trust is TrustLevel.UNTRUSTED
            ]

    def trusted_texts(self) -> list[str]:
        """Dôveryhodné údaje (kmeňové údaje dodávateľov) pre heuristiku pôvodu."""
        with self._lock:
            return [f"{v.id} {v.name} {v.iban}" for v in self._vendors.values() if v.iban_verified]

    def snapshot(self) -> tuple[list[str], list[str]]:
        """Úhrady a účty dodávateľov pre porovnanie pred behom a po ňom."""
        with self._lock:
            payments = [f"{p.invoice_id} → {p.iban}: {_eur(p.amount_eur)}" for p in self._payments]
            accounts = [
                f"{v.id} {v.name}: {v.iban}" + ("" if v.iban_verified else " (neoverený)")
                for v in self._vendors.values()
            ]
            return payments, accounts

    # --- zápisy (iba cez ToolExecutor alebo injekciu dema) ----------------------------------

    def has_document(self, invoice_id: str, document_id: str) -> bool:
        with self._lock:
            invoice = self._invoices.get(invoice_id)
            return bool(invoice and any(d.id == document_id for d in invoice.documents))

    def add_document(self, invoice_id: str, document: EvidenceItem) -> bool:
        with self._lock:
            invoice = self._invoices.get(invoice_id)
            if invoice is None:
                raise NotFoundError(f"Faktúra {invoice_id} neexistuje.")
            if self.has_document(invoice_id, document.id):
                return False
            invoice.documents.append(document.model_copy())
            self._persist()
            return True

    def schedule_payment(self, invoice_id: str, iban: str, amount_eur: float) -> tuple[Payment, StateChange]:
        with self._lock:
            invoice = self._invoices.get(invoice_id)
            if invoice is None:
                raise NotFoundError(f"Faktúra {invoice_id} neexistuje.")
            vendor = self._vendors[invoice.vendor_id]
            account = normalize_iban(iban)
            payment = Payment(
                id=f"PAY-{len(self._payments) + 1:04d}",
                invoice_id=invoice_id,
                vendor_id=vendor.id,
                iban=account,
                amount_eur=round(amount_eur, 2),
                account_verified=vendor.iban_verified and account == vendor.iban,
            )
            self._payments.append(payment)
            invoice.paid = True
            self._persist()
            where = "na overený účet dodávateľa" if payment.account_verified else "na NEOVERENÝ účet"
            return payment, StateChange(
                target=ChangeTarget.PAYMENT,
                target_id=invoice_id,
                field="payment",
                before="neuhradená",
                after=f"{_eur(payment.amount_eur)} → {account}",
                summary=f"Úhrada {invoice_id} ({vendor.name}) {_eur(payment.amount_eur)} {where} {account}.",
            )

    def update_vendor_bank(self, vendor_id: str, iban: str, *, verified: bool) -> StateChange:
        with self._lock:
            vendor = self._vendors.get(vendor_id)
            if vendor is None:
                raise NotFoundError(f"Dodávateľ {vendor_id} neexistuje.")
            before = vendor.iban
            account = normalize_iban(iban)
            self._vendors[vendor_id] = vendor.model_copy(update={"iban": account, "iban_verified": verified})
            self._persist()
            state = "overený človekom" if verified else "bez overenia"
            return StateChange(
                target=ChangeTarget.VENDOR_BANK,
                target_id=vendor_id,
                field="iban",
                before=before,
                after=account,
                summary=f"Účet dodávateľa {vendor.name} zmenený z {before} na {account} ({state}).",
            )

    def _persist(self) -> None:
        write_json_atomic(
            self._state_path,
            {
                "_comment": "Pracovný stav simulovaného účtovníctva. Prepíše sa pri resete dema.",
                "vendors": [vendor.model_dump() for vendor in self._vendors.values()],
                "invoices": [invoice.model_dump(mode="json") for invoice in self._invoices.values()],
                "payments": [payment.model_dump() for payment in self._payments],
            },
        )
