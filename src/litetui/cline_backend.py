"""ClinePass: Cline's OpenAI-compatible endpoint, driven by OUR turn engine.

@cline/sdk reaches ClinePass as provider "cline-pass": plain chat completions at
https://api.cline.bot/api/v1 with `Authorization: Bearer <key>`. So this is a
CustomBackend with a fixed endpoint, a fixed model list and Cline's own auth.
Conversation store, compaction and tools stay ours.

Auth follows github.com/cline/cline @ b51c27b (sdk/packages/core/src), so we can
share one login with the Cline CLI, extension and hub without the CLI installed:
- store: <CLINE_DATA_DIR or ~/.cline/data>/settings/providers.json, entry
  providers.cline.settings.auth ("cline-pass" stores under "cline");
  OAuth wins over CLINE_API_KEY (auth/provider-auth-registry.ts getApiKey).
- header: Bearer workos:<access> (auth/cline.ts formatAccessToken).
- refresh 5 min before expiry: POST /api/v1/auth/refresh {refreshToken, grantType}
  (auth/cline.ts refreshClineToken); a transient failure keeps a token with >30s
  left, a rejected refresh token means log in again (getValidClineCredentials).
- serialized across processes by an SQLite BEGIN EXCLUSIVE on
  <store>.oauth-<sha256("cline")>.lock, re-reading the store under it
  (runtime/orchestration/oauth-refresh-lock.ts, runtime-oauth-token-manager.ts).
- written back through a pid-unique temp file + rename
  (services/storage/provider-settings-manager.ts write).
Tokens are read at call time and never copied into our settings.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx

from litetui.custom_backend import CustomBackend
from litetui.llm_backend import BackendError, ModelRow

CLINE_API = 'https://api.cline.bot'
CLINE_KEY_ENV = 'CLINE_API_KEY'
_PREFIX = 'workos:'
_REFRESH_BUFFER_S = 300
_TRANSIENT_GRACE_S = 30
_RELOGIN = 'run `cline auth` again, then /reconnect'

#: id -> (context window, reasoning effort levels), from the @cline/llms
#: dist/models.js catalog. The live list (GET /api/v1/ai/cline/recommended-models,
#: "clinePass") matched these 14 ids on 2026-09-24. None = no usable window in the
#: catalog (it lists qwen3.7-* as 1 token).
CLINE_MODELS = {
    'cline-pass/glm-5.3-flash': (1310720, ('low', 'high', 'max')),
    'cline-pass/glm-5.3': (1310720, ('low', 'high', 'max')),
    'cline-pass/kimi-k3': (1048576, ('low', 'high', 'max')),
    'cline-pass/deepseek-v4-pro': (1048576, ('high', 'xhigh')),
    'cline-pass/deepseek-v4.1-flash': (1048576, ('low', 'high', 'max')),
    'cline-pass/minimax-m3': (1048576, ()),
    'cline-pass/mimo-v2.6-pro': (1048576, ()),
    'cline-pass/mimo-v2.6-flash': (1048576, ()),
    'cline-pass/mimo-v2.5-pro': (1050000, ()),
    'cline-pass/mimo-v2.5': (1050000, ()),
    'cline-pass/qwen3.8-max': (None, ()),
    'cline-pass/qwen3.7-max': (None, ()),
    'cline-pass/qwen3.7-plus': (None, ()),
    'cline-pass/muse-spark-1.3-contributor': (1048576, ('minimal', 'low', 'medium', 'high', 'xhigh', 'max')),
}


def store_path() -> Path:
    """@cline/shared storage: CLINE_PROVIDER_SETTINGS_PATH, else
    <CLINE_DATA_DIR or ~/.cline/data>/settings/providers.json."""
    explicit = os.environ.get('CLINE_PROVIDER_SETTINGS_PATH', '').strip()
    if explicit:
        return Path(explicit)
    data = os.environ.get('CLINE_DATA_DIR', '').strip()
    return (Path(data) if data else Path.home() / '.cline' / 'data') / 'settings' / 'providers.json'


def _read_store(path: Path):
    """(whole store, the cline auth dict) or (None, None) when there is no login."""
    try:
        store = json.loads(path.read_text(encoding='utf-8'))
        auth = store['providers']['cline']['settings']['auth']
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    if not isinstance(auth, dict) or not auth.get('accessToken') or not auth.get('refreshToken'):
        return None, None
    return store, auth


def _bearer(token: str) -> str:
    token = token.strip()
    return token if token.lower().startswith(_PREFIX) else _PREFIX + token


def _left_s(auth: dict) -> float:
    try:
        return float(auth['expiresAt']) / 1000 - time.time()
    except (KeyError, TypeError, ValueError):
        return 0.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


@contextmanager
def _refresh_lock(path: Path):
    """The SDK's cross-process lock, taken the same way so we interoperate."""
    lock = f"{path.resolve()}.oauth-{hashlib.sha256(b'cline').hexdigest()}.lock"
    db = sqlite3.connect(lock, timeout=0, isolation_level=None)
    deadline = time.time() + 60
    try:
        while True:
            try:
                db.execute('BEGIN EXCLUSIVE')
                break
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc) and 'busy' not in str(exc):
                    raise
                if time.time() >= deadline:
                    raise BackendError('Timed out waiting for another process to refresh the ClinePass login') from None
                time.sleep(0.025)
        yield
    finally:
        if db.in_transaction:
            db.execute('ROLLBACK')
        db.close()


