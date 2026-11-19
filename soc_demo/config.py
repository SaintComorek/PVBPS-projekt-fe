"""Konfigurácia z premenných prostredia. Žiadne tajomstvá v kóde."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"


class Settings(BaseSettings):
    # settings.env: zdieľané nastavenia bez tajomstiev (napr. Groq URL a model).
    # .env: tajomstvá používateľa (API kľúč). Neskorší súbor má prednosť.
    model_config = SettingsConfigDict(
        env_file=("settings.env", ".env"), env_file_encoding="utf-8", extra="ignore", validate_by_name=True
    )

    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("LLM_API_KEY", "GROQ_API_KEY"))
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    llm_temperature: float = Field(default=0.2, ge=0, le=2)
    llm_max_tokens: int = Field(default=1500, ge=128, le=16384)
    llm_reasoning_effort: Literal["low", "medium", "high"] | None = None
    llm_json_mode: bool = False
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    # langchain = LangChain ChatOpenAI + nástroje @tool (predvolené), native-tools = natívny tool calling
    # cez vlastný HTTP klient, json-tools = volania v JSON texte (modely bez function callingu)
    llm_tool_protocol: Literal["langchain", "native-tools", "json-tools"] = "langchain"

    # RAG: lokálny embedding model (fastembed) alebo deterministické hashovanie bez modelu (testy)
    embedding_backend: Literal["fastembed", "hashing"] = "fastembed"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_cache_dir: Path | None = None  # predvolene {RUNTIME_DIR}/models

    max_agent_steps: int = Field(default=6, ge=1, le=10)
    replay_event_delay_ms: int = Field(default=450, ge=0, le=5000)
    live_event_delay_ms: int = Field(default=60, ge=0, le=5000)

    runtime_dir: Path = Path("var")
    cors_origins: list[str] = ["http://localhost:5711", "http://127.0.0.1:5711"]
    log_level: str = "INFO"

    @field_validator("llm_base_url", "llm_model", "llm_reasoning_effort", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("llm_base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LLM_BASE_URL musí byť http(s) URL, napr. http://localhost:11434/v1")
        return value.rstrip("/")

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def llm_api_key_configured(self) -> bool:
        return self.llm_api_key_value is not None

    @property
    def llm_endpoint_host(self) -> str | None:
        return urlparse(self.llm_base_url).netloc if self.llm_base_url else None

    @property
    def llm_api_key_value(self) -> str | None:
        if self.llm_api_key is None:
            return None
        value = self.llm_api_key.get_secret_value().strip()
        return value or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
