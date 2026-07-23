"""
Application entry point.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.core.logging import configure_logging
from app.api.v1.auth import router as auth_router

logger = logging.getLogger(__name__)


class ProcessTimeMiddleware:
    """
    Pure ASGI middleware that stamps an `X-Process-Time-Ms` header on
    every HTTP response. Implemented as raw ASGI (not BaseHTTPMiddleware)
    to avoid the known call_next() hang risk on some environments.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.perf_counter()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                process_time_ms = (time.perf_counter() - start_time) * 1000
                headers = message.setdefault("headers", [])
                headers.append(
                    (b"x-process-time-ms", f"{process_time_ms:.2f}".encode())
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info(
        "Starting %s | environment=%s | debug=%s",
        settings.PROJECT_NAME,
        settings.ENVIRONMENT,
        settings.DEBUG,
    )
    yield
    logger.info("Shutting down %s", settings.PROJECT_NAME)


def create_application() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        description=(
            "Enterprise Real Estate AI Copilot CRM - Backend API. "
            "Built with FastAPI, PostgreSQL, SQLAlchemy, and JWT authentication."
        ),
        version="0.1.0",
        docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
        redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        lifespan=lifespan,
    )

    if settings.BACKEND_CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_middleware(ProcessTimeMiddleware)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc: Exception):
        
        logger.error("Unhandled exception: %s", exc, exc_info=True)
        logger.exception("Unhandled exception on %s %s", request.method, request.url)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error. Please try again later."},
        )

    @app.get("/", tags=["Root"], summary="API root")
    def read_root():
        return {
            "project": settings.PROJECT_NAME,
            "status": "running",
            "environment": settings.ENVIRONMENT,
        }

    @app.get("/health", tags=["Health"], summary="Health check")
    def health_check():
        return {"status": "ok"}

    app.include_router(
        auth_router,
        prefix=settings.API_V1_PREFIX,
    )

    return app


app = create_application()