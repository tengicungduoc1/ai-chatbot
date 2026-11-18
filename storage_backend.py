"""Select an explicit calendar backend without silent fallback."""
import os
from calendar_store import CalendarStore

def create_store(config):
    backend = config.get("db_backend", os.environ.get("DB_BACKEND", "sqlite")).lower()
    if backend == "sqlite":
        return CalendarStore(config["database"])
    if backend != "supabase":
        raise ValueError("Unknown database backend")
    url = config.get("supabase_db_url") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise ValueError("SUPABASE_DB_URL is required")
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise ImportError(
            'Supabase backend requires optional dependency; install with '
            'pip install "psycopg[binary]>=3.2,<4"'
        ) from exc
    from supabase_calendar_store import SupabaseCalendarStore
    return SupabaseCalendarStore(url, driver=psycopg)


def create_auth_store(cfg):
    backend = cfg.get('db_backend', os.environ.get('DB_BACKEND', 'sqlite')).lower()
    if backend == 'sqlite':
        from user_store import UserStore
        return UserStore(cfg['database'])
    if backend != 'supabase':
        raise ValueError('Unknown database backend')
    url = cfg.get('supabase_db_url') or os.environ.get('SUPABASE_DB_URL')
    if not url:
        raise ValueError('SUPABASE_DB_URL is required')
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise ImportError('Supabase backend requires optional dependency; install with pip install "psycopg[binary]>=3.2,<4"') from exc
    from pg_user_store import SupabaseUserStore
    return SupabaseUserStore(url, driver=psycopg)
