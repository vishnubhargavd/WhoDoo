"""Locally issued session tokens (AUTH_MODE=local only).

These are HS256 tokens this app signs with JWT_SECRET; `sub` is a users.id. They are for
development, tests and the demo picker. In AUTH_MODE=supabase they are neither issued nor
accepted: app/core/supabase_auth.py handles Supabase tokens, and nothing falls back here.
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import get_settings
from app.core.errors import AppError

TOKEN_TTL = timedelta(hours=12)


def create_access_token(user_id: uuid.UUID) -> str:
    """Issue a local session token for the demo login and tests. `sub` is the user id."""
    settings = get_settings()
    if settings.auth_mode != "local":
        raise RuntimeError("Local session tokens are only issued in AUTH_MODE=local")
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(payload, settings.token_secret, algorithm=settings.jwt_algorithm)


def decode_user_id(token: str) -> uuid.UUID:
    settings = get_settings()
    if settings.auth_mode != "local":
        raise AppError(401, "invalid_token", "Your session is invalid or has expired")
    try:
        payload = jwt.decode(
            token,
            settings.token_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_aud": False, "require": ["sub", "exp"]},
        )
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise AppError(401, "invalid_token", "Your session is invalid or has expired") from exc
