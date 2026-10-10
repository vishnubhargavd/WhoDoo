"""Supabase Auth bridge: ES256/JWKS verification, auth_user_id mapping, mode and startup safety.

Keys are generated here and served by a fake JWKS provider; no real Supabase token or
network call is involved. The `users.auth_user_id` column (MemberCore migration 030) is
not part of the shared schema yet, so tests that need it add it to the disposable test
database for their duration and drop it afterwards.
"""

import hashlib
import hmac
import logging
import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text

from app.core import supabase_auth
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import create_access_token, decode_user_id
from app.core.supabase_auth import JwksCache, JwksUnavailable, SupabaseTokenVerifier, TokenRejected
from app.models.shared import Club, Event, User
from app.services import shared_directory

SUPABASE_URL = "https://testproject.supabase.co"
ISSUER = SUPABASE_URL + "/auth/v1"
LOCAL_DB = "postgresql+asyncpg://localhost/whodoo"
REMOTE_DB = "postgresql+asyncpg://user:secret@db.example.supabase.co:5432/postgres"
STRONG_SECRET = "s" * 40


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_jwk(key: ec.EllipticCurvePrivateKey, kid: str) -> dict:
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "alg": "ES256", "use": "sig"}


def make_token(key, kid: str | None = "key-1", *, algorithm: str = "ES256", **claims) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": "authenticated",
        "sub": str(uuid.uuid4()),
        "role": "authenticated",
        "iat": now,
        "exp": now + 3600,
        "session_id": str(uuid.uuid4()),
        "email": "someone@example.com",
    }
    payload.update(claims)
    payload = {name: value for name, value in payload.items() if value is not None}
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": kid} if kid is not None else None)


class FakeJwks:
    """Stands in for https://<project>/auth/v1/.well-known/jwks.json."""

    def __init__(self, **keys: ec.EllipticCurvePrivateKey):
        self.keys = dict(keys)
        self.calls = 0
        self.down = False
        self.document: dict | None = None

    async def __call__(self):
        self.calls += 1
        if self.down:
            raise JwksUnavailable("simulated outage")
        if self.document is not None:
            return self.document
        return {"keys": [public_jwk(key, kid) for kid, key in self.keys.items()]}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_verifier(jwks: FakeJwks, clock: Clock | None = None, *, ttl: float = 600) -> SupabaseTokenVerifier:
    cache = JwksCache(jwks, ttl=ttl, clock=clock or Clock())
    return SupabaseTokenVerifier(issuer=ISSUER, jwks=cache, leeway=30)


async def rejected(verifier: SupabaseTokenVerifier, token: str) -> TokenRejected:
    with pytest.raises(TokenRejected) as info:
        await verifier.verify(token)
    return info.value


# --- The verifier ----------------------------------------------------------------------


async def test_valid_es256_token_gives_the_auth_user_id():
    key = new_key()
    sub = uuid.uuid4()
    identity = await make_verifier(FakeJwks(**{"key-1": key})).verify(make_token(key, sub=str(sub)))
    assert identity.auth_user_id == sub
    assert identity.session_id
    assert not hasattr(identity, "email")  # nothing beyond the identity is carried forward


async def test_signature_from_another_key_with_the_same_kid_is_rejected():
    key, forger = new_key(), new_key()
    verifier = make_verifier(FakeJwks(**{"key-1": key}))
    assert "InvalidSignatureError" in (await rejected(verifier, make_token(forger))).reason
    tampered = make_token(key)
    head, body, sig = tampered.split(".")
    other_body = make_token(key, sub=str(uuid.uuid4())).split(".")[1]
    await rejected(verifier, f"{head}.{other_body}.{sig}")


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://otherproject.supabase.co/auth/v1"},
        {"iss": SUPABASE_URL},  # no /auth/v1
        {"iss": ISSUER + "/"},
        {"iss": None},
        {"aud": "anon"},
        {"aud": "service_role"},
        {"aud": None},
    ],
    ids=["other-project", "bare-url", "trailing-slash", "no-iss", "aud-anon", "aud-service-role", "no-aud"],
)
async def test_wrong_or_missing_issuer_and_audience_are_rejected(claims):
    key = new_key()
    await rejected(make_verifier(FakeJwks(**{"key-1": key})), make_token(key, **claims))


async def test_expired_token_is_rejected_and_marked_expired():
    key = new_key()
    now = int(time.time())
    error = await rejected(make_verifier(FakeJwks(**{"key-1": key})), make_token(key, iat=now - 7200, exp=now - 3600))
    assert error.expired


