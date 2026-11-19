"""Syntetické prostredie okolo incidentov: firewall, poštový filter, odoslaná pošta, playbooky, aktíva."""

from __future__ import annotations

from pydantic import Field

from soc_demo.domain.models import StrictModel, TrustLevel


class Asset(StrictModel):
    """Položka inventára aktív. Inventár je serverová konfigurácia, model ho nevidí."""

    ip: str
    name: str
    role: str
    critical: bool = False


class PlaybookSection(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    trust: TrustLevel
    source: str = Field(min_length=1, max_length=160)
    content: str = Field(max_length=4000)
    injected: bool = False


class PlaybookArticle(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    keywords: list[str] = Field(default_factory=list)
    sections: list[PlaybookSection] = Field(default_factory=list)


class BlockedIp(StrictModel):
    ip: str
    reason: str
    asset_name: str | None = None


class SentEmail(StrictModel):
    message_id: str
    to: str
    subject: str
    body: str
    external: bool


class EnvironmentSeed(StrictModel):
    """Obsah súboru data/seed/environment.json."""

    internal_email_domains: list[str]
    protected_networks: list[str]
    assets: list[Asset]
    firewall_blocklist: list[BlockedIp] = Field(default_factory=list)
    sender_allowlist: list[str] = Field(default_factory=list)
    playbooks: list[PlaybookArticle] = Field(default_factory=list)


class EnvironmentSnapshot(StrictModel):
    """Stav prostredia a účtovníctva pred behom a po ňom (pre porovnanie v UI)."""

    blocked_ips: list[str] = Field(default_factory=list)
    sender_allowlist: list[str] = Field(default_factory=list)
    emails_sent: list[str] = Field(default_factory=list)
    payments: list[str] = Field(default_factory=list)
    vendor_accounts: list[str] = Field(default_factory=list)
    replies: list[str] = Field(default_factory=list)
