"""Local SQLite persistence for users, preferences, and device-token hashes."""
import hashlib
import hmac
import json
import secrets
import sqlite3
from uuid import uuid4

from preferences import DEFAULTS, Preferences


_ITERATIONS = 310_000


def _text(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Invalid {name}")
    return value


def hash_password(password):
    _text(password, "password", 1024)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    _text(password, "password", 1024)
    try:
        scheme, iterations, salt, expected = encoded.split("$")
        iterations = int(iterations)
        salt = bytes.fromhex(salt)
        expected = bytes.fromhex(expected)
        if scheme != "pbkdf2_sha256" or not 1 <= iterations <= 2_000_000 or len(salt) < 16 or len(expected) != 32:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Invalid password hash") from exc
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


class UserStore:
    _preference_keys = {"assistant_name", "style_prompt", "model", "voice"}

    def __init__(self, path):
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    email TEXT COLLATE NOCASE UNIQUE
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    preferences TEXT NOT NULL CHECK(json_valid(preferences))
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS user_devices (
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT 'Device',
                    PRIMARY KEY(user_id, device_id)
                )
            """)
            self._add_column("users", "email", "TEXT COLLATE NOCASE")
            self._add_column("user_devices", "name", "TEXT NOT NULL DEFAULT 'Device'")
            self._db.execute("DROP TRIGGER IF EXISTS user_devices_unique_id_insert")
            self._db.execute("DROP TRIGGER IF EXISTS user_devices_unique_id_update")
            self._repair_duplicate_device_ids()
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique "
                "ON users(email COLLATE NOCASE) WHERE email IS NOT NULL"
            )
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS user_devices_device_id ON user_devices(device_id)"
            )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            self._db.close()
            raise

    def _repair_duplicate_device_ids(self):
        rows = self._db.execute(
            "SELECT user_id, device_id FROM user_devices ORDER BY device_id, user_id"
        ).fetchall()
        seen = set()
        used = {row["device_id"] for row in rows}
        counts = {}
        for row in rows:
            device_id = row["device_id"]
            if device_id not in seen:
                seen.add(device_id)
                continue
            counts[device_id] = counts.get(device_id, 1) + 1
            ordinal = counts[device_id]
            suffix = f"__legacy_{row['user_id']}_{ordinal}"
            candidate = f"{device_id}{suffix}"
            collision = 1
            while candidate in used:
                collision += 1
                candidate = f"{device_id}{suffix}_{collision}"
            self._db.execute(
                "UPDATE user_devices SET device_id = ? WHERE user_id = ? AND device_id = ?",
                (candidate, row["user_id"], device_id),
            )
            used.add(candidate)
            seen.add(device_id)


    def _add_column(self, table, column, definition):
        columns = {row["name"] for row in self._db.execute(f'PRAGMA table_info("{table}")')}
        if column not in columns:
            self._db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')

    def close(self):
        self._db.close()

    def create_user(self, username, password, email=None):
        username = _text(username, "username", 80).strip()
        password_hash = hash_password(password)
        if email is not None:
            email = _text(email, "email", 254).strip()
        user = {"id": str(uuid4()), "username": username, "email": email}
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO users (id, username, password_hash, email) VALUES (?, ?, ?, ?)",
                    (user["id"], username, password_hash, email),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Username or email already exists") from exc
        return user

    def authenticate_user(self, username, password):
        _text(username, "username", 80)
        _text(password, "password", 1024)
        row = self._db.execute(
            "SELECT id, username, password_hash, email FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
        if row is None:
            return None
        try:
            valid = verify_password(password, row["password_hash"])
        except ValueError:
            return None
        if not valid:
            return None
        return {"id": row["id"], "username": row["username"], "email": row["email"]}

    def save_user_preferences(self, user_id, preferences):
        _text(user_id, "user ID")
        if not isinstance(preferences, dict) or set(preferences) != self._preference_keys:
            raise ValueError("Invalid preferences")
        preferences = Preferences(":memory:").validate(preferences)
        encoded = json.dumps(preferences, ensure_ascii=False, allow_nan=False)
        with self._db:
            if self._db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                raise ValueError("Unknown user")
            self._db.execute(
                "INSERT INTO user_preferences (user_id, preferences) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET preferences = excluded.preferences",
                (user_id, encoded),
            )
        return preferences

    def get_user_preferences(self, user_id):
        _text(user_id, "user ID")
        if self._db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise ValueError("Unknown user")
        row = self._db.execute(
            "SELECT preferences FROM user_preferences WHERE user_id = ?", (user_id,),
        ).fetchone()
        if row is None:
            return DEFAULTS.copy()
        try:
            value = json.loads(row["preferences"])
            if not isinstance(value, dict) or set(value) != self._preference_keys:
                return DEFAULTS.copy()
            return Preferences(":memory:").validate(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return DEFAULTS.copy()

    def register_device(self, user_id, device_id, name="Device"):
        _text(user_id, "user ID")
        device_id = _text(device_id, "device ID")
        name = _text(name, "device name")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO user_devices (user_id, device_id, token_hash, name) VALUES (?, ?, ?, ?)",
                    (user_id, device_id, token_hash, name),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Unknown user or duplicate device") from exc
        return {"device_id": device_id, "user_id": user_id, "name": name, "token": token}

    def resolve_device_token(self, token):
        _text(token, "device token", 256)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self._db.execute(
            "SELECT device_id, user_id, name FROM user_devices WHERE token_hash = ?", (token_hash,),
        ).fetchone()
        return dict(row) if row else None

    def list_user_ids(self):
        return [row[0] for row in self._db.execute('SELECT id FROM users')]

    def device_id_exists(self, device_id):
        _text(device_id, 'device ID')
        return self._db.execute('SELECT 1 FROM user_devices WHERE device_id=?', (device_id,)).fetchone() is not None

    def list_devices(self, user_id):
        _text(user_id, "user ID")
        return [dict(row) for row in self._db.execute(
            "SELECT device_id, user_id, name FROM user_devices WHERE user_id = ? ORDER BY device_id",
            (user_id,),
        )]

    def revoke_device(self, user_id, device_id):
        _text(user_id, "user ID")
        _text(device_id, "device ID")
        with self._db:
            cursor = self._db.execute(
                "DELETE FROM user_devices WHERE user_id = ? AND device_id = ?",
                (user_id, device_id),
            )
        return cursor.rowcount == 1
