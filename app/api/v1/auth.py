"""Signing in and out with a username and password (AUTH_MODE=local).

A successful sign-in sets the session cookie the rest of the app already reads. The password
travels only in the request body, is never logged, and is never sent back.
"""

import uuid

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.core.errors import AppError
from app.core.security import TOKEN_TTL, create_access_token
from app.services import audit_service, auth_service, membership_service, modules, reminder_service

router = APIRouter(prefix="/auth", tags=["Sign-in"])


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)
    # The person picked on the sign-in tree. The credentials must be theirs.
    user_id: uuid.UUID | None = None


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=auth_service.MIN_PASSWORD, max_length=200)


def _local_only() -> None:
    if get_settings().auth_mode != "local":
        raise AppError(404, "not_found", "Password sign-in is not used in this deployment")


@router.post("/login", dependencies=[Depends(_local_only)])
async def login(body: LoginIn, response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    """Check a username and password and start a session. A wrong username and a wrong password
    get the same answer."""
    user = await auth_service.authenticate(db, body.username, body.password, body.user_id)
    # Every sign-in checks the membership's expiry: a reminder is written if it runs out within
    # the week (or already has), and the bell in the top bar shows it.
    if modules.migrated("reminders") or "reminders" in await modules.load(db):
        await membership_service.expire_lapsed(db, user.club_id)
        await reminder_service.generate(db, user_id=user.id)
    await membership_service.keep_president_a_member(db, user)
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="auth.signed_in",
                         entity_type="user", entity_id=user.id, details=None)
    await db.commit()
    response.set_cookie(
        get_settings().session_cookie, create_access_token(user.id), max_age=int(TOKEN_TTL.total_seconds()),
        httponly=True, samesite="lax",
    )
    return {"id": user.id, "full_name": user.full_name, "role": user.role, "rank": auth_service.rank(user.role)}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(get_settings().session_cookie)
    return {"signed_out": True}


@router.put("/password", dependencies=[Depends(_local_only)])
async def change_password(
    body: PasswordChange, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Change your own password. The current one is needed."""
    await auth_service.change_password(db, user.id, body.current_password, body.new_password)
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="auth.password_changed",
                         entity_type="user", entity_id=user.id, details=None)
    await db.commit()
    return {"changed": True, "status_label": "changed", "title": "Password"}
