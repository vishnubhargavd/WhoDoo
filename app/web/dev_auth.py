"""Demo sign-in: pick a seeded user. Mounted only when DEMO_AUTH=1.

TEMPORARY. Replace with the real login from the member/auth module.
"""

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import get_db
from app.core.errors import not_found
from app.core.security import TOKEN_TTL, create_access_token
from app.services import shared_directory
from app.web.templating import render

router = APIRouter(include_in_schema=False)


def _safe_next(target: str | None) -> str:
    # After signing in, people go to their own home, never back to the public landing page.
    safe = bool(target) and target.startswith("/") and not target.startswith("//") and target != "/"
    return target if safe else "/home"


@router.get("/dev/login")
async def login_page(request: Request, next: str = "/home", db: AsyncSession = Depends(get_db)):
    users = await shared_directory.list_users(db)
    return render(request, "dev_login.html", users=users, next=_safe_next(next))


@router.post("/dev/login")
async def login(user_id: uuid.UUID = Form(...), next: str = Form("/home"), db: AsyncSession = Depends(get_db)):
    user = await shared_directory.get_user(db, user_id)
    if user is None:
        raise not_found("User")
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        get_settings().session_cookie,
        create_access_token(user.id),
        max_age=int(TOKEN_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)  # "Switch user": straight back to the picker
    response.delete_cookie(get_settings().session_cookie)
    return response
