"""Username and password sign-in (AUTH_MODE=local), and the club's hierarchy.

Passwords are never stored or logged. What is stored is a salted scrypt hash (Python's own
hashlib; no extra dependency), and a sign-in compares hashes in constant time. A wrong username
and a wrong password give the same answer and take about the same time, so the form cannot be
used to find out which usernames exist. After a handful of failures a username is locked for a
few minutes.

Hierarchy. Every person has one role, and the roles are ranked:

    1 President    everything in the club
    2 Treasurer    money: ledger, payouts, memberships, merchandise
    3 Event lead   the events they lead, their tasks and approvals
    4 Member       their own things: orders, tickets, claims
    5 Volunteer    the tasks they are given, and the door at events

Power only goes down the tree. What each role may do is enforced where the action happens
(app/core/dependencies.py and the services); this module is the one place that says the order.
"""

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Text, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.errors import AppError, bad_request
from app.models.base import Base, created_at_col, updated_at_col
from app.models.shared import Club, User

# --- Hierarchy --------------------------------------------------------------------------

HIERARCHY = (
    ("president", "President", "Runs the club. Can do everything below."),
    ("treasurer", "Treasurer", "Looks after the money: ledger, memberships, merchandise."),
    ("event_lead", "Event leads", "Run the events they lead: tasks, approvals, the door."),
    ("member", "Members", "Buy, book, claim and take part."),
    ("volunteer", "Volunteers", "Do the tasks they are given and check people in."),
)
RANK = {role: position for position, (role, _, _) in enumerate(HIERARCHY, start=1)}


def rank(role: str) -> int:
    """1 is the top. An unknown role ranks below everyone."""
    return RANK.get(role, len(HIERARCHY) + 1)


def outranks(role: str, other: str) -> bool:
    return rank(role) < rank(other)


# --- Stored credentials -----------------------------------------------------------------


class UserCredential(Base):
    __tablename__ = "user_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
MIN_PASSWORD = 8
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Compared against when the username does not exist, so that case costs the same as a wrong password.
_DUMMY = hash_password(secrets.token_urlsafe(16))


def username_from(full_name: str, taken: set[str]) -> str:
    """first.last from a name, made unique with a number: "Mia Member" -> mia.member, mia.member2."""
    words = re.sub(r"[^a-z0-9 ]", "", full_name.lower()).split() or ["user"]
    base = ".".join(words[:2])[:36]
    if len(base) < 3:
        base = (base + "user")[:36]
    candidate, number = base, 1
    while candidate in taken:
        number += 1
        candidate = f"{base}{number}"
    return candidate


async def set_credentials(db: AsyncSession, user_id: uuid.UUID, username: str, password: str) -> None:
    """Create or replace someone's username and password."""
    username = username.strip().lower()
    if not USERNAME.match(username):
        raise bad_request("invalid_username", "A username is 3 to 40 letters, digits, dots, dashes or underscores.")
    if len(password) < MIN_PASSWORD:
        raise bad_request("weak_password", f"A password needs at least {MIN_PASSWORD} characters.")
    row = await db.get(UserCredential, user_id)
    if row is None:
        db.add(UserCredential(user_id=user_id, username=username, password_hash=hash_password(password)))
    else:
        row.username, row.password_hash, row.updated_at = username, hash_password(password), datetime.now(timezone.utc)
    await db.flush()


# --- Signing in -------------------------------------------------------------------------

MAX_FAILURES = 5
LOCK_SECONDS = 300
_failures: dict[str, list[float]] = defaultdict(list)


def _locked(key: str) -> bool:
    now = time.monotonic()
    _failures[key] = [at for at in _failures[key] if now - at < LOCK_SECONDS]
    return len(_failures[key]) >= MAX_FAILURES


async def authenticate(db: AsyncSession, username: str, password: str, user_id: uuid.UUID | None = None) -> User:
    """The user these credentials belong to, or a 401 that says nothing about which part was wrong.

    `user_id` is the person picked on the sign-in tree: the credentials must be that person's."""
    key = username.strip().lower()[:60]
    if _locked(key):
        raise AppError(429, "too_many_attempts", "Too many wrong attempts. Wait a few minutes and try again.")
    row = await db.scalar(select(UserCredential).where(UserCredential.username == key))
    ok = verify_password(password, row.password_hash if row else _DUMMY)
    if row is None or not ok or (user_id is not None and row.user_id != user_id):
        _failures[key].append(time.monotonic())
        raise AppError(401, "invalid_credentials", "That username and password do not match this account.")
    _failures.pop(key, None)
    return await db.get(User, row.user_id)


async def change_password(db: AsyncSession, user_id: uuid.UUID, current: str, new: str) -> None:
    row = await db.get(UserCredential, user_id)
    if row is None or not verify_password(current, row.password_hash):
        raise AppError(401, "invalid_credentials", "Your current password is not right.")
    await set_credentials(db, user_id, row.username, new)


async def hierarchy(db: AsyncSession) -> list[dict]:
    """Every club's people arranged by rank, for the sign-in tree. Names and roles only."""
    clubs = list(await db.scalars(select(Club).order_by(Club.created_at)))
    people = list(await db.scalars(select(User).order_by(User.full_name)))
    with_login = set(await db.scalars(select(UserCredential.user_id)))
    out = []
    for club in clubs:
        levels = []
        for role, label, what in HIERARCHY:
            members = [{"id": p.id, "full_name": p.full_name, "can_sign_in": p.id in with_login}
                       for p in people if p.club_id == club.id and p.role == role]
            if members:
                levels.append({"role": role, "label": label, "what": what, "rank": RANK[role], "people": members})
        if levels:
            out.append({"club": club.name, "levels": levels})
    return out
