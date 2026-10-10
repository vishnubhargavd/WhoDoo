from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


def sqlalchemy_url(url: str) -> str:
    """Accept plain postgres:// URLs (as Supabase prints them) and point them at asyncpg."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def asyncpg_dsn(url: str) -> str:
    return sqlalchemy_url(url).replace("postgresql+asyncpg://", "postgresql://", 1)


def _create_engine():
    settings = get_settings()
    kwargs: dict = {"pool_pre_ping": True}
    if settings.db_null_pool:
        kwargs = {"poolclass": NullPool}
    if settings.db_disable_statement_cache:
        kwargs["connect_args"] = {"statement_cache_size": 0}
    return create_async_engine(sqlalchemy_url(settings.database_url), **kwargs)


engine = _create_engine()
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
