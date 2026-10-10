"""Shared FastAPI dependencies: database session, current user, role checks.

`get_current_user` is the contract other modules can rely on. It returns a
`CurrentUser` with `id`, `club_id`, `role` and `is_active_member`. When the real
auth module lands, only this file and app/services/shared_directory.py change.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Callable

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import supabase_auth
from app.core.config import get_settings
from app.core.errors import AppError, forbidden
from app.core.security import decode_user_id
from app.db.database import SessionLocal
from app.services import shared_directory

logger = logging.getLogger("whodoo.auth")

ROLE_PRESIDENT = "president"
ROLE_TREASURER = "treasurer"
ROLE_EVENT_LEAD = "event_lead"
ROLE_VOLUNTEER = "volunteer"
ROLE_MEMBER = "member"

FINANCE_ADMINS = (ROLE_TREASURER, ROLE_PRESIDENT)
APPROVERS = (ROLE_EVENT_LEAD, ROLE_TREASURER, ROLE_PRESIDENT)


class CurrentUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    club_id: uuid.UUID
    role: str
    is_active_member: bool
    full_name: str = ""
    email: str = ""

    @property
    def is_finance_admin(self) -> bool:
        return self.role in FINANCE_ADMINS


async def get_db() -> AsyncIterator[AsyncSession]:
    """One session per request. Routes commit explicitly; anything uncommitted is rolled back."""
    async with SessionLocal() as session:
        yield session


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _extract_token(request: Request) -> str | None:
    """Local mode: the bearer header, else the demo session cookie."""
    return _bearer_token(request) or request.cookies.get(get_settings().session_cookie)


async def _supabase_user(request: Request, db: AsyncSession):
    """Supabase mode: a verified ES256 token, then users.auth_user_id. Bearer header only.

    Club, role and membership come from the users row, never from the token.
    """
    token = _bearer_token(request)
    if not token:
        raise AppError(401, "not_authenticated", "Please sign in")
    identity = await supabase_auth.verify_access_token(token)
    try:
        user = await shared_directory.get_user_by_auth_user_id(db, identity.auth_user_id)
    except shared_directory.AuthIdentityUnavailable as exc:
        logger.error("Supabase sign-in refused, identity mapping unavailable: %s", exc)
        raise AppError(503, "auth_unavailable", "Sign-in is temporarily unavailable, please try again") from None
    if user is None:
        logger.info("Supabase sign-in refused: no application user linked to this auth user")
        raise AppError(401, "account_not_linked", "This sign-in is not linked to a WhoDoo account")
    return user


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> CurrentUser:
    """Resolve the caller. AUTH_MODE decides how; the token never does."""
    if get_settings().auth_mode == "supabase":
        user = await _supabase_user(request, db)
    else:
        token = _extract_token(request)
        if not token:
            raise AppError(401, "not_authenticated", "Please sign in")
        user = await shared_directory.get_user(db, decode_user_id(token))
        if user is None:
            raise AppError(401, "unknown_user", "This account no longer exists")
    return CurrentUser(
        id=user.id,
        club_id=user.club_id,
        role=user.role,
        is_active_member=user.is_active_member,
        full_name=user.full_name,
        email=user.email,
    )


async def get_optional_user(request: Request, db: AsyncSession = Depends(get_db)) -> CurrentUser | None:
    """The caller if they are signed in, otherwise None. For pages that have a public version."""
    try:
        return await get_current_user(request, db)
    except AppError as exc:
        if exc.status_code == 401:
            return None
        raise


def require_roles(*roles: str) -> Callable[..., CurrentUser]:
    """Dependency factory: allow only the given roles."""

    async def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise forbidden(f"This action needs one of these roles: {', '.join(roles)}")
        return user

    return dependency


require_finance_admin = require_roles(*FINANCE_ADMINS)
require_approver = require_roles(*APPROVERS)
