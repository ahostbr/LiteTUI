"""Codex control plane; the official app-server owns login and agent execution."""

import json

from litetui.llm_backend import BackendError, ModelRow
from litetui.model_transport import ProviderError, credential_path


class OAuthBackend:
    remote = True
    attached = True

    def __init__(self, settings):
        self.name = settings.backend
        self.settings = settings
        self.models = {}
        from litetui.codex_app_server import AppServer

        self.app_server = AppServer()

    def base_url(self):
        # Identity only; remote inference never uses the OpenAI client.
        return "https://chatgpt.com/backend-api/codex"

    def host(self):
        return self.base_url()

    async def ensure_running(self):
        await self.app_server.start()
        account = await self.app_server.request("account/read", {"refreshToken": False})
        if (account.get("account") or {}).get("type") != "chatgpt":
            raise BackendError(
                "Run `codex login` with your ChatGPT subscription, then /reconnect."
            )
        return "ok"

    def shutdown(self):
        server = getattr(self, "app_server", None)
        if server is not None:
            server.shutdown()

    async def list_models(self):
        try:
            obj = json.loads(
                (credential_path(self.name).parent / "models_cache.json").read_text(
                    encoding="utf-8"
                )
            )
            cached = {m["slug"]: m for m in obj["models"]}
        except (OSError, ValueError, KeyError, TypeError):
            cached = {}
        models, cursors = {}, set()
        cursor = None
        try:
            await self.app_server.start()
            while True:
                page = await self.app_server.request(
                    "model/list",
                    {"includeHidden": False, **({"cursor": cursor} if cursor else {})},
                )
                for model in page["data"]:
                    if model.get("hidden"):
                        continue
                    key = model["model"]
                    supplemental = cached.get(key, {})
                    models[key] = {
                        **{
                            field: supplemental[field]
                            for field in (
                                "context_window",
                                "effective_context_window_percent",
                            )
                            if field in supplemental
                        },
                        "slug": key,
                        "display_name": model.get("displayName", key),
                        "default_reasoning_level": model["defaultReasoningEffort"],
                        "supported_reasoning_levels": [
                            {
                                "effort": option["reasoningEffort"],
                                "description": option.get("description", ""),
                            }
                            for option in model["supportedReasoningEfforts"]
                        ],
                        "input_modalities": model.get("inputModalities") or ["text"],
                        "service_tiers": model.get("serviceTiers", []),
                        "default_service_tier": model.get("defaultServiceTier"),
                        "multi_agent_version": model.get("multiAgentVersion"),
                        "metadata_source": "codex-app-server",
                    }
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                if cursor in cursors:
                    raise ValueError("Repeated model catalog cursor")
                cursors.add(cursor)
        except (ProviderError, OSError, ValueError, KeyError, TypeError) as error:
            raise BackendError(
                "Codex app-server model metadata is unavailable. Reconnect to refresh it."
            ) from error
        if not models:
            raise BackendError(
                "Codex app-server returned no available models. Reconnect to refresh it."
            )
        self.models = models
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
        if not model or not model.get("context_window"):
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
