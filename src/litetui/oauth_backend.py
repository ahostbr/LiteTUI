"""Cloud control plane. Model metadata comes from the official CLI cache."""

import json

from litetui.llm_backend import BackendError, ModelRow
from litetui.model_transport import credential_path, read_credentials


class OAuthBackend:
    remote = True
    attached = True

    def __init__(self, settings):
        self.name = settings.backend
        self.settings = settings
        self.models = {}

    def base_url(self):
        # Identity only; remote inference never uses the OpenAI client.
        return "https://chatgpt.com/backend-api/codex"

    def host(self):
        return self.base_url()

    async def ensure_running(self):
        read_credentials(self.name)
        return "ok"

    def shutdown(self):
        pass

    async def list_models(self):
        try:
            obj = json.loads(
                (credential_path(self.name).parent / "models_cache.json").read_text(
                    encoding="utf-8"
                )
            )
            self.models = {
                m["slug"]: m
                for m in obj["models"]
                if m.get("visibility", "list") == "list" and m.get("context_window")
            }
        except (OSError, ValueError, KeyError, TypeError):
            self.models = {}
        if not self.models:
            raise BackendError(
                "Codex model metadata is unavailable. Start `codex` once to refresh its model list, then /reconnect."
            )
        return [
            ModelRow(
                key=k,
                path=None,
                source="Codex OAuth",
                loaded=True,
                modalities=tuple(v.get("input_modalities") or ["text"]),
            )
            for k, v in self.models.items()
        ]

    async def model_info(self, key):
        model = self.models.get(key)
        if not model:
            return None
        window = (
            int(model["context_window"])
            * int(model.get("effective_context_window_percent", 95))
            // 100
        )
        return (
            window,
            "vlm" if "image" in model.get("input_modalities", []) else "llm",
            True,
        )

    async def ensure_chat_ready(self, key):
        read_credentials(self.name)
        if key not in self.models:
            raise BackendError("Choose an available Codex model with /model.")

    def request_overrides(self, key):
        value = (
            (self.settings.model_infer_overrides or {})
            .get(key, {})
            .get("reasoning_effort")
        )
        return {"reasoning_effort": value} if value else {}

    def reasoning_levels(self, key):
        return [
            r["effort"]
            for r in self.models.get(key, {}).get("supported_reasoning_levels", [])
        ]

    async def load(self, key, **kwargs):
        raise BackendError(
            "Codex runs remotely. Select its model with /model; loading is not needed."
        )

    async def unload(self, key):
        raise BackendError(
            "Codex runs remotely and does not occupy local model memory."
        )

    def seat_snapshot(self, key):
        return None
