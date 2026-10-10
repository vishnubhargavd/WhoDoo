from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-only-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "WhoDoo Finance"

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

    payment_provider: str = "auto"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    mock_payment_secret: str = "mock_key_secret"
    mock_webhook_secret: str = "mock_webhook_secret"

    @property
    def token_secret(self) -> str:
        return self.supabase_jwt_secret or self.jwt_secret

    @property
    def use_razorpay(self) -> bool:
        if self.payment_provider == "mock":
            return False
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    def check(self) -> None:
        if not self.demo_auth and self.token_secret == DEFAULT_JWT_SECRET:
            raise RuntimeError(
                "JWT_SECRET (or SUPABASE_JWT_SECRET) must be set when DEMO_AUTH is off"
            )
        if self.payment_provider == "razorpay" and not self.use_razorpay:
            raise RuntimeError("PAYMENT_PROVIDER=razorpay needs RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET")


@lru_cache
def get_settings() -> Settings:
    return Settings()
