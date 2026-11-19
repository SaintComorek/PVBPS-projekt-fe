# SOC Agent Lab – backend (FastAPI). Závislosti presne podľa uv.lock, embedding model pre RAG je už v obraze,
# takže kontajner pri štarte nič nesťahuje. API kľúč sa do obrazu nedostane: číta sa z .env cez docker compose.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    RUNTIME_DIR=/app/var \
    EMBEDDING_CACHE_DIR=/opt/models

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY soc_demo ./soc_demo
RUN uv sync --frozen

# Stiahne embedding model (~220 MB z HuggingFace) počas buildu, aby demo bežalo aj bez internetu.
RUN python -c "from soc_demo.config import Settings; from soc_demo.llm.embeddings import build_embeddings; \
build_embeddings(Settings()).embed_query('test')"

RUN useradd --create-home --uid 10001 demo \
    && mkdir -p /app/var \
    && chown -R demo:demo /app/var /opt/models

USER demo
EXPOSE 8711
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8711/api/health', timeout=2)"
CMD ["uvicorn", "soc_demo.main:app", "--host", "0.0.0.0", "--port", "8711"]
