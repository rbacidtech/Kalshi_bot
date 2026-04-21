from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
)

# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

_PASSWORD_RE = re.compile(
    r"^(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*()\-_=+\[\]{};:'\",.<>?/\\|`~]).{8,}$"
)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        if not _PASSWORD_RE.match(v):
            raise ValueError(
                "Password must contain at least one uppercase letter, "
                "one digit, and one special character."
            )
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseModel):
    refresh_token: str


# ---------------------------------------------------------------------------
# USER
# ---------------------------------------------------------------------------


class UserTier(str, Enum):
    free = "free"
    starter = "starter"
    pro = "pro"
    institutional = "institutional"


class UserResponse(BaseModel):
    id: UUID
    email: str
    tier: UserTier
    is_active: bool
    is_admin: bool
    created_at: datetime
    last_login_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class UserUpdate(BaseModel):  # admin only
    tier: Optional[UserTier] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None


# ---------------------------------------------------------------------------
# API KEYS
# ---------------------------------------------------------------------------


class ExchangeType(str, Enum):
    kalshi = "kalshi"
    coinbase = "coinbase"


class APIKeyStoreRequest(BaseModel):
    exchange: ExchangeType
    key_id: str = Field(min_length=1, max_length=256)
    private_key: str = Field(min_length=10, max_length=8192)  # PEM or secret


class APIKeyResponse(BaseModel):
    id: UUID
    exchange: ExchangeType
    created_at: datetime
    last_used_at: Optional[datetime]

    # NOTE: key_id_enc and private_key_enc are intentionally excluded.
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# SUBSCRIPTION
# ---------------------------------------------------------------------------


class SubscriptionResponse(BaseModel):
    tier: UserTier
    volume_limit_cents: int
    current_month_volume_cents: int
    billing_cycle_start: datetime
    is_active: bool

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def volume_used_pct(self) -> float:
        if self.volume_limit_cents <= 0:
            return 0.0
        return min(
            100.0,
            round(self.current_month_volume_cents / self.volume_limit_cents * 100, 2),
        )


# ---------------------------------------------------------------------------
# POSITIONS  (proxied from Redis)
# ---------------------------------------------------------------------------


class PositionSide(str, Enum):
    yes = "yes"
    no = "no"


class PositionResponse(BaseModel):
    ticker: str
    side: PositionSide
    contracts: int
    entry_cents: int
    fair_value: Optional[float]
    fill_confirmed: bool
    entered_at: Optional[str]
    close_time: Optional[str]
    unrealized_pnl_cents: Optional[int]  # computed by API layer, may be None


class PortfolioResponse(BaseModel):
    positions: List[PositionResponse]
    total_deployed_cents: int
    total_unrealized_pnl_cents: int
    balance_cents: Optional[int]          # available cash from Kalshi API
    total_value_cents: Optional[int]      # available + deployed (total account value)
    position_count: int


# ---------------------------------------------------------------------------
# ERROR
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None
