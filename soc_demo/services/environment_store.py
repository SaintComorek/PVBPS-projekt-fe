"""Simulované prostredie SOC: firewall, allowlist odosielateľov, odoslaná pošta a playbooky.

Nič z toho nekomunikuje so sieťou. Stav sa drží v pamäti a v `var/environment.json`.
"""

from __future__ import annotations

import json
import threading
import unicodedata
from pathlib import Path
from typing import Any

from soc_demo.domain.environment import (
    Asset,
    BlockedIp,
    EnvironmentSeed,
    EnvironmentSnapshot,
    PlaybookArticle,
    PlaybookSection,
    SentEmail,
)
from soc_demo.domain.errors import NotFoundError
from soc_demo.domain.models import ChangeTarget, StateChange, TrustLevel
from soc_demo.services.persistence import write_json_atomic


def load_environment_seed(path: Path) -> EnvironmentSeed:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    return EnvironmentSeed.model_validate(raw)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


class EnvironmentStore:
    def __init__(self, seed_path: Path, state_path: Path) -> None:
        self._seed_path = seed_path
        self._state_path = state_path
        self._lock = threading.RLock()
        self._seed = load_environment_seed(seed_path)
        self._blocklist: list[BlockedIp] = []
        self._allowlist: list[str] = []
        self._outbox: list[SentEmail] = []
        self._playbooks: dict[str, PlaybookArticle] = {}
        self.reset()

    @property
    def internal_email_domains(self) -> list[str]:
        return list(self._seed.internal_email_domains)

    @property
    def assets(self) -> list[Asset]:
        return [asset.model_copy() for asset in self._seed.assets]

    def reset(self) -> None:
        with self._lock:
            seed = load_environment_seed(self._seed_path)
            self._seed = seed
            self._blocklist = [item.model_copy() for item in seed.firewall_blocklist]
            self._allowlist = list(seed.sender_allowlist)
            self._outbox = []
            self._playbooks = {article.id: article.model_copy(deep=True) for article in seed.playbooks}
            self._persist()

    # --- čítanie -------------------------------------------------------------------------------

    def snapshot(self) -> EnvironmentSnapshot:
        with self._lock:
            return EnvironmentSnapshot(
                blocked_ips=[item.ip for item in self._blocklist],
                sender_allowlist=list(self._allowlist),
                emails_sent=[f"{mail.to}: {mail.subject}" for mail in self._outbox],
            )

    def firewall_blocklist(self) -> list[BlockedIp]:
        with self._lock:
            return [item.model_copy() for item in self._blocklist]

    def sender_allowlist(self) -> list[str]:
        with self._lock:
            return list(self._allowlist)

    def outbox(self) -> list[SentEmail]:
        with self._lock:
            return [mail.model_copy() for mail in self._outbox]

    def playbooks(self) -> list[PlaybookArticle]:
        with self._lock:
            return [article.model_copy(deep=True) for article in self._playbooks.values()]

    def asset_for_ip(self, ip: str) -> Asset | None:
        return next((asset for asset in self._seed.assets if asset.ip == ip), None)

    def search_playbooks(self, query: str) -> dict[str, Any]:
        words = {w for w in _normalize(query).replace(",", " ").split() if len(w) >= 3}
        with self._lock:
            hits = [
                article.model_dump(mode="json", exclude={"keywords"})
                for article in self._playbooks.values()
                if words & {_normalize(k) for k in article.keywords}
                or any(w in _normalize(article.title) for w in words)
            ]
            result: dict[str, Any] = {"query": query, "results": hits}
            if not hits:
                result["available_articles"] = [article.title for article in self._playbooks.values()]
            return result

    def untrusted_sections(self) -> list[tuple[PlaybookArticle, PlaybookSection]]:
        with self._lock:
            return [
                (article, section)
                for article in self._playbooks.values()
                for section in article.sections
                if section.trust is TrustLevel.UNTRUSTED
            ]

    def trusted_sections(self) -> list[PlaybookSection]:
        with self._lock:
            return [s for a in self._playbooks.values() for s in a.sections if s.trust is TrustLevel.TRUSTED]

    # --- zápisy (iba cez ToolExecutor alebo injekciu dema) ----------------------------------

    def has_section(self, article_id: str, section_id: str) -> bool:
        with self._lock:
            article = self._playbooks.get(article_id)
            return bool(article and any(s.id == section_id for s in article.sections))

    def add_section(self, article_id: str, section: PlaybookSection) -> bool:
        with self._lock:
            article = self._playbooks.get(article_id)
            if article is None:
                raise NotFoundError(f"Playbook {article_id} neexistuje.")
            if self.has_section(article_id, section.id):
                return False
            article.sections.append(section.model_copy())
            self._persist()
            return True

    def block_ip(self, ip: str, reason: str) -> StateChange:
        with self._lock:
            asset = self.asset_for_ip(ip)
            already = any(item.ip == ip for item in self._blocklist)
            if not already:
                self._blocklist.append(BlockedIp(ip=ip, reason=reason, asset_name=asset.name if asset else None))
                self._persist()
            impact = f" Zasiahnutý vlastný systém {asset.name} ({asset.role})." if asset else ""
            return StateChange(
                target=ChangeTarget.FIREWALL,
                target_id=ip,
                field="blocked",
                before="áno" if already else "nie",
                after="áno",
                summary=f"Firewall: zablokovaná IP {ip}.{impact}",
            )

    def allowlist_sender(self, domain: str) -> StateChange:
        with self._lock:
            already = domain in self._allowlist
            if not already:
                self._allowlist.append(domain)
                self._persist()
            return StateChange(
                target=ChangeTarget.MAIL_ALLOWLIST,
                target_id=domain,
                field="allowlisted",
                before="áno" if already else "nie",
                after="áno",
                summary=f"Poštový filter: doména {domain} je na allowliste, jej e-maily obídu kontrolu.",
            )

    def send_email(self, to: str, subject: str, body: str) -> tuple[SentEmail, StateChange]:
        with self._lock:
            domain = to.rsplit("@", 1)[-1].lower()
            mail = SentEmail(
                message_id=f"MSG-{len(self._outbox) + 1:04d}",
                to=to,
                subject=subject,
                body=body,
                external=domain not in self._seed.internal_email_domains,
            )
            self._outbox.append(mail)
            self._persist()
            where = "mimo organizáciu" if mail.external else "v rámci organizácie"
            return mail, StateChange(
                target=ChangeTarget.OUTBOX,
                target_id=to,
                field="sent",
                before="",
                after=subject,
                summary=f"E-mail „{subject}“ odoslaný na {to} ({where}, simulácia).",
            )

    def _persist(self) -> None:
        write_json_atomic(
            self._state_path,
            {
                "_comment": "Pracovný stav simulovaného prostredia. Prepíše sa pri resete dema.",
                "firewall_blocklist": [item.model_dump() for item in self._blocklist],
                "sender_allowlist": self._allowlist,
                "outbox": [mail.model_dump() for mail in self._outbox],
                "playbooks": [article.model_dump(mode="json") for article in self._playbooks.values()],
            },
        )
