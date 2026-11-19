"""Znalostná báza pre RAG (IT príručka v Markdowne) a odpovede helpdesku zamestnancom."""

from __future__ import annotations

from pydantic import Field

from soc_demo.domain.models import StrictModel, TrustLevel


class KbChunk(StrictModel):
    """Jeden chunk príručky: kus sekcie, ktorý sa embedduje a vracia pri vyhľadávaní."""

    id: str
    section: str
    text: str
    chars: int
    trust: TrustLevel  # schválená verzia príručky = trusted, neschválená úprava wiki = untrusted
    source: str
    injected: bool = False  # iba pre UI dema, model ho nevidí


class Reply(StrictModel):
    """Odpoveď helpdesku zamestnancovi (simulované odoslanie)."""

    id: str
    to: str
    subject: str
    body: str
    links: list[str] = Field(default_factory=list)  # hostitelia odkazov v odpovedi
    external_links: list[str] = Field(default_factory=list)  # z toho mimo firemných domén
