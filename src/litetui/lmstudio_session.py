"""Owned SDK connections, with no process-default endpoint configuration."""
from contextlib import contextmanager
from threading import RLock

# The installed SDK exposes timeout only as a module-global setting. Serialize
# our synchronous control operations and restore it even on failure. This does
# not assert control over unrelated embedders calling the SDK directly.
_SDK_CONTROL_LOCK = RLock()


class LMStudioSession:
    def __init__(self, host, *, timeout):
        import lmstudio
        self.sdk = lmstudio
        self.host = host
        self.timeout = timeout
        self._client = None
        self._closed = False

    @contextmanager
    def _operation(self):
        with _SDK_CONTROL_LOCK:
            if self._closed:
                raise RuntimeError('LM Studio session is closed')
            previous = self.sdk.get_sync_api_timeout()
            try:
                self.sdk.set_sync_api_timeout(self.timeout)
                if self._client is None:
                    self._client = self.sdk.Client(self.host)
                yield self._client
            finally:
                self.sdk.set_sync_api_timeout(previous)

    def load(self, key, *, config=None):
        with self._operation() as client:
            client.llm.model(key, config=config)

    def unload(self, key):
        with self._operation() as client:
            for model in client.list_loaded_models():
                if model.identifier == key:
                    model.unload()
                    break

    def close(self):
        with _SDK_CONTROL_LOCK:
            if self._closed:
                return
            if self._client is not None:
                self._client.close()
            self._client = None
            self._closed = True
