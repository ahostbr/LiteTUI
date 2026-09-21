"""Codex control plane.

Two engines behind one backend, chosen by ``settings.codex_native_engine``:
the default is LiteTUI's own loop over the Responses API (credentials and
model metadata from the official CLI's files); with the flag on, the official
app-server owns login and agent execution and this object grows an
``app_server`` attribute — that attribute is the discriminator every other
module checks (``hasattr(backend, "app_server")``).
"""

import json

from litetui.llm_backend import BackendError, ModelRow
from litetui.model_transport import ProviderError, credential_path, read_credentials


class OAuthBackend:
    remote = True
    attached = True

    #: The name a HUMAN reads in the header (T861). An INSTANCE attribute below,
    #: not a class one, because this class serves every OAuth provider and its
    #: `name` comes from the setting — so the label has to follow the instance
    #: rather than be fixed at class definition.
    def __init__(self, settings):
        self.name = settings.backend
        self.label = {"codex": "Codex OAuth"}.get(self.name, self.name)
        self.settings = settings
        self.models = {}
        if getattr(settings, "codex_native_engine", False):
            from litetui.codex_app_server import AppServer

            self.app_server = AppServer()

    def set_settings(self, settings) -> None:
        """Keep this backend on the App's current Settings object after a save
        replaces it — the same stale-snapshot defect as `_VramGate.set_settings`,
        stored here on the public `settings` attribute instead.

        ⚠️ REBINDS THE SETTINGS ONLY. `app_server` is constructed once, in
        `__init__`, and is the discriminator every other module tests with
        `hasattr(backend, "app_server")` — building a second one here on a save
        would strand the running server that `ensure_running` already started.
        A backend whose ENGINE must change is replaced through the factory, not
        mutated (see `_switch_backend`).
        """
        self.settings = settings

    def base_url(self):
        # Identity only; remote inference never uses the OpenAI client.
        return "https://chatgpt.com/backend-api/codex"

    def host(self):
        return self.base_url()

    async def ensure_running(self):
        if not hasattr(self, "app_server"):
            read_credentials(self.name)
            return "ok"
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
        if not hasattr(self, "app_server"):
            # LiteTUI-loop path: the CLI's cache is the whole catalogue.
            self.models = {
                k: m for k, m in cached.items()
                if m.get("visibility", "list") == "list" and m.get("context_window")
            }
            if not self.models:
                raise BackendError(
                    "Codex model metadata is unavailable. Start `codex` once to refresh its model list, then /reconnect."
                )
            return self._rows()
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
        return self._rows()

    def _rows(self):
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
        if not hasattr(self, "app_server"):
            read_credentials(self.name)
        # /resume can construct this backend after the startup connect worker
        # has finished. An empty catalog means it has not been initialized,
        # not that the restored model is unavailable. Do this in the awaited
        # send gate so an immediate send cannot race a background reconnect.
        if not self.models:
            await self.ensure_running()
            await self.list_models()
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
