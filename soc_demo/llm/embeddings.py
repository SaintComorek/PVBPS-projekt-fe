"""Embeddingy pre RAG.

`FastEmbedEmbeddings`: lokálny viacjazyčný model cez fastembed (ONNX, bez PyTorch). Model sa pri prvom použití
stiahne z verejného HuggingFace do `EMBEDDING_CACHE_DIR` a potom beží offline.
`HashingEmbeddings`: iba pre testy a prostredie bez modelu. Vrecko slov hashované do pevného počtu rozmerov,
deterministické a bez siete. Nie je to sémantický model.
"""

from __future__ import annotations

import math
import re
import threading
import zlib
from pathlib import Path
from typing import Any

from langchain_core.embeddings import Embeddings

from soc_demo.config import Settings


class FastEmbedEmbeddings(Embeddings):
    def __init__(self, model_name: str, cache_dir: Path) -> None:
        self.model_name = model_name
        self._cache_dir = cache_dir
        self._model: Any = None
        self._lock = threading.Lock()

    def _get(self) -> Any:
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding  # import až pri použití: načítanie ONNX trvá

                self._cache_dir.mkdir(parents=True, exist_ok=True)
                self._model = TextEmbedding(self.model_name, cache_dir=str(self._cache_dir))
            return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._get().embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class HashingEmbeddings(Embeddings):
    model_name = "hashing-bow-256 (iba testy)"
    dimensions = 256

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in re.findall(r"\w+", text.lower()):
            vector[zlib.crc32(word.encode("utf-8")) % self.dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def build_embeddings(settings: Settings) -> Embeddings:
    if settings.embedding_backend == "hashing":
        return HashingEmbeddings()
    cache_dir = settings.embedding_cache_dir or settings.runtime_dir / "models"
    return FastEmbedEmbeddings(settings.embedding_model, cache_dir)
