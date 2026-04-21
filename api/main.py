from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from api.config import get_settings
from api.database import get_db, init_db
from api.redis_client import close_redis
from api.routers.admin import router as admin_router
from api.routers.auth import limiter, router as auth_router
from api.routers.keys import router as keys_router
from api.routers.microsoft import router as microsoft_router
from api.routers.positions import router as positions_router
from api.routers.controls import router as controls_router
from api.routers.advisor import router as advisor_router
from api.routers.performance import router as performance_router
from api.routers.subscriptions import router as subscriptions_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("EdgePulse API starting (env=%s)", settings.app_env)
    await init_db()
    logger.info("Database tables verified")
    yield
    await close_redis()
    logger.info("EdgePulse API shutting down")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="EdgePulse API",
        version="1.0.0",
        description="Multi-tenant prediction market trading platform",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Rate limiter ──────────────────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(admin_router)
    app.include_router(advisor_router)
    app.include_router(auth_router)
    app.include_router(microsoft_router)
    app.include_router(keys_router)
    app.include_router(positions_router)
    app.include_router(subscriptions_router)
    app.include_router(controls_router)
    app.include_router(performance_router)

    # ── Global exception handlers ─────────────────────────────────────────────
    @app.exception_handler(status.HTTP_422_UNPROCESSABLE_ENTITY)
    async def validation_error_handler(request: Request, exc: Any) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": exc.errors() if hasattr(exc, "errors") else str(exc)},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["meta"], include_in_schema=False)
    async def health(db: AsyncSession = Depends(get_db)) -> dict:
        checks: dict[str, str] = {"api": "ok", "env": settings.app_env}
        # Redis check
        try:
            from api.redis_client import get_redis
            r = get_redis()
            await r.ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"error: {e}"
        # DB check
        try:
            await db.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as e:
            checks["db"] = f"error: {e}"

        overall = "ok" if all(v in ("ok", settings.app_env) for v in checks.values()) else "degraded"
        checks["status"] = overall
        return checks

    # ── Static dashboard (serve built React app) ──────────────────────────────
    dist_dir = Path(__file__).parent.parent / "dashboard" / "dist"
    if dist_dir.exists():
        app.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str) -> FileResponse:
            index = dist_dir / "index.html"
            return FileResponse(str(index))

    return app


app = create_app()
