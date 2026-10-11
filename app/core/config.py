from decimal import Decimal
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-only-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "WhoDoo"

    database_url: str = "postgresql+asyncpg://localhost/whodoo"
    db_null_pool: bool = False
    db_disable_statement_cache: bool = False

    # local: tokens this app signs with JWT_SECRET (sub = users.id), plus the demo picker when
    #        DEMO_AUTH=1 on a local database. For development and tests.
    # supabase: Supabase Auth access tokens only, verified with ES256 against the project's
    #        JWKS and mapped to a user through users.auth_user_id. See docs/AUTH_BRIDGE.md.
    auth_mode: Literal["local", "supabase"] = "local"
    demo_auth: bool = False
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: Literal["HS256"] = "HS256"
    session_cookie: str = "whodoo_session"
    # No longer used. Kept as a field only so a leftover value stops the app (see check()).
    supabase_jwt_secret: str = ""
    supabase_jwks_cache_seconds: int = Field(600, ge=60, le=3600)
    supabase_jwks_timeout_seconds: float = Field(5.0, ge=1.0, le=10.0)
    supabase_jwt_leeway_seconds: int = Field(30, ge=0, le=60)

    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_storage_bucket: str = "receipts"
    upload_dir: str = "uploads"

    expense_auto_approval_limit: Decimal = Decimal("1000")
    currency: str = "INR"

    # stripe | mock | auto. Stripe is only used with a test-mode secret key; anything
    # else falls back to the mock provider, so a missing key never breaks checkout.
    payment_provider: str = "auto"
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    mock_webhook_secret: str = "whsec_mock_secret"
    # Where Stripe sends the buyer back to after paying.
    app_base_url: str = "http://127.0.0.1:8000"

    # Encrypts stored UPI ids (any string; a Fernet key is derived from it). Blank = derived from
    # JWT_SECRET, acceptable only with demo sign-in (see check()).
    payment_profile_key: str = ""

    # MemberCore renewal reminders: days before expiry, and how often the in-process job runs
    # (0 = only when members open their pages; the job is idempotent either way).
    renewal_reminder_days: str = "30,14,7,1"
    renewal_reminder_interval_seconds: int = Field(3600, ge=0, le=86400)

    # CrewPulse: most unfinished tasks one person may hold before an assignment is flagged (0 = no limit).
    crew_max_open_tasks: int = Field(0, ge=0, le=100)

    # FairSeat ticket booking. On by default; FAIRSEAT_ENABLED=0 makes its routes answer 404.
    # With demo sign-in anyone can act as anyone, so it may only be on against a local database.
    fairseat_enabled: bool = True
    fairseat_hold_seconds: int = 600
    fairseat_quote_seconds: int = 300
    fairseat_max_per_booking: int = 6
    fairseat_max_per_purchaser: int = 6
    # Ticket payments are a second switch, also on by default.
    fairseat_payments_enabled: bool = True
    # Where the payment provider sends a ticket buyer back to. Blank = this app's /tickets/return page.
    fairseat_return_url: str = ""
    # Signs the QR code on each ticket. Blank = JWT_SECRET, which is only acceptable when this
    # app signs its own sign-in tokens with a real one (see check()).
    ticket_credential_secret: str = ""

    # Public event registration. A place is held this long while a paid registration is being paid for
    # (Stripe will not expire a checkout session sooner than 30 minutes).
    registration_hold_seconds: int = 1800
    # Participant details are deleted this many days after the event ends.
    registration_retention_days: int = Field(60, ge=1, le=365)
    # Most registrations one address may submit in ten minutes. 0 turns the limit off.
    registration_rate_limit: int = 20
    # Ticket emails. With no SMTP host they are written to MAIL_OUTBOX_DIR instead of being sent.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_outbox_dir: str = "outbox"

    @property
    def database_is_local(self) -> bool:
        return urlsplit(self.database_url).hostname in (None, "localhost", "127.0.0.1", "::1")

    @property
    def token_secret(self) -> str:
        """The secret for locally issued (AUTH_MODE=local) tokens. Never the Supabase secret."""
        return self.jwt_secret

    @property
    def supabase_auth_issuer(self) -> str:
        return self.supabase_url.strip().rstrip("/") + "/auth/v1"

    @property
    def supabase_jwks_url(self) -> str:
        return self.supabase_auth_issuer + "/.well-known/jwks.json"

    @property
    def use_stripe(self) -> bool:
        if self.payment_provider == "mock":
            return False
        key = self.stripe_secret_key.strip()
        return key.startswith(("sk_test_", "rk_test_")) and "REPLACE" not in key

    @property
    def renewal_reminder_offsets(self) -> tuple[int, ...]:
        days = {int(part) for part in self.renewal_reminder_days.split(",") if part.strip().isdigit()}
        return tuple(sorted((d for d in days if 0 < d <= 365), reverse=True))

    @property
    def profile_secret(self) -> str | None:
        """The key material for UPI encryption. None when only a weak fallback is available
        outside demo sign-in: saving or reading UPI ids is then refused (profile_service)."""
        secret = self.payment_profile_key or self.jwt_secret
        if not self.demo_auth and (secret == DEFAULT_JWT_SECRET or len(secret) < 32):
            return None
        return secret

    def check(self) -> None:
        if self.supabase_jwt_secret:
            raise RuntimeError(
                "SUPABASE_JWT_SECRET is not used and must be removed: Supabase tokens are verified with "
                "ES256 public keys from the project's JWKS (AUTH_MODE=supabase), never with a shared secret"
            )
        if self.auth_mode == "supabase":
            self._check_supabase_auth()
        elif not self.demo_auth and (self.jwt_secret == DEFAULT_JWT_SECRET or len(self.jwt_secret) < 32):
            raise RuntimeError("JWT_SECRET must be set to at least 32 characters when DEMO_AUTH is off")
        if self.fairseat_enabled and self.demo_auth and not self.database_is_local:
            raise RuntimeError(
                "FAIRSEAT_ENABLED cannot be combined with DEMO_AUTH on a database that is not local: "
                "demo sign-in lets anyone book as anyone"
            )
        if self.fairseat_enabled and not self.demo_auth:
            secret = self.ticket_credential_secret or self.jwt_secret
            if secret == DEFAULT_JWT_SECRET or len(secret) < 32:
                raise RuntimeError(
                    "TICKET_CREDENTIAL_SECRET must be set to at least 32 characters when FAIRSEAT_ENABLED is on: "
                    "it signs the QR code on every ticket"
                )
        if self.payment_profile_key and len(self.payment_profile_key) < 32:
            raise RuntimeError("PAYMENT_PROFILE_KEY must be at least 32 characters: it encrypts UPI ids")
        if self.demo_auth and not self.database_is_local:
            raise RuntimeError(
                "DEMO_AUTH cannot be used with a database that is not local: demo sign-in lets anyone act as anyone"
            )

    def _check_supabase_auth(self) -> None:
        if self.demo_auth:
            raise RuntimeError("DEMO_AUTH cannot be combined with AUTH_MODE=supabase")
        parts = urlsplit(self.supabase_url.strip())
        loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")  # the Supabase CLI's local stack
        if (
            not parts.hostname
            or not (parts.scheme == "https" or (parts.scheme == "http" and loopback))
            or parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise RuntimeError(
                "AUTH_MODE=supabase needs SUPABASE_URL set to the project URL, e.g. https://<ref>.supabase.co"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
