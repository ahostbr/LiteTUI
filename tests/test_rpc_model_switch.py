"""T684 — a host model choice reuses LiteTUI's real /model switch path."""

from types import SimpleNamespace

from litetui import llm_backend, rpc


class RpcModelApp(SimpleNamespace):
    def __init__(self):
        super().__init__(
            available_models=["alpha", "beta"],
            model_id="alpha",
            model_rows={
                "alpha": llm_backend.ModelRow("alpha", None, "test", loaded=True),
                "beta": llm_backend.ModelRow("beta", None, "test", loaded=True),
            },
            backend=SimpleNamespace(name="codex", models={}),
            _rpc=True,
            _rpc_ready_sent=True,
            _model_thinking_levels=("old",),
            headers=0,
            fetches=0,
            applies=0,
            emitted=[],
            messages=[],
        )

    def update_header(self):
        self.headers += 1

    def fetch_context_window(self):
        self.fetches += 1

    def apply_context_length(self):
        self.applies += 1

    def system_message(self, message):
        self.messages.append(message)

    def _probe_thinking(self):
        raise AssertionError("Codex must not run the LM Studio probe")

    def _rpc_model_state(self):
        return {
            "model": self.model_id,
            "backend": self.backend.name,
            "models": [
                {
                    "slug": slug,
                    "name": slug,
                    "current": slug == self.model_id,
                    "loaded": self.model_rows[slug].loaded,
                }
                for slug in self.available_models
            ],
        }

    def _rpc_emit_model_state(self):
        self.emitted.append({"type": "model_state", **self._rpc_model_state()})


def test_set_model_drives_the_existing_switch_effects_and_emits_fresh_state(monkeypatch):
    app = RpcModelApp()
    replies = []
    monkeypatch.setattr(rpc, "_respond", lambda *args, **kwargs: replies.append((args, kwargs)))

    rpc._dispatch(app, {"type": "set_model", "id": "switch-1", "slug": "beta"})

    assert app.model_id == "beta"
    assert (app.headers, app.fetches, app.applies) == (1, 1, 1)
    assert app.messages == ["Switched to: beta"]
    assert app.emitted[-1]["models"][1]["current"] is True
    assert replies[-1][0] == ("switch-1",)
    assert replies[-1][1]["ok"] is True
    assert replies[-1][1]["result"]["model"] == "beta"


def test_set_model_refuses_a_slug_the_backend_did_not_offer(monkeypatch):
    app = RpcModelApp()
    replies = []
    monkeypatch.setattr(rpc, "_respond", lambda *args, **kwargs: replies.append((args, kwargs)))

    rpc._dispatch(app, {"type": "set_model", "id": "switch-2", "slug": "missing"})

    assert app.model_id == "alpha"
    assert (app.headers, app.fetches, app.applies, app.emitted) == (0, 0, 0, [])
    assert replies[-1] == (
        ("switch-2",),
        {"ok": False, "error": "model not available: missing"},
    )
