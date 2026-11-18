"""Trusted backend-only PostgreSQL persistence for Google accounts and devices."""
from contextlib import contextmanager
from collections.abc import Mapping
import hashlib
import re
import secrets
import threading

from preferences import DEFAULTS, Preferences
from user_store import _text


def _pg_text(value, name, maximum=200):
    if isinstance(value, str) and "\x00" in value:
        raise ValueError(f"Invalid {name}")
    return _text(value, name, maximum)


class SupabaseUserStore:
    blocks_event_loop = True
    _preference_keys = {'assistant_name', 'style_prompt', 'model', 'voice'}

    def __init__(self, url, driver):
        self._driver = driver
        self._db = driver.connect(url, autocommit=True, row_factory=driver.rows.dict_row)
        self._mutex = threading.RLock()
        try:
            cursor = self._db.cursor()
            try:
                cursor.execute("SELECT pg_has_role(current_user, %s, 'member') AS is_member", ('app_calendar_backend',))
                row = cursor.fetchone()
                member = row.get('is_member') if isinstance(row, Mapping) else row[0] if row else None
                if member is not True:
                    raise RuntimeError('Database login is not a member of app_calendar_backend')
            finally:
                cursor.close()
        except BaseException:
            self._db.close()
            raise

    def close(self):
        self._db.close()

    @contextmanager
    def _transaction(self, user_id=None):
        # ponytail: one serialized connection; use a pool if throughput requires it.
        with self._mutex:
            cursor = self._db.cursor()
            try:
                cursor.execute('BEGIN')
                cursor.execute('SET LOCAL ROLE app_calendar_backend')
                if user_id is not None:
                    cursor.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
                yield cursor
                cursor.execute('COMMIT')
            except BaseException:
                cursor.execute('ROLLBACK')
                raise
            finally:
                cursor.close()

    def upsert_google_user(self, sub, email, name):
        if not isinstance(sub, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', sub, re.ASCII):
            raise ValueError('Invalid Google sub')
        email = _pg_text(email, 'email', 254).strip()
        if isinstance(name, str) and not name.strip():
            name = 'Google user'
        name = _pg_text(name, 'username', 80).strip()
        user_id = 'google:' + sub
        with self._transaction(user_id) as cursor:
            cursor.execute('INSERT INTO public.users (id, username, email) VALUES (%s, %s, %s) '
                           'ON CONFLICT (id) DO UPDATE SET username = EXCLUDED.username, email = EXCLUDED.email '
                           'RETURNING id, username, email', (user_id, name, email))
            return dict(cursor.fetchone())

    def list_user_ids(self):
        with self._transaction() as cursor:
            cursor.execute('SELECT id FROM public.list_user_ids()')
            return [row['id'] for row in cursor.fetchall()]

    def device_id_exists(self, device_id):
        _pg_text(device_id, 'device ID')
        with self._transaction() as cursor:
            cursor.execute('SELECT public.device_id_exists(%s) AS exists', (device_id,))
            return bool(cursor.fetchone()['exists'])

    def _require_user(self, cursor, user_id):
        cursor.execute('SELECT 1 FROM public.users WHERE id = %s', (user_id,))
        if cursor.fetchone() is None:
            raise ValueError('Unknown user')

    def get_user_preferences(self, user_id):
        _pg_text(user_id, 'user ID')
        with self._transaction(user_id) as cursor:
            self._require_user(cursor, user_id)
            cursor.execute('SELECT assistant_name, style_prompt, model, voice FROM public.user_preferences WHERE user_id = %s', (user_id,))
            row = cursor.fetchone()
        if not row:
            return DEFAULTS.copy()
        try:
            return Preferences(':memory:').validate(dict(row))
        except (TypeError, ValueError):
            return DEFAULTS.copy()

    def save_user_preferences(self, user_id, preferences):
        _pg_text(user_id, 'user ID')
        if not isinstance(preferences, dict) or set(preferences) != self._preference_keys:
            raise ValueError('Invalid preferences')
        preferences = Preferences(':memory:').validate(preferences)
        with self._transaction(user_id) as cursor:
            self._require_user(cursor, user_id)
            cursor.execute('INSERT INTO public.user_preferences (user_id, assistant_name, style_prompt, model, voice) '
                           'VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET '
                           'assistant_name = EXCLUDED.assistant_name, style_prompt = EXCLUDED.style_prompt, '
                           'model = EXCLUDED.model, voice = EXCLUDED.voice, updated_at = now()',
                           (user_id, *(preferences[k] for k in ('assistant_name', 'style_prompt', 'model', 'voice'))))
        return preferences

    def register_device(self, user_id, device_id, name='Device'):
        _pg_text(user_id, 'user ID')
        _pg_text(device_id, 'device ID')
        _pg_text(name, 'device name')
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        try:
            with self._transaction(user_id) as cursor:
                cursor.execute('INSERT INTO public.devices (id, user_id, device_token_hash, name) VALUES (%s, %s, %s, %s)',
                               (device_id, user_id, token_hash, name))
        except self._driver.IntegrityError as exc:
            raise ValueError('Unknown user or duplicate device') from exc
        return {'device_id': device_id, 'user_id': user_id, 'name': name, 'token': token}

    def resolve_device_token(self, token):
        _text(token, 'device token', 256)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._transaction() as cursor:
            cursor.execute('SELECT device_id, user_id, name FROM public.resolve_device_token(%s)', (token_hash,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_devices(self, user_id):
        _pg_text(user_id, 'user ID')
        with self._transaction(user_id) as cursor:
            cursor.execute('SELECT id AS device_id, user_id, name FROM public.devices WHERE user_id = %s ORDER BY id', (user_id,))
            return [dict(row) for row in cursor.fetchall()]

    def revoke_device(self, user_id, device_id):
        _pg_text(user_id, 'user ID')
        _pg_text(device_id, 'device ID')
        with self._transaction(user_id) as cursor:
            cursor.execute('DELETE FROM public.devices WHERE user_id = %s AND id = %s', (user_id, device_id))
            return cursor.rowcount == 1
