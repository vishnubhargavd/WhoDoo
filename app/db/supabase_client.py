from functools import lru_cache

from app.core.config import get_settings


@lru_cache
def get_supabase():
    """Supabase client for Storage, or None when Supabase is not configured (local development)."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        return None
    from supabase import create_client

    return create_client(settings.supabase_url, settings.supabase_service_key)