def _write_store(path: Path, store: dict) -> None:
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(store, indent=2) + '\n', encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _refresh(path: Path, auth: dict) -> str:
    """Exchange the refresh token and write the new credentials back. Called
    under _refresh_lock with a store read under it."""
    try:
        response = httpx.post(f'{CLINE_API}/api/v1/auth/refresh', timeout=30,
                              json={'refreshToken': auth['refreshToken'], 'grantType': 'refresh_token'})
    except httpx.HTTPError as exc:
        return _keep_or_fail(auth, type(exc).__name__)
    if response.status_code in (400, 401, 403):
        raise BackendError(f'ClinePass login was rejected ({response.status_code}); {_RELOGIN}')
    try:
        response.raise_for_status()
        body = response.json()
        data = body['data']
        if not body.get('success') or not data.get('accessToken'):
            raise ValueError
        expires = datetime.fromisoformat(data['expiresAt'])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return _keep_or_fail(auth, f'HTTP {response.status_code}')
    # A sign-out or new sign-in does not take the lock; never overwrite it.
    store, latest = _read_store(path)
    if not latest:
        raise BackendError(f'ClinePass login disappeared; {_RELOGIN}')
    if latest.get('refreshToken') != auth['refreshToken']:
        return _bearer(latest['accessToken'])
    user = data.get('userInfo') or {}
    auth = latest  # write into what is on disk NOW, not the pre-HTTP copy
    metadata = {**(auth.get('metadata') or {}), 'tokenType': data.get('tokenType'), 'userInfo': user}
    metadata.pop('startedAt', None)
    auth.update(accessToken=_bearer(data['accessToken']),
                refreshToken=data.get('refreshToken') or auth['refreshToken'],
                accountId=user.get('clineUserId') or auth.get('accountId'),
                expiresAt=int(expires.timestamp() * 1000), metadata=metadata)
    entry = store['providers']['cline']
    entry.update(updatedAt=_now_iso(), tokenSource='oauth')
    _write_store(path, store)
    return auth['accessToken']


def _keep_or_fail(auth: dict, why: str) -> str:
    if _left_s(auth) > _TRANSIENT_GRACE_S:
        return _bearer(auth['accessToken'])
    raise BackendError(f'ClinePass token refresh failed ({why}); try again, or {_RELOGIN}')


def auth_status() -> str:
    """The /backend readiness mark. Reads files only, no network."""
    if _read_store(store_path())[1]:
        return 'OAuth signed in'
    if os.environ.get(CLINE_KEY_ENV, '').strip():
        return 'key set'
    return 'run: cline auth'


class ClineBackend(CustomBackend):
    name = 'cline'
    label = 'ClinePass'

    def set_settings(self, settings):
        self._settings = settings
        self._base = f'{CLINE_API}/api/v1'

    def api_key(self):
        path = store_path()
        _, auth = _read_store(path)
        if auth and _left_s(auth) > _REFRESH_BUFFER_S:
            return _bearer(auth['accessToken'])
        if auth:
            with _refresh_lock(path):
                _, auth = _read_store(path)  # another holder may have rotated it
                if not auth:
                    raise BackendError(f'ClinePass login disappeared; {_RELOGIN}')
                if _left_s(auth) > _REFRESH_BUFFER_S:
                    return _bearer(auth['accessToken'])
                return _refresh(path, auth)
        key = os.environ.get(CLINE_KEY_ENV, '').strip()
        if key:
            return key
        raise BackendError(f'ClinePass needs a login: run `cline auth`, or set {CLINE_KEY_ENV}; then /reconnect')

    async def api_key_provider(self):
        """Handed to AsyncOpenAI, which awaits it before EVERY request, so a
        token that expires mid-conversation is refreshed, not sent stale."""
        import asyncio
        return await asyncio.to_thread(self.api_key)

    def _rows(self):
        return [ModelRow(key=k, path=None, source='server', loaded=True) for k in CLINE_MODELS]

    async def ensure_running(self):
        import asyncio
        await asyncio.to_thread(self.api_key)
        return 'ok'

    async def model_info(self, key):
        if key not in CLINE_MODELS:
            return None
        return CLINE_MODELS[key][0], 'llm', True

    def reasoning_levels(self, key):
        return ['none', *CLINE_MODELS.get(key, (None, ()))[1]]

    async def load(self, key, *, ctx=None, notice=None):
        raise BackendError('ClinePass models are remote; pick one with /model.')

    async def unload(self, key):
        raise BackendError('ClinePass models are remote; nothing to unload.')

    def empty_state_hint(self):
        return f'Run `cline auth` (or set {CLINE_KEY_ENV}), then /reconnect.'
