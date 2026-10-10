import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import get_settings
from app.core.errors import AppError

TOKEN_TTL = timedelta(hours=12)


def create_access_token(user_id: uuid.UUID) -> str:
    """Issue a session token for the demo login. Same shape as a Supabase JWT: `sub` is the user id."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(payload, settings.token_secret, algorithm=settings.jwt_algorithm)


def decode_user_id(token: str) -> uuid.UUID:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.token_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_aud": False, "require": ["sub", "exp"]},
        )
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise AppError(401, "invalid_token", "Your session is invalid or has expired") from exc
