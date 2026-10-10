from decimal import Decimal
from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
