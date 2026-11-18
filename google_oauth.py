"""Google OAuth web flow. Caller owns one-time state, nonce and PKCE verifier storage."""

import asyncio
import base64
import hashlib
import ipaddress
import secrets
from functools import partial
from urllib.parse import urlencode, urlsplit

import aiohttp
from google.auth.transport.requests import Request
from google.oauth2 import id_token


class GoogleOAuth:
    def __init__(self, client_id, client_secret, redirect_uri, session=None):
        if not all(isinstance(value, str) and value.strip() for value in (client_id, client_secret, redirect_uri)):
            raise ValueError('Google OAuth credentials and redirect URI are required')
        if any(ord(char) <= 32 or ord(char) == 127 for char in redirect_uri) or '\\' in redirect_uri:
            raise ValueError('Invalid redirect URI')
        try:
            parsed = urlsplit(redirect_uri)
            host = parsed.hostname
            port = parsed.port
            if not host or parsed.username is not None or parsed.password is not None or parsed.fragment or '#' in redirect_uri:
                raise ValueError
            loopback = host == 'localhost'
            try:
                loopback = loopback or ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
            if parsed.scheme != 'https' and not (parsed.scheme == 'http' and loopback):
                raise ValueError
            if port == 0:
                raise ValueError
        except ValueError:
            raise ValueError('Invalid redirect URI') from None
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.session = session

    def authorization_url(self):
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        query = urlencode({'client_id': self.client_id, 'redirect_uri': self.redirect_uri,
                           'response_type': 'code', 'scope': 'openid email profile',
                           'state': state, 'nonce': nonce, 'code_challenge': challenge,
                           'code_challenge_method': 'S256'})
        return f'https://accounts.google.com/o/oauth2/v2/auth?{query}', state, nonce, verifier

    async def exchange(self, code, verifier, nonce):
        if not all(isinstance(value, str) and value for value in (code, verifier, nonce)):
            raise ValueError('Missing OAuth callback data')
        data = {'code': code, 'client_id': self.client_id, 'client_secret': self.client_secret,
                'redirect_uri': self.redirect_uri, 'grant_type': 'authorization_code', 'code_verifier': verifier}

        async def request_token(session):
            async with session.post('https://oauth2.googleapis.com/token', data=data, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    raise ValueError('Google token exchange failed')
                payload = await response.json()
                token = payload.get('id_token') if isinstance(payload, dict) else None
                if not isinstance(token, str) or not token:
                    raise ValueError('Google token response missing ID token')
                return token

        try:
            if self.session is None:
                async with aiohttp.ClientSession() as session:
                    token = await request_token(session)
            else:
                token = await request_token(self.session)
            claims = await asyncio.to_thread(id_token.verify_oauth2_token, token,
                                             partial(Request(), timeout=10), self.client_id)
            if (not isinstance(claims.get('nonce'), str) or not secrets.compare_digest(claims['nonce'], nonce)
                    or claims.get('email_verified') is not True
                    or not isinstance(claims.get('sub'), str) or not claims['sub']
                    or not isinstance(claims.get('email'), str) or not claims['email']):
                raise ValueError('Invalid Google identity claims')
            name = claims.get('name', '')
            if not isinstance(name, str):
                raise ValueError('Invalid Google identity claims')
            return {'sub': claims['sub'], 'email': claims['email'], 'name': name.strip() or 'Google user'}
        except Exception:
            raise ValueError('Google OAuth verification failed') from None
