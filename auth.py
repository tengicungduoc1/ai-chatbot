"""Local authentication using existing hashed user and device storage."""
import hashlib
import secrets
import time

from user_store import UserStore


class Auth:
    def __init__(self, path=None, *, store=None):
        self.store = store if store is not None else UserStore(path)
        self.sessions = {}

    def close(self):
        self.store.close()

    def create_session(self, user):
        token = secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (user, time.monotonic() + 86400)
        # ponytail: in-memory sessions expire on restart; use shared durable sessions for multi-worker deployment.
        for key, (_, expiry) in list(self.sessions.items()):
            if expiry <= time.monotonic():
                self.sessions.pop(key, None)
        return token

    def get_session(self, token):
        if not isinstance(token, str) or len(token) > 256:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        item = self.sessions.get(digest)
        if not item:
            return None
        if item[1] <= time.monotonic():
            self.sessions.pop(digest, None)
            return None
        return item[0]

    def revoke_session(self, token):
        if isinstance(token, str) and len(token) <= 256:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def user_ids(self):
        return ['default'] + self.store.list_user_ids()
