-- Optional PostgreSQL backend; fresh schema only, no SQLite data migration.
-- Google identities and device tokens are persisted here; sessions remain server-owned.
-- Run this migration as table owner so SECURITY DEFINER lookups can cross tenant RLS.
-- Install the optional driver: pip install "psycopg[binary]>=3.2,<4"
-- Set DB_BACKEND=supabase and SUPABASE_DB_URL in the server environment.
-- Grant app_calendar_backend membership only to the trusted server DB login.
-- Migration requires a role able to create roles, tables, policies and grants.
BEGIN;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_calendar_backend') THEN
        CREATE ROLE app_calendar_backend NOLOGIN;
    END IF;
END; $$;

CREATE TABLE IF NOT EXISTS public.users (
    id text PRIMARY KEY,
    username text NOT NULL,
    email text,
    password_hash text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.user_preferences (
    user_id text PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    assistant_name text NOT NULL,
    style_prompt text NOT NULL,
    model text NOT NULL,
    voice text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.devices (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    name text NOT NULL DEFAULT 'Device',
    device_token_hash text NOT NULL,
    last_seen timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.events (
    id text PRIMARY KEY,
    user_id text NOT NULL DEFAULT 'default',
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    date text NOT NULL,
    start text NOT NULL,
    "end" text NOT NULL CHECK ("end" > start),
    note text NOT NULL DEFAULT '' CHECK (length(note) <= 2000),
    remind_minutes integer NOT NULL DEFAULT 0 CHECK (remind_minutes BETWEEN 0 AND 1440),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1)
);
CREATE TABLE IF NOT EXISTS public.reminders (
    id text PRIMARY KEY,
    user_id text NOT NULL,
    event_id text NOT NULL,
    revision integer NOT NULL CHECK (revision >= 1),
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'played', 'failed', 'cancelled')),
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, revision)
);
-- Google subject, not display name or email, is the identity key.
ALTER TABLE public.users ALTER COLUMN password_hash DROP NOT NULL;
DROP INDEX IF EXISTS public.users_username_unique_ci;
DROP INDEX IF EXISTS public.users_email_unique_ci;
CREATE UNIQUE INDEX IF NOT EXISTS devices_token_hash_unique ON public.devices (device_token_hash);
CREATE INDEX IF NOT EXISTS events_user_date_start_end_idx ON public.events (user_id, date, start, "end");
CREATE INDEX IF NOT EXISTS reminders_user_updated_at_idx ON public.reminders (user_id, updated_at DESC, id DESC);

-- Deny browser roles all table access, especially password and device-token hashes.
REVOKE ALL ON public.users, public.user_preferences, public.devices,
    public.events, public.reminders FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA public TO app_calendar_backend;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.users, public.user_preferences,
    public.devices, public.events, public.reminders TO app_calendar_backend;
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reminders ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS users_owner_all ON public.users;
DROP POLICY IF EXISTS user_preferences_owner_all ON public.user_preferences;
DROP POLICY IF EXISTS devices_owner_all ON public.devices;
DROP POLICY IF EXISTS events_owner_all ON public.events;
DROP POLICY IF EXISTS reminders_owner_all ON public.reminders;
DROP POLICY IF EXISTS users_backend_owner ON public.users;
CREATE POLICY users_backend_owner ON public.users FOR ALL TO app_calendar_backend
    USING (id = current_setting('app.user_id', true))
    WITH CHECK (id = current_setting('app.user_id', true));
DROP POLICY IF EXISTS user_preferences_backend_owner ON public.user_preferences;
CREATE POLICY user_preferences_backend_owner ON public.user_preferences FOR ALL TO app_calendar_backend
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));
DROP POLICY IF EXISTS devices_backend_owner ON public.devices;
CREATE POLICY devices_backend_owner ON public.devices FOR ALL TO app_calendar_backend
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));
DROP POLICY IF EXISTS events_backend_owner ON public.events;
CREATE POLICY events_backend_owner ON public.events FOR ALL TO app_calendar_backend
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));
DROP POLICY IF EXISTS reminders_backend_owner ON public.reminders;
CREATE POLICY reminders_backend_owner ON public.reminders FOR ALL TO app_calendar_backend
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

-- These owner-executed functions expose only the minimum pre-tenant data to the
-- trusted server role. Table ownership must bypass RLS; never FORCE RLS here.
CREATE OR REPLACE FUNCTION public.resolve_device_token(hash text)
RETURNS TABLE(device_id text, user_id text, name text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT d.id, d.user_id, d.name FROM public.devices AS d
      WHERE d.device_token_hash = $1 $$;
CREATE OR REPLACE FUNCTION public.device_id_exists(device_id text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT EXISTS (SELECT 1 FROM public.devices AS d WHERE d.id = $1) $$;
CREATE OR REPLACE FUNCTION public.list_user_ids()
RETURNS TABLE(id text) LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT u.id FROM public.users AS u ORDER BY u.id $$;
REVOKE ALL ON FUNCTION public.resolve_device_token(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.device_id_exists(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.list_user_ids() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_device_token(text) TO app_calendar_backend;
GRANT EXECUTE ON FUNCTION public.device_id_exists(text) TO app_calendar_backend;
GRANT EXECUTE ON FUNCTION public.list_user_ids() TO app_calendar_backend;
COMMIT;
