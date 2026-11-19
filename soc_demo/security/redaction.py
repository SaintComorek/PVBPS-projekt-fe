"""Maskovanie tajomstiev v logoch, audite a chybových hláseniach."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

MASK = "***"

_GENERIC_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)(api[_-]?key[\"'\s:=]+)[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"()\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"()\bAIza[0-9A-Za-z_-]{20,}"),
)


class SecretRedactor:
    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        for pattern in _GENERIC_PATTERNS:
            text = pattern.sub(lambda m: f"{m.group(1)}{MASK}", text)
        return text


class RedactingFilter(logging.Filter):
    """Maskuje tajomstvá vo výslednej správe každého log záznamu."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = self._redactor.redact(message)
        record.args = None
        return True
