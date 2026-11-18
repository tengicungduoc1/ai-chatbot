"""Explicit, lazy provider selection; external error bodies never reach callers."""
from urllib.parse import urlsplit


class ProviderError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__(f"AI provider error: {category}")


class GeminiProvider:
    """Gemini stays on the existing agent and speech SDK paths."""
    def __init__(self, api_key):
        self.api_key = api_key


class OpenAICompatibleProvider:
    def __init__(self, api_key, model, base_url, session=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1'))
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip('/') not in ('', '/v1')):
            raise ValueError('Invalid OpenAI-compatible base URL')
        if not isinstance(api_key, str) or not api_key or not isinstance(model, str) or not model:
            raise ValueError('OpenAI-compatible key and model are required')
        self._api_key, self.model = api_key, model
        self.base_url = base_url.rstrip('/')
        self._session = session
        self._owns_session = session is None

    async def generate(self, messages, *, tools=None):
        if self._session is None:
            from aiohttp import ClientSession, ClientTimeout
            self._session = ClientSession(timeout=ClientTimeout(total=30))
        body = {'model': self.model, 'messages': messages}
        if tools:
            body['tools'] = tools
        try:
            async with self._session.post(
                self.base_url + ('/chat/completions' if self.base_url.endswith('/v1') else '/v1/chat/completions'),
                json=body, headers={'Authorization': 'Bearer ' + self._api_key},
            ) as response:
                if response.status >= 400:
                    category = ('quota' if response.status == 429 else 'auth' if response.status in (401, 403)
                                else 'unavailable' if response.status >= 500 else 'invalid_request')
                    raise ProviderError(category)
                data = await response.json()
                if not isinstance(data, dict) or not isinstance(data.get('choices'), list):
                    raise ProviderError('invalid_response')
                return data
        except ProviderError:
            raise
        except Exception:
            raise ProviderError('unavailable') from None

    async def close(self):
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None


def create_provider(config):
    kind = config.get('ai_provider', 'gemini')
    if kind == 'gemini':
        return GeminiProvider(config.get('api_key', ''))
    if kind == 'openai_compatible':
        return OpenAICompatibleProvider(config.get('api_key', ''), config.get('model', ''),
                                        config.get('ai_base_url', ''))
    raise ValueError('Unknown AI provider')
