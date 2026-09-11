"""T531 — subagent tool: schema, background gate, label, runner."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from litetui import tasks as tasks_mod
from litetui import tool_schemas


class _FakeResp:
    def __init__(self, body: dict):
        self._data = json.dumps(body).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestSchema:
    def test_loads(self):
        spec = tool_schemas.load("subagent")
        fn = spec["function"]
        assert fn["name"] == "subagent"
        props = fn["parameters"]["properties"]
        assert "prompt" in props
        assert "system" in props
        assert "max_tokens" in props
        assert "model" in props
        assert "think" in props
        assert "background" in props

    def test_prompt_required(self):
        spec = tool_schemas.load("subagent")
        assert "prompt" in spec["function"]["parameters"]["required"]

    def test_backgroundable(self):
        tasks_mod.backgroundable.cache_clear()
        assert tasks_mod.backgroundable("subagent") is True


class TestLabel:
    def test_uses_prompt(self):
        label = tasks_mod.label_of("subagent", {"prompt": "Summarise the README"})
        assert "Summarise" in label

    def test_truncates(self):
        label = tasks_mod.label_of("subagent", {"prompt": "x" * 100})
        assert len(label) <= 61
        assert label.endswith("…")


class TestRunner:
    def _make_app(self, host="http://localhost:1234", model="test-model"):
        return SimpleNamespace(
            model_id=model,
            model_rows={key: SimpleNamespace(loaded=True) for key in (model, "small-child")},
            settings=SimpleNamespace(
                lm_host=host,
                compact_max_tokens=12288,
            ),
        )

    def test_empty_prompt_rejected(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        run = _make_runner(app)
        result = run({"prompt": ""})
        assert "[error]" in result

    def test_max_tokens_capped(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        app.settings.compact_max_tokens = 5000

        captured = {}

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)
            captured["max_tokens"] = body["max_tokens"]
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 10}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "hello", "max_tokens": 99999})

        assert captured["max_tokens"] == 5000

    def test_success_result_shape(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()

        def fake_urlopen(req, timeout=None):
            return _FakeResp({"choices": [{"message": {"content": "Summary: it works"}}], "usage": {"completion_tokens": 42}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            result = run({"prompt": "summarise"})

        assert "Summary: it works" in result
        assert "42 tokens" in result
        assert "test-model" in result

    def test_system_message_sent(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["messages"] = json.loads(req.data)["messages"]
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "hello", "system": "You are a pirate"})

        assert captured["messages"][0] == {"role": "system", "content": "You are a pirate"}
        assert captured["messages"][1] == {"role": "user", "content": "hello"}

    def test_no_system_when_omitted(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["messages"] = json.loads(req.data)["messages"]
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "hello"})

        assert len(captured["messages"]) == 1
        assert captured["messages"][0]["role"] == "user"

    def test_tokens_logged_to_task_ledger(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        task = tasks_mod.Task(
            id="t-test", tool="subagent", label="test",
            convo_id="", started=0.0,
        )
        assert task.tokens is None

        def fake_urlopen(req, timeout=None):
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 77}})

        run = _make_runner(app)
        tok = tasks_mod.CURRENT.set(task)
        try:
            with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
                run({"prompt": "hello"})
        finally:
            tasks_mod.CURRENT.reset(tok)

        assert task.tokens == 77

    def test_network_error_returns_error(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        run = _make_runner(app)

        def boom(*a, **kw):
            raise ConnectionRefusedError("nope")

        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", boom):
            result = run({"prompt": "hello"})

        assert "[error]" in result
        assert "ConnectionRefusedError" in result

    def test_reasoning_only_returns_warning_with_tail(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()

        def fake_urlopen(req, timeout=None):
            return _FakeResp({
                "choices": [{"message": {"content": "", "reasoning_content": "I think therefore I am"}}],
                "usage": {"completion_tokens": 400},
            })

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            result = run({"prompt": "hello"})

        assert "WARNING" in result
        assert "all tokens went to reasoning" in result
        assert "I think therefore I am" in result

    def test_files_appended_to_prompt(self, tmp_path):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        f = tmp_path / "readme.md"
        f.write_text("# Hello World", encoding="utf-8")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "summarise this", "files": [str(f)]})

        content = captured["body"]["messages"][-1]["content"]
        assert "summarise this" in content
        assert "# Hello World" in content
        assert "readme.md" in content

    def test_missing_file_named_error(self, tmp_path):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "summarise", "files": [str(tmp_path / "nope.txt")]})

        content = captured["body"]["messages"][-1]["content"]
        assert "[error reading file:" in content

    def test_large_file_truncated(self, tmp_path):
        from litetui.plugins.subagent_plugin import FILE_CAP, _make_runner
        app = self._make_app()
        f = tmp_path / "big.txt"
        f.write_text("x" * (FILE_CAP + 1000), encoding="utf-8")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "summarise", "files": [str(f)]})

        content = captured["body"]["messages"][-1]["content"]
        assert "truncated" in content

    def test_think_false_sends_reasoning_off_toplevel(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "hello"})

        assert captured["body"]["reasoning_effort"] == "none"
        assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_think_true_omits_reasoning_keys(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "hello", "think": True})

        assert "reasoning_effort" not in captured["body"]
        assert "chat_template_kwargs" not in captured["body"]

    def test_subagent_model_setting_is_the_default_child(self):
        # T538: the child goes to settings.subagent_model when the call names none.
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app(model="big-parent")
        app.settings.subagent_model = "small-child"
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "hello"})

        assert captured["body"]["model"] == "small-child"

    def test_explicit_model_beats_the_setting(self):
        from litetui.plugins.subagent_plugin import _make_runner
        app = self._make_app(model="big-parent")
        app.settings.subagent_model = "small-child"
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}})

        run = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.urllib.request.urlopen", fake_urlopen):
            run({"prompt": "hello", "model": "named-one"})

        assert captured["body"]["model"] == "named-one"

class TestBackendModelSelection:
    def _run(self, remote, persisted, current, available, explicit=None):
        from litetui.plugins.subagent_plugin import _make_runner
        app = SimpleNamespace(
            backend=SimpleNamespace(name="codex" if remote else "lmstudio", remote=remote,
                                    models={key: {} for key in available}),
            model_id=current,
            model_rows={key: SimpleNamespace(loaded=loaded) for key, loaded in available.items()},
            settings=SimpleNamespace(subagent_model=persisted, compact_max_tokens=100),
        )
        captured = []
        def complete(app, payload, **kwargs):
            captured.append(payload["model"])
            return {"choices": [{"message": {"content": "ok"}}]}
        args = {"prompt": "hello"}
        if explicit:
            args["model"] = explicit
        with patch("litetui.plugins.subagent_plugin.model_transport.complete_sidecall", complete):
            result = _make_runner(app)(args)
        return captured, result

    def test_codex_ignores_persisted_local_slot(self):
        calls, result = self._run(True, "minicpm5-2b-q4", "gpt-6-astra", {"gpt-6-astra": True})
        assert calls == ["gpt-6-astra"]
        assert "[error]" not in result

    def test_local_ignores_persisted_codex_model(self):
        calls, result = self._run(False, "gpt-6-astra", "local-main", {"local-main": True})
        assert calls == ["local-main"]
        assert "[error]" not in result

    def test_local_unloaded_child_is_not_requested(self):
        calls, _ = self._run(False, "unloaded-child", "local-main",
                             {"local-main": True, "unloaded-child": False})
        assert calls == ["local-main"]

    def test_valid_persisted_codex_child_wins(self):
        calls, _ = self._run(True, "gpt-child", "gpt-main", {"gpt-child": True, "gpt-main": True})
        assert calls == ["gpt-child"]

    def test_explicit_model_wins(self):
        calls, _ = self._run(True, "gpt-child", "gpt-main", {"gpt-main": True}, explicit="gpt-explicit")
        assert calls == ["gpt-explicit"]

    def test_no_valid_codex_model_returns_existing_error_without_request(self):
        calls, result = self._run(True, "local-child", "local-main", {})
        assert calls == []
        assert result == "[error] ProviderError: Select a Codex model for the subagent with /model or its model argument."

    def test_residency_is_refreshed_between_calls(self):
        from litetui.plugins.subagent_plugin import _make_runner
        residents = ["local-child", "local-main"]
        app = SimpleNamespace(
            backend=SimpleNamespace(remote=False, loaded_models=lambda: list(residents)),
            model_id="local-main",
            model_rows={"local-child": SimpleNamespace(loaded=True)},
            settings=SimpleNamespace(subagent_model="local-child", compact_max_tokens=100),
        )
        calls = []
        def complete(app, payload, **kwargs):
            calls.append(payload["model"])
            return {"choices": [{"message": {"content": "ok"}}]}
        runner = _make_runner(app)
        with patch("litetui.plugins.subagent_plugin.model_transport.complete_sidecall", complete):
            runner({"prompt": "first"})
            residents.remove("local-child")
            runner({"prompt": "second"})
            app.backend = SimpleNamespace(remote=True, models={"gpt-main": {}})
            app.model_id = "gpt-main"
            runner({"prompt": "third"})
        assert calls == ["local-child", "local-main", "gpt-main"]
