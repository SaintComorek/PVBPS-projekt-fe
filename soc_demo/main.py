"""FastAPI aplikácia SOC Agent Lab."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from soc_demo import __version__
from soc_demo.api import routes_audit, routes_demo, routes_runs
from soc_demo.config import Settings, get_settings
from soc_demo.container import Container, build_container
from soc_demo.domain.errors import DemoError
from soc_demo.security.redaction import RedactingFilter, SecretRedactor

logger = logging.getLogger("soc_demo.api")


def configure_logging(level: str, redactor: SecretRedactor) -> None:
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler.addFilter(RedactingFilter(redactor))
        root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _warm_knowledge(container: Container) -> None:
    try:
        container.knowledge.warm()
        logger.info("knowledge_base_ready chunks=%d", len(container.knowledge.chunks()))
    except Exception as exc:  # noqa: BLE001 – bez modelu dema funguje všetko okrem vyhľadávania v znalostnej báze
        logger.warning("knowledge_base_unavailable error=%s", exc.__class__.__name__)


def create_app(settings: Settings | None = None, llm_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(settings, llm_transport=llm_transport)
        configure_logging(settings.log_level, container.redactor)
        app.state.container = container
        logger.info(
            "startup version=%s llm_configured=%s llm_model=%s llm_host=%s",
            __version__,
            settings.llm_configured,
            settings.llm_model,
            settings.llm_endpoint_host,
        )
        # Embedding model pre RAG sa načíta na pozadí, aby prvé vyhľadávanie v behu nečakalo.
        threading.Thread(target=_warm_knowledge, args=(container,), name="kb-warm", daemon=True).start()
        yield

    app = FastAPI(title="SOC Agent Lab", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.endswith("/events"):
            logger.info(
                "request id=%s %s %s status=%d ms=%d",
                request_id,
                request.method,
                request.url.path,
                response.status_code,
                int((time.perf_counter() - started) * 1000),
            )
        return response

    @app.exception_handler(DemoError)
    async def demo_error_handler(request: Request, exc: DemoError) -> JSONResponse:
        return _error(request, exc.http_status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors())
        return _error(request, 422, "VALIDATION_ERROR", f"Neplatná požiadavka: {problems}")

    app.include_router(routes_demo.router)
    app.include_router(routes_runs.router)
    app.include_router(routes_audit.router)
    return app


def _error(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
    )


app = create_app()
