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
    #        JWKS and mapped to a user through users.auth_user_id. See README_AUTH_BRIDGE.md.
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
    # Razorpay test mode settings
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpayx_account_number: str = ""
    # Where Stripe / Razorpay sends the buyer back to after paying.
    app_base_url: str = "http://127.0.0.1:8000"

    # FairSeat ticket booking. Off by default: the routes answer 404 until it is switched on.
    # With demo sign-in anyone can act as anyone, so it may only be on against a local database.
    fairseat_enabled: bool = False
    fairseat_hold_seconds: int = 600
    fairseat_quote_seconds: int = 300
    fairseat_max_per_booking: int = 6
    fairseat_max_per_purchaser: int = 6
    # Read-only membership API and FairSeat source. Requires reviewed pending 030/031.
    membercore_enabled: bool = False
    # GateSync is opt-in; keep the credential encryption key on the server only.
    gatesync_enabled: bool = False
    gatesync_credential_key: str = ""
    # Ticket payments are a second switch, also off by default.
    fairseat_payments_enabled: bool = False
    # Where the payment provider sends a ticket buyer back to. Blank = this API's own return route.
    fairseat_return_url: str = ""

    # Administrator-configured approved campus venues
    approved_venues: str = (
        "Main Hall, Auditorium, Open Air Theatre, Student Activity Center, "
        "Seminar Hall A, Seminar Hall B, Seminar Room 1, Campus Grounds, "
        "Campus Quad, Sports Complex, Amphitheatre, Hall"
    )

    def get_approved_venues(self) -> list[str]:
        return [v.strip() for v in self.approved_venues.split(",") if v.strip()]

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
    def use_razorpay(self) -> bool:
        if self.payment_provider == "mock":
            return False
        key = self.razorpay_key_id.strip()
        return key.startswith("rzp_test_") and bool(self.razorpay_key_secret.strip())

    def check(self) -> None:
        if self.razorpay_key_id.strip().startswith("rzp_live_"):
            raise RuntimeError("Production Razorpay keys (rzp_live_) are strictly forbidden. Only test mode is allowed.")
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
