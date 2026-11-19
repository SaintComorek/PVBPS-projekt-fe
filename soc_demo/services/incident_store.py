"""Úložisko syntetických incidentov: seed v JSON, pracovná kópia v pamäti a v `var/`."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from soc_demo.domain.errors import NotFoundError
from soc_demo.domain.models import ChangeTarget, EvidenceItem, Incident, IncidentStatus, StateChange
from soc_demo.services.persistence import write_json_atomic


def load_seed(path: Path) -> list[Incident]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Incident.model_validate(item) for item in raw["incidents"]]


class IncidentStore:
    """Jediné miesto, ktoré mení stav incidentov. Cesty sú pevné z konfigurácie, nie z agentov."""

    def __init__(self, seed_path: Path, state_path: Path) -> None:
        self._seed_path = seed_path
        self._state_path = state_path
        self._lock = threading.RLock()
        self._incidents: dict[str, Incident] = {}
        self.reset()

    def reset(self) -> list[Incident]:
        with self._lock:
            self._incidents = {incident.id: incident for incident in load_seed(self._seed_path)}
            self._persist()
            return self.list()

    def list(self) -> list[Incident]:
        with self._lock:
            return [incident.model_copy(deep=True) for incident in self._incidents.values()]

    def get(self, incident_id: str) -> Incident:
        with self._lock:
            return self._require(incident_id).model_copy(deep=True)

    def has_evidence(self, incident_id: str, evidence_id: str) -> bool:
        with self._lock:
            incident = self._incidents.get(incident_id)
            return bool(incident and any(e.id == evidence_id for e in incident.evidence))

    def add_evidence(self, incident_id: str, evidence: EvidenceItem) -> bool:
        """Priloží dôkaz k incidentu. Vráti False, ak už priložený bol."""
        with self._lock:
            if self.has_evidence(incident_id, evidence.id):
                return False
            self._require(incident_id).evidence.append(evidence.model_copy(deep=True))
            self._persist()
            return True

    def update_status(self, incident_id: str, status: IncidentStatus) -> StateChange:
        with self._lock:
            incident = self._require(incident_id)
            change = StateChange(
                target=ChangeTarget.INCIDENT,
                target_id=incident_id,
                field="status",
                before=incident.status.value,
                after=status.value,
                summary=f"{incident_id}: stav {incident.status.value} → {status.value}",
            )
            incident.status = status
            self._persist()
            return change

    def _require(self, incident_id: str) -> Incident:
        incident = self._incidents.get(incident_id)
        if incident is None:
            raise NotFoundError(f"Incident {incident_id} neexistuje.")
        return incident

    def _persist(self) -> None:
        write_json_atomic(
            self._state_path,
            {
                "_comment": "Pracovný stav syntetických incidentov. Prepíše sa pri resete dema.",
                "incidents": [i.model_dump(mode="json") for i in self._incidents.values()],
            },
        )
