"""An explicitly selected OpenAI-compatible server, with no endpoint fallback."""
from __future__ import annotations

import asyncio
import os

import httpx

from litetui.llm_backend import BackendError, ModelRow, _merged_overrides


def api_base(value: str) -> str:
    from urllib.parse import urlsplit
    value = value.strip().rstrip('/')
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Server URL must be http(s), without embedded credentials')
    if parts.query or parts.fragment:
        raise ValueError('Server URL must not contain a query or fragment')
    return value if value.endswith('/v1') else value + '/v1'


class CustomBackend:
    name = 'custom'
    label = 'Custom server'
    attached = True

    def __init__(self, settings):
        self.set_settings(settings)

    def set_settings(self, settings):
        self._settings = settings
        self._base = api_base(settings.custom_base_url) if settings.custom_base_url else 'http://127.0.0.1:0/v1'

    def base_url(self):
        return self._base

    def host(self):
        return self._base.removesuffix('/v1')

    def api_key(self):
        name = self._settings.custom_api_key_env
        if name and not os.environ.get(name):
            raise BackendError(f'Set the {name} environment variable for this custom server')
        return os.environ.get(name, 'litetui') if name else 'litetui'

    def _get(self, path):
        try:
            with httpx.Client(timeout=10, headers={'Authorization': f'Bearer {self.api_key()}'}) as client:
                response = client.get(self._base + path)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendError(f'Custom server request failed at {self._base}{path} ({type(exc).__name__})') from exc

    def _rows(self):
        body = self._get('/models')
        if not isinstance(body, dict) or not isinstance(body.get('data'), list):
            raise BackendError('Custom /models response must contain a data array')
        return [ModelRow(key=m['id'], path=None, source='server',
                        loaded='status' not in m or m.get('status', {}).get('value') == 'loaded')
                for m in body['data'] if isinstance(m, dict) and isinstance(m.get('id'), str)]

    async def ensure_running(self):
        if not self._settings.custom_base_url:
            raise BackendError('Set Custom server URL in /settings, or pass --base-url')
        await self.list_models()
        return 'ok'

    async def list_models(self):
        return await asyncio.to_thread(self._rows)

    def loaded_models(self):
        return [r.key for r in self._rows() if r.loaded]

    async def model_info(self, key):
        if key not in await asyncio.to_thread(self.loaded_models):
            return None
        # Generic OpenAI APIs do not standardize a live context query. This is
        # the user's declared client budget, never a claimed server resize.
        return self._settings.custom_context_length or None, 'llm', True

    async def ensure_chat_ready(self, key):
        if key not in await asyncio.to_thread(self.loaded_models):
            raise BackendError(f'Custom server does not advertise {key!r}; no model fallback was used')

    def request_overrides(self, key):
        return _merged_overrides(self._settings, key)

    def reasoning_levels(self, key):
        # Adapter vocabulary; actual support is defined by the server/template.
        return ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']

    async def load(self, key, *, ctx=None, notice=None):
        raise BackendError('Custom servers have no standard load API. Start the server with its model/context arguments.')

    async def unload(self, key):
        raise BackendError('Unload the model in the custom server that owns it.')

    async def apply_load_settings(self, key, cfg, *, notice=None):
        await self.load(key)

    def shutdown(self):
        pass

    def seat_snapshot(self, key):
        return None

    def seat_suspend(self, rec):
        return 'Custom server model suspension is not supported.'

    def seat_resume(self, rec):
        return 'Reconnect to the custom server.'

    def empty_state_hint(self):
        return 'Set the custom server URL in /settings, start that server, then /reconnect.'
