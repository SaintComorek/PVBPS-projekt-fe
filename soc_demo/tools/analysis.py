"""Deterministická analýza závažnosti zo štruktúrovaných (dôveryhodných) signálov."""

from __future__ import annotations

from pydantic import BaseModel

from soc_demo.domain.models import Incident, Severity, Signal

SIGNAL_RULES: dict[Signal, tuple[int, str, str]] = {
    Signal.DATA_EXFILTRATION: (40, "Únik alebo export dát", "Zastaviť export a ukončiť aktívne relácie."),
    Signal.CREDENTIAL_EXPOSURE: (
        35,
        "Uniknuté prihlasovacie údaje",
        "Okamžite rotovať kľúč a skontrolovať jeho použitie.",
    ),
    Signal.PRIVILEGED_ACCOUNT: (25, "Privilegovaný účet", "Dočasne obmedziť práva účtu."),
    Signal.PHISHING: (25, "Phishing", "Zablokovať odosielateľa a doménu, vyhľadať ďalších príjemcov."),
    Signal.MFA_BYPASS: (20, "Obídenie MFA", "Vynútiť MFA a zrušiť registráciu neznámeho zariadenia."),
    Signal.PUBLIC_EXPOSURE: (20, "Verejná expozícia", "Odstrániť tajomstvo z histórie repozitára."),
    Signal.AI_SUPPLY_CHAIN: (15, "AI dodávateľský reťazec", "Obmedziť kľúč na známe IP a nastaviť limity spotreby."),
    Signal.NEW_DEVICE_SIGNIN: (15, "Prihlásenie z nového zariadenia", "Overiť zariadenie s používateľom."),
    Signal.USER_REPORTED: (5, "Nahlásené používateľom", "Poďakovať používateľovi a uzavrieť spätnú väzbu."),
}

SEVERITY_THRESHOLDS: tuple[tuple[int, Severity], ...] = (
    (80, Severity.CRITICAL),
    (55, Severity.HIGH),
    (30, Severity.MEDIUM),
)


class AnalysisFactor(BaseModel):
    signal: Signal
    weight: int
    label: str


class IncidentAnalysis(BaseModel):
    incident_id: str
    computed_severity: Severity
    score: int
    factors: list[AnalysisFactor]
    recommended_actions: list[str]
    untrusted_content_present: bool


def severity_from_score(score: int) -> Severity:
    for threshold, severity in SEVERITY_THRESHOLDS:
        if score >= threshold:
            return severity
    return Severity.LOW


def analyze_incident(incident: Incident) -> IncidentAnalysis:
    factors = [
        AnalysisFactor(signal=signal, weight=SIGNAL_RULES[signal][0], label=SIGNAL_RULES[signal][1])
        for signal in incident.signals
    ]
    factors.sort(key=lambda f: f.weight, reverse=True)
    score = min(100, sum(f.weight for f in factors))
    return IncidentAnalysis(
        incident_id=incident.id,
        computed_severity=severity_from_score(score),
        score=score,
        factors=factors,
        recommended_actions=[SIGNAL_RULES[f.signal][2] for f in factors],
        untrusted_content_present=bool(incident.untrusted_evidence()),
    )
