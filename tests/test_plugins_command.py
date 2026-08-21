"""/plugins and settings.plugins_disabled — the observability of the loader.

The failure-isolation policy is only honest while its outcomes are visible,
and a disable flag is only real if disabling actually discriminates: every
positive here rides with a negative control.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import app as m  # noqa: E402
import settings as settings_mod  # noqa: E402
from plugins import PLUGIN_LOAD_ORDER  # noqa: E402
from settings import Settings  # noqa: E402


def _app(monkeypatch, **settings_kw):
    monkeypatch.setattr(settings_mod, "load", lambda: Settings(**settings_kw))
    a = m.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def test_plugins_command_lists_every_plugin_active(monkeypatch):
    a = _app(monkeypatch)
    msgs = []
    a._system = lambda t: msgs.append(t)
    a._handle_command("/plugins")
    out = msgs[-1]
    assert f"{len(PLUGIN_LOAD_ORDER)} plugin(s)" in out
    for pid in ("core-tools", "scheduler", "misc", "mcp"):
        assert pid in out, f"{pid} missing from /plugins"
    assert out.count("active") == len(PLUGIN_LOAD_ORDER)
    assert "disabled" not in out and "failed" not in out


def test_disable_discriminates_and_survives_in_plugins_output(monkeypatch):
    a = _app(monkeypatch, plugins_disabled=["mark"])
    msgs = []
    a._system = lambda t: msgs.append(t)
    # the disabled plugin's command is genuinely gone...
    a._handle_command("/mark")
    assert "Unknown: /mark" in msgs[-1]
    # ...its status says so...
    a._handle_command("/plugins")
    assert "disabled  mark" in msgs[-1]
    # ...and /plugins itself survives BECAUSE it is host-registered, not a
    # plugin's — the readout must not die with a disabled plugin.
    a2 = _app(monkeypatch, plugins_disabled=["misc"])
    msgs2 = []
    a2._system = lambda t: msgs2.append(t)
    a2._handle_command("/plugins")
    assert "disabled  misc" in msgs2[-1]
    # negative control on the same app: an ENABLED plugin's command still
    # works (a synchronous one — /mark is a worker and needs a live loop)
    a2._handle_command("/cron list")
    assert "Unknown" not in msgs2[-1]


def test_unknown_id_in_the_disable_list_is_inert(monkeypatch):
    # A ghost id must change NOTHING — the flag discriminates, it does not
    # merely correlate with "something was configured".
    a = _app(monkeypatch, plugins_disabled=["no-such-plugin-anywhere"])
    assert all(v == "active" for v in a.plugins.status.values()), a.plugins.status
    assert "no-such-plugin-anywhere" not in a.plugins.status


def test_critical_core_tools_ignores_disable(monkeypatch):
    a = _app(monkeypatch, plugins_disabled=["core-tools"])
    names = [s["function"]["name"] for s in a._all_tools()]
    for floor in ("bash", "read", "write", "web_fetch"):
        assert floor in names, "the floor capability bowed to a disable flag"
    assert a.plugins.status["core-tools"] == "active"
