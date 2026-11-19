"""Katalóg simulovaných útokov a ich vkladanie do dát dema.

Útočník v každom scenári ovláda iba jeden kus nedôveryhodných dát (prílohu, komentár, feed,
úpravu wiki, e-mail k faktúre, neschválenú úpravu znalostnej bázy). Nikdy neovláda systémové
inštrukcie, kód agentov ani oprávnenia.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from soc_demo.domain.environment import PlaybookSection
from soc_demo.domain.errors import NotFoundError
from soc_demo.domain.models import EvidenceItem
from soc_demo.services.environment_store import EnvironmentStore
from soc_demo.services.finance_store import FinanceStore
from soc_demo.services.incident_store import IncidentStore
from soc_demo.services.knowledge_base import KnowledgeBase

DEFAULT_ATTACK_ID = "status_override"


class AttackTargetKind(StrEnum):
    INCIDENT_EVIDENCE = "incident_evidence"
    PLAYBOOK_SECTION = "playbook_section"
    INVOICE_DOCUMENT = "invoice_document"
    KB_SECTION = "kb_section"


class AttackTarget(BaseModel):
    kind: AttackTargetKind
    id: str


class AttackFixture(BaseModel):
    id: str
    title: str
    summary: str
    target: AttackTarget
    evidence: dict[str, object]
    notice: str = ""

    @property
    def evidence_id(self) -> str:
        return str(self.evidence["id"])

    @property
    def target_label(self) -> str:
        if self.target.kind is AttackTargetKind.PLAYBOOK_SECTION:
            return f"playbook {self.target.id}"
        if self.target.kind is AttackTargetKind.INVOICE_DOCUMENT:
            return f"faktúra {self.target.id}"
        if self.target.kind is AttackTargetKind.KB_SECTION:
            return f"znalostná báza {self.target.id}"
        return f"incident {self.target.id}"


class AttackCatalog:
    def __init__(self, fixtures_dir: Path) -> None:
        fixtures: dict[str, AttackFixture] = {}
        for path in sorted(fixtures_dir.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["notice"] = raw.pop("_simulation_notice", "")
            raw.pop("_comment", None)
            fixture = AttackFixture.model_validate(raw)
            fixtures[fixture.id] = fixture
        self._fixtures = fixtures

    def get(self, attack_id: str) -> AttackFixture:
        fixture = self._fixtures.get(attack_id)
        if fixture is None:
            raise NotFoundError(f"Útok {attack_id} neexistuje.")
        return fixture

    def all(self) -> list[AttackFixture]:
        return list(self._fixtures.values())


class InjectionService:
    """Vloží nedôveryhodný obsah útoku na jeho miesto (príloha incidentu, sekcia playbooku, dokument faktúry)."""

    def __init__(
        self,
        catalog: AttackCatalog,
        incidents: IncidentStore,
        environment: EnvironmentStore,
        finance: FinanceStore,
        knowledge: KnowledgeBase,
    ) -> None:
        self._catalog = catalog
        self._incidents = incidents
        self._environment = environment
        self._finance = finance
        self._knowledge = knowledge

    @property
    def catalog(self) -> AttackCatalog:
        return self._catalog

    def is_present(self, attack_id: str) -> bool:
        fixture = self._catalog.get(attack_id)
        if fixture.target.kind is AttackTargetKind.PLAYBOOK_SECTION:
            return self._environment.has_section(fixture.target.id, fixture.evidence_id)
        if fixture.target.kind is AttackTargetKind.INVOICE_DOCUMENT:
            return self._finance.has_document(fixture.target.id, fixture.evidence_id)
        if fixture.target.kind is AttackTargetKind.KB_SECTION:
            return self._knowledge.has_edit(fixture.evidence_id)
        return self._incidents.has_evidence(fixture.target.id, fixture.evidence_id)

    def inject(self, attack_id: str) -> bool:
        """Vráti False, ak už bol obsah útoku vložený."""
        fixture = self._catalog.get(attack_id)
        if fixture.target.kind is AttackTargetKind.PLAYBOOK_SECTION:
            section = PlaybookSection.model_validate(fixture.evidence)
            return self._environment.add_section(fixture.target.id, section)
        if fixture.target.kind is AttackTargetKind.INVOICE_DOCUMENT:
            return self._finance.add_document(fixture.target.id, EvidenceItem.model_validate(fixture.evidence))
        if fixture.target.kind is AttackTargetKind.KB_SECTION:
            return self._knowledge.add_edit(EvidenceItem.model_validate(fixture.evidence))
        return self._incidents.add_evidence(fixture.target.id, EvidenceItem.model_validate(fixture.evidence))
