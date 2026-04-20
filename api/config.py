from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str  # Required: set DATABASE_URL env var

    # ── Redis (trading state) ─────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Security ──────────────────────────────────────────────────────────────
    jwt_secret_key: str
    master_encryption_key: str  # 64 hex chars = 32 bytes

    # ── CORS ──────────────────────────────────────────────────────────────────
    allowed_origins: list[str] = [
        "https://edgepulse.us",
        "https://www.edgepulse.us",
        "http://localhost:3000",
        "http://localhost:5173",
    ]

    # ── Rate limits ───────────────────────────────────────────────────────────
    rate_limit_auth: str = "5/minute"       # login / register
    rate_limit_api: str = "120/minute"      # general API

    # ── Subscription tier volume limits (cents) ───────────────────────────────
    tier_volume_free: int          = 50_000      # $500
    tier_volume_starter: int       = 500_000     # $5,000
    tier_volume_pro: int           = 5_000_000   # $50,000
    tier_volume_institutional: int = 0           # unlimited (0 = no cap)

    # ── Microsoft OAuth2 (personal Outlook / consumers tenant) ───────────────
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    app_env: str = "development"
    debug: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
