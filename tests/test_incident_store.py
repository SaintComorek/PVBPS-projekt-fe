from __future__ import annotations

import json

import pytest

from soc_demo.container import Container
from soc_demo.domain.errors import NotFoundError
from soc_demo.domain.models import Incident, IncidentStatus, TrustLevel
from tests.conftest import FIXTURE_PATH, INJECTION_TEXT, seed_incidents


def test_seed_contains_three_synthetic_incidents(container: Container) -> None:
    incidents = container.store.list()
    assert [i.id for i in incidents] == ["INC-001", "INC-002", "INC-003"]
    assert container.store.get("INC-003").status is IncidentStatus.OPEN


def test_update_status_returns_change_and_persists(container: Container) -> None:
    change = container.store.update_status("INC-003", IncidentStatus.RESOLVED)
    assert (change.before, change.after) == ("OPEN", "RESOLVED")
    state_file = container.settings.runtime_dir / "incidents.json"
    persisted = json.loads(state_file.read_text(encoding="utf-8"))["incidents"]
    assert next(i for i in persisted if i["id"] == "INC-003")["status"] == "RESOLVED"


def test_unknown_incident_raises_not_found(container: Container) -> None:
    with pytest.raises(NotFoundError):
        container.store.get("INC-999")
    with pytest.raises(NotFoundError):
        container.store.update_status("INC-999", IncidentStatus.RESOLVED)


def test_reset_restores_exact_seed_state(container: Container) -> None:
    container.injections.inject("status_override")
    container.store.update_status("INC-003", IncidentStatus.RESOLVED)
    container.store.update_status("INC-001", IncidentStatus.INVESTIGATING)

    container.store.reset()

    expected = [Incident.model_validate(raw) for raw in seed_incidents()]
    assert container.store.list() == expected
    persisted = json.loads((container.settings.runtime_dir / "incidents.json").read_text(encoding="utf-8"))
    assert [Incident.model_validate(raw) for raw in persisted["incidents"]] == expected
    assert container.injections.is_present("status_override") is False


def test_returned_incidents_are_copies(container: Container) -> None:
    incident = container.store.get("INC-003")
    incident.status = IncidentStatus.RESOLVED
    assert container.store.get("INC-003").status is IncidentStatus.OPEN


def test_injection_fixture_matches_specification() -> None:
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    evidence = raw["evidence"]
    assert raw["id"] == "status_override"
    assert raw["target"] == {"kind": "incident_evidence", "id": "INC-002"}
    assert evidence["trust"] == "untrusted"
    assert evidence["injected"] is True
    assert INJECTION_TEXT in evidence["content"]
    assert "SIMULÁCIA" in raw["_simulation_notice"]


def test_inject_adds_untrusted_evidence_once(container: Container) -> None:
    assert container.injections.is_present("status_override") is False
    assert container.injections.inject("status_override") is True
    assert container.injections.inject("status_override") is False

    target = container.store.get("INC-002")
    injected = [e for e in target.evidence if e.injected]
    assert len(injected) == 1
    assert injected[0].trust is TrustLevel.UNTRUSTED
    assert INJECTION_TEXT in injected[0].content
    assert all(not e.injected for i in container.store.list() if i.id != "INC-002" for e in i.evidence)
