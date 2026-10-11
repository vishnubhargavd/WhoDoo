"""Supabase Auth access-token verification (ES256, keys from the project's JWKS).

Used only when AUTH_MODE=supabase. Verifies who signed the token and nothing more: the
result is the Supabase Auth user id (`sub`). Club, role and membership never come from
the token; app/core/dependencies.py looks them up in `users` through `auth_user_id`.

Policy (see docs/AUTH_BRIDGE.md):
- the header must say alg ES256 and carry a kid; anything else is rejected before any
  key is looked at, so the mode is never chosen from the token;
- the key is the JWKS entry with that kid (EC P-256 only); unknown kids trigger at most
  one rate-limited refresh, which is how key rotation is picked up;
- iss must equal <SUPABASE_URL>/auth/v1 exactly, aud must be "authenticated";
- exp and iat are required, nbf is enforced when present, all with a small leeway;
- sub must be a canonical UUID, role must be "authenticated", anonymous users are refused.

Errors raised here never carry the token or library messages to the client, and the
token is never logged.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from app.core.config import get_settings
from app.core.errors import AppError

logger = logging.getLogger("whodoo.auth")

ALGORITHM = "ES256"
AUDIENCE = "authenticated"
MAX_TOKEN_BYTES = 8192
MAX_KID_LENGTH = 128
MAX_JWKS_BYTES = 64 * 1024
MAX_JWKS_KEYS = 20
# An unknown kid may force a refresh at most this often, so random kids cannot hammer Supabase.
MIN_REFRESH_INTERVAL = 30.0
# If Supabase is unreachable, keys fetched within this window keep working.
MAX_STALE_SECONDS = 3600.0


class JwksUnavailable(Exception):
    """The signing keys could not be fetched and no usable copy is cached."""


class TokenRejected(Exception):
    """The token failed verification. `reason` is for logs only, never for the client."""

    def __init__(self, reason: str, *, expired: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.expired = expired


@dataclass(frozen=True)
class VerifiedIdentity:
    auth_user_id: uuid.UUID
    session_id: str | None
    expires_at: int


def _parse_jwks(document: Any) -> dict[str, jwt.PyJWK]:
    """Keep only public EC P-256 signing keys usable for ES256."""
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise JwksUnavailable("JWKS document has no keys list")
    entries = document["keys"]
    if len(entries) > MAX_JWKS_KEYS:
        raise JwksUnavailable("JWKS document has too many keys")
    keys: dict[str, jwt.PyJWK] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        usable = (
            isinstance(kid, str)
            and 0 < len(kid) <= MAX_KID_LENGTH
            and entry.get("kty") == "EC"
            and entry.get("crv") == "P-256"
            and entry.get("alg", ALGORITHM) == ALGORITHM
            and entry.get("use", "sig") == "sig"
            and "d" not in entry  # never accept private key material as a verification key
        )
        if not usable or kid in keys:
            continue
        try:
            keys[kid] = jwt.PyJWK(entry, algorithm=ALGORITHM)
        except jwt.PyJWTError:
            logger.warning("Ignoring an unreadable JWKS entry (kid=%s)", kid)
    if not keys:
        raise JwksUnavailable(
            "JWKS has no ES256 signing keys; the Supabase project may still use the legacy shared secret"
        )
    return keys


def http_jwks_fetcher(
    url: str, timeout: float, *, transport: httpx.AsyncBaseTransport | None = None
) -> Callable[[], Awaitable[Any]]:
    """Fetch the JWKS over HTTPS with bounded time and size. No redirects are followed."""

    async def fetch() -> Any:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(timeout, 3.0)), follow_redirects=False, transport=transport
            ) as http:
                response = await http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise JwksUnavailable(f"JWKS request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise JwksUnavailable(f"JWKS request returned HTTP {response.status_code}")
        if len(response.content) > MAX_JWKS_BYTES:
            raise JwksUnavailable("JWKS response is too large")
        try:
            return json.loads(response.content)
        except ValueError:
            raise JwksUnavailable("JWKS response is not JSON") from None

    return fetch


class JwksCache:
    """Signing keys by kid, refreshed every `ttl` seconds or on an unknown kid (rate limited).

    A refresh replaces the whole set, so a key Supabase has revoked stops working at the
    next refresh. A failed refresh keeps the previous keys for up to MAX_STALE_SECONDS.
    """

    def __init__(
        self,
        fetch: Callable[[], Awaitable[Any]],
        *,
        ttl: float,
        min_refresh_interval: float = MIN_REFRESH_INTERVAL,
        max_stale: float = MAX_STALE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fetch = fetch
        self._ttl = ttl
        self._min_refresh_interval = min_refresh_interval
        self._max_stale = max_stale
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock: asyncio.Lock | None = None

    def _may_attempt(self, now: float) -> bool:
        return self._last_attempt is None or now - self._last_attempt >= self._min_refresh_interval

    async def get_key(self, kid: str) -> jwt.PyJWK | None:
        now = self._clock()
        if self._fetched_at is None or now - self._fetched_at >= self._ttl:
            await self._refresh(reason="expired")
        key = self._keys.get(kid)
        if key is None and self._may_attempt(self._clock()):
            await self._refresh(reason="unknown kid")
            key = self._keys.get(kid)
        return key

    async def _refresh(self, *, reason: str) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        started = self._last_attempt
        async with self._lock:
            now = self._clock()
            if self._last_attempt != started or not self._may_attempt(now):
                # Someone else refreshed while we waited, or we tried too recently.
                if self._fetched_at is None or now - self._fetched_at >= self._max_stale:
                    raise JwksUnavailable("Signing keys are not available")
                return
            self._last_attempt = now
            try:
                keys = _parse_jwks(await self._fetch())
            except JwksUnavailable as exc:
                if self._fetched_at is not None and now - self._fetched_at < self._max_stale:
                    logger.warning("JWKS refresh (%s) failed, keeping cached keys: %s", reason, exc)
                    return
                logger.error("JWKS refresh (%s) failed and no usable keys are cached: %s", reason, exc)
                self._keys = {}
                self._fetched_at = None
                raise
            except Exception as exc:  # a fetcher bug must not become an auth bypass or a 500 loop
                logger.error("JWKS refresh (%s) failed unexpectedly: %s", reason, type(exc).__name__)
                raise JwksUnavailable("JWKS refresh failed") from None
            self._keys = keys
            self._fetched_at = now
            logger.info("JWKS refreshed (%s): %d signing key(s)", reason, len(keys))


class SupabaseTokenVerifier:
    def __init__(self, *, issuer: str, jwks: JwksCache, leeway: int):
        self.issuer = issuer
        self.jwks = jwks
        self.leeway = leeway

    async def verify(self, token: str) -> VerifiedIdentity:
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise TokenRejected("token missing or too long")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenRejected("malformed header") from None
        if header.get("alg") != ALGORITHM:
            # Covers "none", HS256 (legacy secret or demo tokens) and every other algorithm.
            raise TokenRejected("algorithm not allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not 0 < len(kid) <= MAX_KID_LENGTH:
            raise TokenRejected("missing or invalid kid")

        key = await self.jwks.get_key(kid)  # JwksUnavailable propagates: we cannot verify at all
        if key is None:
            raise TokenRejected("unknown kid")

        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenRejected("expired", expired=True) from None
        except jwt.PyJWTError as exc:
            raise TokenRejected(f"claims or signature invalid ({type(exc).__name__})") from None

        sub = claims.get("sub")
        try:
            auth_user_id = uuid.UUID(sub) if isinstance(sub, str) else None
        except ValueError:
            auth_user_id = None
        if auth_user_id is None or str(auth_user_id) != sub:
            raise TokenRejected("sub is not a canonical UUID")
        if claims.get("role") != AUDIENCE:
            raise TokenRejected("role claim is not authenticated")
        if claims.get("is_anonymous") is True:
            raise TokenRejected("anonymous sign-in")
        session_id = claims.get("session_id")
        return VerifiedIdentity(
            auth_user_id=auth_user_id,
            session_id=session_id if isinstance(session_id, str) else None,
            expires_at=int(claims["exp"]),
        )


_verifier: SupabaseTokenVerifier | None = None
_verifier_config: tuple | None = None


def get_verifier() -> SupabaseTokenVerifier:
    """The process-wide verifier for the current settings; rebuilt if they change."""
    global _verifier, _verifier_config
    settings = get_settings()
    config = (
        settings.supabase_auth_issuer,
        settings.supabase_jwks_url,
        settings.supabase_jwks_cache_seconds,
        settings.supabase_jwks_timeout_seconds,
        settings.supabase_jwt_leeway_seconds,
    )
    if _verifier is None or _verifier_config != config:
        jwks = JwksCache(
            http_jwks_fetcher(settings.supabase_jwks_url, settings.supabase_jwks_timeout_seconds),
            ttl=settings.supabase_jwks_cache_seconds,
        )
        _verifier = SupabaseTokenVerifier(
            issuer=settings.supabase_auth_issuer, jwks=jwks, leeway=settings.supabase_jwt_leeway_seconds
        )
        _verifier_config = config
    return _verifier


async def verify_access_token(token: str) -> VerifiedIdentity:
    """Verify a Supabase access token, or raise AppError with a client-safe message."""
    try:
        return await get_verifier().verify(token)
    except TokenRejected as exc:
        logger.info("Rejected Supabase access token: %s", exc.reason)
        if exc.expired:
            raise AppError(401, "token_expired", "Your session has expired, please sign in again") from None
        raise AppError(401, "invalid_token", "Your session is invalid or has expired") from None
    except JwksUnavailable:
        raise AppError(503, "auth_unavailable", "Sign-in is temporarily unavailable, please try again") from None
