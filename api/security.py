"""
EdgePulse API security layer.

Three independent subsystems:
  1. Password hashing  — passlib bcrypt, rounds=12
  2. JWT tokens        — python-jose HS256 (access + refresh)
  3. API key encryption — AES-256-GCM via cryptography hazmat
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext

# ---------------------------------------------------------------------------
# 1. Password hashing
# ---------------------------------------------------------------------------

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain* (rounds=12)."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*."""
    return _pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# 2. JWT tokens
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set. "
            "Set it before importing this module."
        )
    return value


SECRET_KEY: str = _require_env("JWT_SECRET_KEY")
_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
REFRESH_TOKEN_EXPIRE_DAYS: int = 30


def create_access_token(subject: str, extra: dict | None = None) -> str:
    """
    Create a signed HS256 access JWT.

    Args:
        subject: The user identifier (typically user UUID/id as a string).
        extra:   Optional additional claims merged into the payload.

    Returns:
        Encoded JWT string.
    """
    if extra is None:
        extra = {}

    now = datetime.now(tz=timezone.utc)
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    payload: dict = {
        **extra,
        "sub": subject,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=_ALGORITHM)


def create_refresh_token() -> tuple[str, str]:
    """
    Generate a refresh token pair.

    Returns:
        (raw_token, token_hash) where raw_token is the value sent to the
        client and token_hash (SHA-256 hex) is what must be stored in the DB.
    """
    raw_token: str = secrets.token_urlsafe(64)
    token_hash: str = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash


def decode_access_token(token: str) -> dict:
    """
    Decode and validate an access JWT.

    Raises:
        HTTPException(401): if the token is invalid, expired, or not of
                            type "access".

    Returns:
        The decoded payload dict.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload: dict = jwt.decode(token, SECRET_KEY, algorithms=[_ALGORITHM])
    except JWTError:
        raise credentials_exception

    if payload.get("type") != "access":
        raise credentials_exception

    return payload


# ---------------------------------------------------------------------------
# 3. API key encryption (AES-256-GCM)
# ---------------------------------------------------------------------------

def _load_master_key() -> bytes:
    raw = os.environ.get("MASTER_ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError(
            "Required environment variable 'MASTER_ENCRYPTION_KEY' is not set."
        )
    try:
        key_bytes = bytes.fromhex(raw)
    except ValueError:
        raise RuntimeError(
            "MASTER_ENCRYPTION_KEY must be a valid hexadecimal string."
        )
    if len(key_bytes) != 32:
        raise RuntimeError(
            f"MASTER_ENCRYPTION_KEY must decode to exactly 32 bytes "
            f"(got {len(key_bytes)}). Provide a 64-character hex string."
        )
    return key_bytes


MASTER_KEY: bytes = _load_master_key()

# GCM authentication tag is always 16 bytes
_GCM_TAG_LENGTH = 16


def encrypt_value(plaintext: str) -> tuple[bytes, bytes, bytes]:
    """
    Encrypt *plaintext* with AES-256-GCM.

    Returns:
        (ciphertext, iv, tag)
        - iv  — 12 random bytes (nonce); must be stored alongside ciphertext.
        - tag — 16-byte GCM authentication tag; must be stored alongside ciphertext.
        - ciphertext — encrypted bytes (without the tag).
    """
    iv: bytes = os.urandom(12)
    aesgcm = AESGCM(MASTER_KEY)
    # AESGCM.encrypt() returns ciphertext || tag (tag appended at end)
    combined: bytes = aesgcm.encrypt(iv, plaintext.encode(), None)
    ciphertext: bytes = combined[: len(combined) - _GCM_TAG_LENGTH]
    tag: bytes = combined[len(combined) - _GCM_TAG_LENGTH :]
    return ciphertext, iv, tag


def decrypt_value(ciphertext: bytes, iv: bytes, tag: bytes) -> str:
    """
    Decrypt AES-256-GCM encrypted data.

    Args:
        ciphertext: Encrypted bytes (without tag).
        iv:         12-byte nonce used during encryption.
        tag:        16-byte GCM authentication tag.

    Returns:
        Decrypted plaintext string.

    Raises:
        HTTPException(500): on any decryption failure (e.g., tampered data,
                            wrong key). Key material is never included in the
                            error detail.
    """
    try:
        aesgcm = AESGCM(MASTER_KEY)
        combined: bytes = ciphertext + tag
        plaintext_bytes: bytes = aesgcm.decrypt(iv, combined, None)
        return plaintext_bytes.decode()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Decryption failed",
        )
