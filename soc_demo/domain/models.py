"""Doménové modely syntetických incidentov a režimov dema."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

INCIDENT_ID_PATTERN = r"^INC-\d{3}$"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"


class TrustLevel(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class Signal(StrEnum):
    """Štruktúrovaná telemetria z interných systémov (dôveryhodná)."""

    CREDENTIAL_EXPOSURE = "credential_exposure"
    PUBLIC_EXPOSURE = "public_exposure"
    AI_SUPPLY_CHAIN = "ai_supply_chain"
    PHISHING = "phishing"
    USER_REPORTED = "user_reported"
    PRIVILEGED_ACCOUNT = "privileged_account"
    DATA_EXFILTRATION = "data_exfiltration"
    NEW_DEVICE_SIGNIN = "new_device_signin"
    MFA_BYPASS = "mfa_bypass"


class SecurityMode(StrEnum):
    VULNERABLE = "vulnerable"
    PROTECTED = "protected"


class ExecutionMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class Scenario(StrEnum):
    NORMAL = "normal"
    ATTACK = "attack"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceItem(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    trust: TrustLevel
    source: str = Field(min_length=1, max_length=120)
    content: str = Field(max_length=4000)
    injected: bool = False


class Incident(StrictModel):
    id: str = Field(pattern=INCIDENT_ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    severity: Severity
    status: IncidentStatus
    reported_at: str
    source: str
    affected_assets: list[str] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    def untrusted_evidence(self) -> list[EvidenceItem]:
        return [item for item in self.evidence if item.trust is TrustLevel.UNTRUSTED]


class ChangeTarget(StrEnum):
    """Čo sa zmenilo: incident, časť prostredia (firewall, poštový filter, pošta) alebo účtovníctvo."""

    INCIDENT = "incident"
    FIREWALL = "firewall"
    MAIL_ALLOWLIST = "mail_allowlist"
    OUTBOX = "outbox"
    PAYMENT = "payment"
    VENDOR_BANK = "vendor_bank"
    REPLY = "reply"


class StateChange(StrictModel):
    target: ChangeTarget
    target_id: str
    field: str
    before: str
    after: str
    summary: str