async def test_expiry_within_the_leeway_is_still_accepted():
    key = new_key()
    now = int(time.time())
    await make_verifier(FakeJwks(**{"key-1": key})).verify(make_token(key, iat=now - 3600, exp=now - 10))


@pytest.mark.parametrize(
    "claims",
    [
        {"iat": int(time.time()) + 600},
        {"nbf": int(time.time()) + 600},
        {"iat": None},
        {"exp": None},
    ],
    ids=["issued-in-future", "not-yet-valid", "no-iat", "no-exp"],
)
async def test_time_claims_are_enforced(claims):
    key = new_key()
    await rejected(make_verifier(FakeJwks(**{"key-1": key})), make_token(key, **claims))


@pytest.mark.parametrize(
    "sub",
    [None, "", "not-a-uuid", "123", str(uuid.uuid4()).upper(), uuid.uuid4().hex, "urn:uuid:" + str(uuid.uuid4())],
    ids=["missing", "empty", "text", "number", "uppercase", "no-hyphens", "urn"],
)
async def test_missing_or_non_canonical_subject_is_rejected(sub):
    key = new_key()
    await rejected(make_verifier(FakeJwks(**{"key-1": key})), make_token(key, sub=sub))


@pytest.mark.parametrize("claims", [{"role": "anon"}, {"role": "service_role"}, {"role": None}, {"is_anonymous": True}])
async def test_only_signed_in_non_anonymous_users_are_accepted(claims):
    key = new_key()
    await rejected(make_verifier(FakeJwks(**{"key-1": key})), make_token(key, **claims))


async def test_unknown_kid_refreshes_once_then_is_rate_limited():
    key = new_key()
    jwks = FakeJwks(**{"key-1": key})
    clock = Clock()
    verifier = make_verifier(jwks, clock)
    await verifier.verify(make_token(key))
    assert jwks.calls == 1

    clock.now += supabase_auth.MIN_REFRESH_INTERVAL
    assert (await rejected(verifier, make_token(key, kid="nobody-knows"))).reason == "unknown kid"
    assert jwks.calls == 2  # one refresh, in case the key was just rotated in
    for _ in range(5):
        await rejected(verifier, make_token(key, kid=f"random-{uuid.uuid4()}"))
    assert jwks.calls == 2  # random kids cannot make us hammer Supabase

    clock.now += supabase_auth.MIN_REFRESH_INTERVAL
    await rejected(verifier, make_token(key, kid="nobody-knows"))
    assert jwks.calls == 3


@pytest.mark.parametrize("kid", [None, "", "k" * 200], ids=["missing", "empty", "huge"])
async def test_missing_or_invalid_kid_is_rejected_without_a_fetch(kid):
    key = new_key()
    jwks = FakeJwks(**{"key-1": key})
    await rejected(make_verifier(jwks), make_token(key, kid=kid))
    assert jwks.calls == 0


async def test_jwks_outage_with_nothing_cached_is_unavailable_not_a_bypass():
    key = new_key()
    jwks = FakeJwks(**{"key-1": key})
    jwks.down = True
    clock = Clock()
    verifier = make_verifier(jwks, clock)
    with pytest.raises(JwksUnavailable):
        await verifier.verify(make_token(key))
    with pytest.raises(JwksUnavailable):  # retries are rate limited too
        await verifier.verify(make_token(key))
    assert jwks.calls == 1


async def test_jwks_outage_keeps_recent_keys_for_a_bounded_time():
    key = new_key()
    jwks = FakeJwks(**{"key-1": key})
    clock = Clock()
    verifier = make_verifier(jwks, clock, ttl=600)
    await verifier.verify(make_token(key))

    jwks.down = True
    clock.now += 700  # cache expired, refresh fails: recent keys keep working
    await verifier.verify(make_token(key))
    clock.now += supabase_auth.MAX_STALE_SECONDS  # far past the stale window
    with pytest.raises(JwksUnavailable):
        await verifier.verify(make_token(key))


