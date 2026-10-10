from decimal import Decimal
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-only-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "WhoDoo"

    database_url: str = "postgresql+asyncpg://localhost/whodoo"
    db_null_pool: bool = False
    db_disable_statement_cache: bool = False

    demo_auth: bool = False
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    session_cookie: str = "whodoo_session"
    supabase_jwt_secret: str = ""

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

    # FairSeat ticket booking. Off by default: the routes answer 404 until it is switched on.
    # With demo sign-in anyone can act as anyone, so it may only be on against a local database.
    fairseat_enabled: bool = False
    fairseat_hold_seconds: int = 600
    fairseat_quote_seconds: int = 300
    fairseat_max_per_booking: int = 6
    fairseat_max_per_purchaser: int = 6

    @property
    def database_is_local(self) -> bool:
        return urlsplit(self.database_url).hostname in (None, "localhost", "127.0.0.1", "::1")

    @property
    def token_secret(self) -> str:
        return self.supabase_jwt_secret or self.jwt_secret

    @property
    def use_stripe(self) -> bool:
        if self.payment_provider == "mock":
            return False
        key = self.stripe_secret_key.strip()
        return key.startswith(("sk_test_", "rk_test_")) and "REPLACE" not in key

    def check(self) -> None:
        if not self.demo_auth and self.token_secret == DEFAULT_JWT_SECRET:
            raise RuntimeError(
                "JWT_SECRET (or SUPABASE_JWT_SECRET) must be set when DEMO_AUTH is off"
            )
        if self.fairseat_enabled and self.demo_auth and not self.database_is_local:
            raise RuntimeError(
                "FAIRSEAT_ENABLED cannot be combined with DEMO_AUTH on a database that is not local: "
                "demo sign-in lets anyone book as anyone"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
