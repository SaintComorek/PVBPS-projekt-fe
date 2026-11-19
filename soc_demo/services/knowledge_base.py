"""Znalostná báza pre RAG: IT príručka v Markdowne, chunkovanie podľa nadpisov a vektorové vyhľadávanie.

Chunkovanie: `MarkdownHeaderTextSplitter` rozdelí príručku podľa nadpisov `##` (každá sekcia je chunk s nadpisom
v metadátach), dlhé sekcie ďalej delí `RecursiveCharacterTextSplitter` (CHUNK_SIZE znakov, CHUNK_OVERLAP prekryv).
Vyhľadávanie: embeddingy chunkov v `InMemoryVectorStore`, kosínusová podobnosť s embeddingom otázky.

Neschválené úpravy wiki (útok dema) sa vložia za sekciu s podobným nadpisom a chunkujú sa ako zvyšok textu.
Odpovede helpdesku zamestnancom sa iba simulujú (nič neodchádza do siete).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from soc_demo.domain.knowledge import KbChunk, Reply
from soc_demo.domain.models import ChangeTarget, EvidenceItem, StateChange, TrustLevel
from soc_demo.services.persistence import write_json_atomic

DOC_ID = "KB-IT"
CHUNK_SIZE = 700
CHUNK_OVERLAP = 80
TOP_K = 3
_URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
_APPROVED = "IT príručka, schválená verzia"


def link_hosts(text: str) -> list[str]:
    return list(dict.fromkeys(host.lower().rstrip(".") for host in _URL_HOST.findall(text)))


def is_internal_host(host: str, domains: list[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


class KnowledgeBase:
    def __init__(self, doc_path: Path, state_path: Path, embeddings: Embeddings, internal_domains: list[str]) -> None:
        self._seed_text = doc_path.read_text(encoding="utf-8")
        self._state_path = state_path
        self._embeddings = embeddings
        self._internal_domains = [d.lower() for d in internal_domains]
        self._lock = threading.RLock()
        self._edits: list[EvidenceItem] = []
        self._replies: list[Reply] = []
        self._chunks: list[KbChunk] = []
        self._store: InMemoryVectorStore | None = None
        self.reset()

    @property
    def embedding_label(self) -> str:
        return str(getattr(self._embeddings, "model_name", type(self._embeddings).__name__))

    @property
    def title(self) -> str:
        first = next((line for line in self._seed_text.splitlines() if line.startswith("# ")), DOC_ID)
        return first.removeprefix("# ").strip()

    def reset(self) -> None:
        with self._lock:
            self._edits = []
            self._replies = []
            self._rechunk()
            self._persist()

    # --- čítanie -------------------------------------------------------------------------------

    def chunks(self) -> list[KbChunk]:
        with self._lock:
            return [chunk.model_copy() for chunk in self._chunks]

    def replies(self) -> list[Reply]:
        with self._lock:
            return [reply.model_copy() for reply in self._replies]

    def overview(self) -> list[dict[str, Any]]:
        """Prehľad pre Research Agenta: sekcie a počet chunkov, bez obsahu (obsah sa hľadá nástrojom search_docs)."""
        with self._lock:
            sections: dict[str, int] = {}
            for chunk in self._chunks:
                sections[chunk.section] = sections.get(chunk.section, 0) + 1
            return [
                {
                    "id": DOC_ID,
                    "title": self.title,
                    "sections": [{"section": name, "chunks": count} for name, count in sections.items()],
                }
            ]

    def search(self, query: str, k: int = TOP_K) -> dict[str, Any]:
        with self._lock:
            store = self._index()
            by_id = {chunk.id: chunk for chunk in self._chunks}
            hits = []
            for document, score in store.similarity_search_with_score(query, k=k):
                chunk = by_id[document.metadata["chunk_id"]]
                hits.append(
                    {
                        "chunk_id": chunk.id,
                        "section": chunk.section,
                        "score": round(float(score), 3),
                        "source": chunk.source,
                        "trust": chunk.trust.value,
                        "text": chunk.text,
                        "injected": chunk.injected,
                    }
                )
            return {"query": query, "results": hits}

    def warm(self) -> None:
        """Načíta embedding model a zaindexuje chunky vopred, aby prvé vyhľadávanie v behu nečakalo."""
        with self._lock:
            self._index()

    def has_edit(self, edit_id: str) -> bool:
        with self._lock:
            return any(edit.id == edit_id for edit in self._edits)

    def untrusted_edits(self) -> list[EvidenceItem]:
        with self._lock:
            return [edit.model_copy() for edit in self._edits]

    def approved_text(self) -> str:
        return self._seed_text

    def snapshot(self) -> list[str]:
        with self._lock:
            return [f"{reply.to}: {reply.subject}" for reply in self._replies]

    # --- zápisy (iba cez ToolExecutor alebo injekciu dema) ----------------------------------

    def add_edit(self, edit: EvidenceItem) -> bool:
        with self._lock:
            if self.has_edit(edit.id):
                return False
            self._edits.append(edit.model_copy())
            self._rechunk()
            self._persist()
            return True

    def send_reply(self, to: str, subject: str, body: str) -> tuple[Reply, StateChange]:
        with self._lock:
            hosts = link_hosts(f"{subject}\n{body}")
            external = [host for host in hosts if not is_internal_host(host, self._internal_domains)]
            reply = Reply(
                id=f"REPLY-{len(self._replies) + 1:04d}",
                to=to,
                subject=subject,
                body=body,
                links=hosts,
                external_links=external,
            )
            self._replies.append(reply)
            self._persist()
            links = f" Odkazy: {', '.join(hosts)}." if hosts else ""
            return reply, StateChange(
                target=ChangeTarget.REPLY,
                target_id=to,
                field="reply",
                before="",
                after=subject,
                summary=f"Odpoveď „{subject}“ odoslaná zamestnancovi {to} (simulácia).{links}",
            )

    # --- chunkovanie a index ---------------------------------------------------------------

    def _markdown(self) -> str:
        """Príručka s neschválenými úpravami vloženými za sekciu, ktorej nadpis úprava napodobňuje."""
        text = self._seed_text
        for edit in self._edits:
            heading = next(
                (h for h in re.findall(r"^## (.+)$", text, re.MULTILINE) if edit.title.startswith(h.strip())), None
            )
            if heading is None:
                text = f"{text.rstrip()}\n\n{edit.content}\n"
                continue
            start = text.index(f"## {heading}")
            following = text.find("\n## ", start + 3)
            cut = len(text) if following < 0 else following + 1
            text = f"{text[:cut].rstrip()}\n\n{edit.content}\n\n{text[cut:]}"
        return text

    def _rechunk(self) -> None:
        headers = MarkdownHeaderTextSplitter(
            headers_to_split_on=[("#", "document"), ("##", "section")], strip_headers=False
        ).split_text(self._markdown())
        pieces = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP).split_documents(
            headers
        )
        chunks = []
        for index, piece in enumerate(pieces, 1):
            edit = self._edit_in(piece.page_content)
            chunks.append(
                KbChunk(
                    id=f"{DOC_ID}#{index:02d}",
                    section=str(piece.metadata.get("section") or "Úvod"),
                    text=piece.page_content,
                    chars=len(piece.page_content),
                    trust=TrustLevel.UNTRUSTED if edit else TrustLevel.TRUSTED,
                    source=edit.source if edit else _APPROVED,
                    injected=bool(edit and edit.injected),
                )
            )
        self._chunks = chunks
        self._store = None  # index sa postaví znova pri ďalšom vyhľadávaní

    def _edit_in(self, text: str) -> EvidenceItem | None:
        for edit in self._edits:
            parts = [p.strip() for p in edit.content.split("\n\n") if len(p.strip()) > 40]
            if any(part in text for part in parts):
                return edit
        return None

    def _index(self) -> InMemoryVectorStore:
        if self._store is None:
            store = InMemoryVectorStore(embedding=self._embeddings)
            store.add_documents(
                [Document(page_content=c.text, metadata={"chunk_id": c.id}) for c in self._chunks],
                ids=[c.id for c in self._chunks],
            )
            self._store = store
        return self._store

    def _persist(self) -> None:
        write_json_atomic(
            self._state_path,
            {
                "_comment": "Pracovný stav znalostnej bázy (neschválené úpravy) a odpovedí helpdesku. Reset ho zmaže.",
                "edits": [edit.model_dump() for edit in self._edits],
                "replies": [reply.model_dump() for reply in self._replies],
            },
        )