@pytest.mark.parametrize(
    "document",
    [
        {"keys": []},  # a project still on the legacy shared secret publishes no keys
        {"nope": 1},
        [],
        {"keys": [{"kty": "oct", "kid": "key-1", "k": "c2VjcmV0"}]},
        {"keys": [{"kty": "RSA", "kid": "key-1", "n": "AQAB", "e": "AQAB"}]},
    ],
    ids=["empty", "no-keys", "not-an-object", "symmetric-key", "rsa-key"],
)
async def test_jwks_without_es256_keys_is_unavailable(document):
    key = new_key()
    jwks = FakeJwks()
    jwks.document = document
    with pytest.raises(JwksUnavailable):
        await make_verifier(jwks).verify(make_token(key))


async def test_jwks_entry_with_private_key_material_is_ignored():
    key = new_key()
    jwks = FakeJwks()
    private = jwt.algorithms.ECAlgorithm.to_jwk(key, as_dict=True)
    jwks.document = {"keys": [{**private, "kid": "key-1"}]}
    with pytest.raises(JwksUnavailable):
        await make_verifier(jwks).verify(make_token(key))


async def test_key_rotation_picks_up_new_keys_and_drops_revoked_ones():
    old, new = new_key(), new_key()
    jwks = FakeJwks(old=old)
    clock = Clock()
    verifier = make_verifier(jwks, clock, ttl=600)
    await verifier.verify(make_token(old, kid="old"))

    jwks.keys = {"old": old, "new": new}  # Supabase publishes the next key
    clock.now += supabase_auth.MIN_REFRESH_INTERVAL
    await verifier.verify(make_token(new, kid="new"))  # unknown kid -> one refresh -> accepted
    await verifier.verify(make_token(old, kid="old"))

    jwks.keys = {"new": new}  # the old key is revoked
    clock.now += 600
    await verifier.verify(make_token(new, kid="new"))
    await rejected(verifier, make_token(old, kid="old"))


async def test_hs256_and_other_algorithms_are_rejected_before_any_key_lookup():
    key = new_key()
    jwks = FakeJwks(**{"key-1": key})
    verifier = make_verifier(jwks)
    legacy = make_token("a-supabase-legacy-jwt-secret-of-enough-length", algorithm="HS256")
    demo = jwt.encode({"sub": str(uuid.uuid4()), "exp": int(time.time()) + 600}, "x" * 40, algorithm="HS256")
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    header = jwt.utils.base64url_encode(b'{"alg":"HS256","kid":"key-1","typ":"JWT"}').decode()
    body = make_token(key).split(".")[1]
    # Algorithm confusion: HMAC "signed" with the public key's PEM as the secret.
    confused_sig = jwt.utils.base64url_encode(hmac.new(pem, f"{header}.{body}".encode(), hashlib.sha256).digest())
    confused = f"{header}.{body}.{confused_sig.decode()}"
    unsigned_header = jwt.utils.base64url_encode(b'{"alg":"none","kid":"key-1"}').decode()
    unsigned = f"{unsigned_header}.{body}."
    for token in (legacy, demo, confused, unsigned):
        assert (await rejected(verifier, token)).reason == "algorithm not allowed"
    assert jwks.calls == 0


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "x" * 9000, "....."], ids=["empty", "junk", "dots", "huge", "only-dots"])
async def test_malformed_tokens_are_rejected(token):
    await rejected(make_verifier(FakeJwks(**{"key-1": new_key()})), token)


async def test_api_errors_are_generic_and_never_echo_the_token(monkeypatch, caplog):
    key = new_key()
    verifier = make_verifier(FakeJwks(**{"key-1": key}))
    monkeypatch.setattr(supabase_auth, "get_verifier", lambda: verifier)
    caplog.set_level(logging.DEBUG)
    now = int(time.time())
    cases = {
        "invalid_token": make_token(new_key()),
        "token_expired": make_token(key, iat=now - 7200, exp=now - 3600),
    }
    for expected_code, token in cases.items():
        with pytest.raises(AppError) as info:
            await supabase_auth.verify_access_token(token)
        error = info.value
        assert (error.status_code, error.code, error.details) == (401, expected_code, None)
        assert error.__cause__ is None and error.__suppress_context__
        rendered = f"{error.message} {error!r}"
        assert token not in rendered and "Signature" not in rendered and "kid" not in rendered
        assert token not in caplog.text
        assert token.split(".")[2] not in caplog.text


async def test_http_fetcher_reads_the_project_jwks():
    key = new_key()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"keys": [public_jwk(key, "key-1")]})

    url = ISSUER + "/.well-known/jwks.json"
    fetch = supabase_auth.http_jwks_fetcher(url, 5.0, transport=httpx.MockTransport(handler))
    verifier = SupabaseTokenVerifier(issuer=ISSUER, jwks=JwksCache(fetch, ttl=600), leeway=30)
    await verifier.verify(make_token(key))
    assert seen == [url]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://evil.example/jwks.json"}),
        httpx.Response(500),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b'{"keys": []' + b" " * supabase_auth.MAX_JWKS_BYTES + b"}"),
    ],
    ids=["redirect", "server-error", "not-json", "too-large"],
)
async def test_http_fetcher_refuses_bad_responses(response):
    fetch = supabase_auth.http_jwks_fetcher(ISSUER, 5.0, transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(JwksUnavailable):
        await fetch()


async def test_http_fetcher_turns_timeouts_into_unavailable():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    fetch = supabase_auth.http_jwks_fetcher(ISSUER, 1.0, transport=httpx.MockTransport(handler))
    with pytest.raises(JwksUnavailable):
        await fetch()


# --- Startup configuration ---------------------------------------------------------------


def test_supabase_mode_needs_a_project_url():
    Settings(auth_mode="supabase", supabase_url=SUPABASE_URL, demo_auth=False).check()
    Settings(auth_mode="supabase", supabase_url="http://127.0.0.1:54321", demo_auth=False).check()  # Supabase CLI
    for url in ("", "http://testproject.supabase.co", "https://user:pw@testproject.supabase.co",
                "https://testproject.supabase.co/auth/v1", "https://testproject.supabase.co?x=1", "not a url"):
        with pytest.raises(RuntimeError, match="SUPABASE_URL"):
            Settings(auth_mode="supabase", supabase_url=url, demo_auth=False).check()


def test_supabase_mode_derives_issuer_and_jwks_url():
    settings = Settings(auth_mode="supabase", supabase_url=SUPABASE_URL + "/")
    assert settings.supabase_auth_issuer == ISSUER
    assert settings.supabase_jwks_url == ISSUER + "/.well-known/jwks.json"


def test_demo_sign_in_is_refused_with_supabase_mode_or_a_remote_database():
    with pytest.raises(RuntimeError, match="DEMO_AUTH"):
        Settings(auth_mode="supabase", supabase_url=SUPABASE_URL, demo_auth=True, database_url=LOCAL_DB).check()
    with pytest.raises(RuntimeError, match="DEMO_AUTH"):
        Settings(demo_auth=True, database_url=REMOTE_DB, fairseat_enabled=False).check()
    Settings(demo_auth=True, database_url=LOCAL_DB).check()


def test_the_supabase_jwt_secret_is_never_used_as_a_key():
    with pytest.raises(RuntimeError, match="SUPABASE_JWT_SECRET"):
        Settings(supabase_jwt_secret="legacy-secret", demo_auth=True, database_url=LOCAL_DB).check()
    with pytest.raises(RuntimeError, match="SUPABASE_JWT_SECRET"):
        Settings(auth_mode="supabase", supabase_url=SUPABASE_URL, supabase_jwt_secret="legacy-secret").check()
    assert Settings(jwt_secret=STRONG_SECRET, supabase_jwt_secret="legacy").token_secret == STRONG_SECRET


def test_local_mode_without_demo_needs_a_strong_secret():
    for secret in ("dev-only-change-me", "short"):
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            Settings(demo_auth=False, jwt_secret=secret, database_url=REMOTE_DB).check()
    Settings(demo_auth=False, jwt_secret=STRONG_SECRET, database_url=REMOTE_DB).check()


def test_local_tokens_cannot_use_another_algorithm():
    with pytest.raises(ValueError):
        Settings(jwt_algorithm="none")
    with pytest.raises(ValueError):
        Settings(supabase_jwks_cache_seconds=10)
    with pytest.raises(ValueError):
        Settings(supabase_jwks_timeout_seconds=120)


# --- Through the API -------------------------------------------------------------------


@pytest.fixture
def supabase_mode(monkeypatch):
    """Switch the running app to AUTH_MODE=supabase with a fake JWKS for one test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "supabase")
    monkeypatch.setattr(settings, "demo_auth", False)
    monkeypatch.setattr(settings, "supabase_url", SUPABASE_URL)
    monkeypatch.setattr(shared_directory, "_auth_identity_ready", False)
    key = new_key()
    jwks = FakeJwks(**{"key-1": key})
    verifier = make_verifier(jwks)
    monkeypatch.setattr(supabase_auth, "get_verifier", lambda: verifier)

    def bearer(sub: uuid.UUID | str, **claims) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token(key, sub=str(sub), **claims)}"}

    bearer.jwks = jwks
    bearer.key = key
    return bearer


@pytest.fixture
async def identity_column(db):
    """What migration 030 adds, on the disposable test database only, for this test only."""
    await db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_user_id uuid UNIQUE"))
    await db.commit()
    yield
    await db.rollback()
    await db.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS auth_user_id"))
    await db.commit()


async def link(db, user, auth_user_id: uuid.UUID | None = None) -> uuid.UUID:
    auth_user_id = auth_user_id or uuid.uuid4()
    await db.execute(text("UPDATE users SET auth_user_id = :a WHERE id = :u"), {"a": auth_user_id, "u": user.id})
    await db.commit()
    return auth_user_id


def code(response) -> str:
    return response.json()["error"]["code"]


async def test_linked_user_signs_in_with_a_supabase_token(client, world, db, supabase_mode, identity_column):
    auth_id = await link(db, world.president)
    # scope=all: the fixture's events have fixed dates, which the default "upcoming" drops once past.
    response = await client.get("/api/v1/events?scope=all", headers=supabase_mode(auth_id))
    assert response.status_code == 200
    assert {e["name"] for e in response.json()} == {"Bake Sale", "Tech Fest"}
    response = await client.get("/api/v1/ledger/summary", headers=supabase_mode(auth_id))
    assert response.status_code == 200


async def test_unlinked_auth_user_gets_401_even_with_a_matching_email(client, world, db, supabase_mode, identity_column):
    # Same email as a real user, but no auth_user_id link: email never links accounts.
    response = await client.get("/api/v1/events", headers=supabase_mode(uuid.uuid4(), email=world.president.email))
    assert (response.status_code, code(response)) == (401, "account_not_linked")


async def test_supabase_sub_is_never_read_as_a_users_id(client, world, db, supabase_mode, identity_column):
    response = await client.get("/api/v1/events", headers=supabase_mode(world.president.id))
    assert (response.status_code, code(response)) == (401, "account_not_linked")


async def test_role_and_club_come_from_the_database_not_the_token(client, world, db, supabase_mode, identity_column):
    other = Club(id=uuid.uuid4(), name="Other Club")
    db.add(other)
    await db.flush()
    # Published on both sides: a plain member never sees drafts, so only the club decides what is listed.
    other_event = Event(id=uuid.uuid4(), club_id=other.id, name="Other Gala", status="published")
    db.add(other_event)
    await db.execute(text("UPDATE events SET status = 'published' WHERE club_id = :c"), {"c": world.club.id})
    await db.commit()
    auth_id = await link(db, world.member)
    claims = {
        "app_metadata": {"club_id": str(other.id), "role": "president", "provider": "email"},
        "user_metadata": {"club_id": str(other.id), "role": "treasurer", "is_active_member": True},
        "club_id": str(other.id),
        "user_role": "president",
    }
    headers = supabase_mode(auth_id, **claims)

    response = await client.get("/api/v1/ledger/summary", headers=headers)
    assert (response.status_code, code(response)) == (403, "forbidden")  # still a plain member
    response = await client.get("/api/v1/events?scope=all", headers=headers)
    assert response.status_code == 200 and {e["club_id"] for e in response.json()} == {str(world.club.id)}
    response = await client.get(f"/api/v1/events/{other_event.id}", headers=headers)
    assert response.status_code == 404  # the other club stays invisible


async def test_unlinking_or_demoting_takes_effect_on_the_next_request(client, world, db, supabase_mode, identity_column):
    auth_id = await link(db, world.treasurer)
    headers = supabase_mode(auth_id)
    assert (await client.get("/api/v1/ledger/summary", headers=headers)).status_code == 200

    await db.execute(text("UPDATE users SET role = 'member' WHERE id = :u"), {"u": world.treasurer.id})
    await db.commit()
    assert (await client.get("/api/v1/ledger/summary", headers=headers)).status_code == 403

    await db.execute(text("UPDATE users SET auth_user_id = NULL WHERE id = :u"), {"u": world.treasurer.id})
    await db.commit()
    response = await client.get("/api/v1/events", headers=headers)  # same, still unexpired token
    assert (response.status_code, code(response)) == (401, "account_not_linked")


async def test_supabase_mode_fails_closed_without_migration_030(client, world, db, supabase_mode):
    response = await client.get("/api/v1/events", headers=supabase_mode(world.president.id))
    assert (response.status_code, code(response)) == (503, "auth_unavailable")
    assert "auth_user_id" not in response.text and "migration" not in response.text
    assert shared_directory._auth_identity_ready is False


async def test_supabase_mode_fails_closed_if_auth_user_id_is_not_unique(client, world, db, supabase_mode):
    await db.execute(text("ALTER TABLE users ADD COLUMN auth_user_id uuid"))
    await db.commit()
    try:
        response = await client.get("/api/v1/events", headers=supabase_mode(uuid.uuid4()))
        assert (response.status_code, code(response)) == (503, "auth_unavailable")
    finally:
        await db.rollback()
        await db.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS auth_user_id"))
        await db.commit()


async def test_local_tokens_and_cookies_are_refused_in_supabase_mode(client, world, db, auth, identity_column, monkeypatch):
    local_headers = auth(world.president)  # issued while still in local mode
    session_cookie = local_headers["Authorization"].removeprefix("Bearer ")
    key = new_key()
    verifier = make_verifier(FakeJwks(**{"key-1": key}))
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "supabase")
    monkeypatch.setattr(settings, "demo_auth", False)
    monkeypatch.setattr(settings, "supabase_url", SUPABASE_URL)
    monkeypatch.setattr(supabase_auth, "get_verifier", lambda: verifier)

    response = await client.get("/api/v1/events", headers=local_headers)
    assert (response.status_code, code(response)) == (401, "invalid_token")
    client.cookies.set(settings.session_cookie, session_cookie)
    response = await client.get("/api/v1/events")
    assert (response.status_code, code(response)) == (401, "not_authenticated")
    with pytest.raises(RuntimeError):
        create_access_token(world.president.id)
    with pytest.raises(AppError):
        decode_user_id(session_cookie)


async def test_supabase_token_is_refused_in_local_mode(client, world, db, auth, identity_column):
    key = new_key()
    auth_id = await link(db, world.president, auth_user_id=world.president.id)  # even if ids happened to match
    token = make_token(key, sub=str(auth_id))
    response = await client.get("/api/v1/events", headers={"Authorization": f"Bearer {token}"})
    assert (response.status_code, code(response)) == (401, "invalid_token")
    assert (await client.get("/api/v1/events", headers=auth(world.president))).status_code == 200


async def test_local_demo_sign_in_still_works(client, world, auth):
    assert get_settings().auth_mode == "local" and get_settings().demo_auth
    assert (await client.get("/api/v1/events", headers=auth(world.member))).status_code == 200
    forged = jwt.encode({"sub": str(world.president.id), "exp": int(time.time()) + 600}, "wrong-" * 8, algorithm="HS256")
    response = await client.get("/api/v1/events", headers={"Authorization": f"Bearer {forged}"})
    assert (response.status_code, code(response)) == (401, "invalid_token")
    response = await client.post("/dev/login", data={"user_id": str(world.member.id), "next": "/home"})
    assert response.status_code == 303 and get_settings().session_cookie in response.cookies


async def test_jwks_outage_is_a_503_through_the_api(client, world, db, supabase_mode, identity_column):
    supabase_mode.jwks.down = True
    response = await client.get("/api/v1/events", headers=supabase_mode(await link(db, world.president)))
    assert (response.status_code, code(response)) == (503, "auth_unavailable")


async def test_api_error_bodies_leak_nothing(client, world, db, supabase_mode, identity_column, caplog):
    caplog.set_level(logging.DEBUG)
    auth_id = await link(db, world.president)
    now = int(time.time())
    expired = supabase_mode(auth_id, iat=now - 7200, exp=now - 3600)
    forged = {"Authorization": f"Bearer {make_token(new_key(), sub=str(auth_id))}"}
    unlinked = supabase_mode(uuid.uuid4())
    for headers, expected in ((expired, "token_expired"), (forged, "invalid_token"), (unlinked, "account_not_linked")):
        response = await client.get("/api/v1/events", headers=headers)
        assert (response.status_code, code(response)) == (401, expected)
        token = headers["Authorization"].removeprefix("Bearer ")
        body = response.text
        assert response.json()["error"]["details"] is None
        for secret in (token, token.split(".")[2], get_settings().jwt_secret, "Traceback", "jwt.", "kid"):
            assert secret not in body
        assert token not in caplog.text and token.split(".")[2] not in caplog.text


async def test_user_model_is_unchanged_until_migration_030():
    assert "auth_user_id" not in User.__table__.columns
