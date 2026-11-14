"""T1306-A (prefix-cache audit P1 + P6): Strata's conversation cache is on and the reasoning-effort
sentence goes at the end, by default, through Settings.

P1: `~/.litesuite/llm/strata/strata-uncensored_iq3_xxs.log` (engine 0.1.41) showed 224 full prompt
re-reads, 2,260 s of 2,955 s prefill, each straight after a short card-summary side call
(C:/Projects/.scratch/prefix-audit/REPORT.md §0, §3.1). Engine 0.1.41 parks a conversation only with
`--conversation-cache-mib N` ("default 0 = off", compiled help string), and LiteTUI never set it.

P6: Strata's template writes the effort sentence at the top of the system turn, so a change of effort
re-reads the whole prompt; the installed server's `"effort_position": "end"` moves it to a short
system turn right before the answer (serve/server.py `render_prompt`, `effort_end_args`).

No model, no engine, no server: launch plans are built from a temp install, and the boundary proof
renders the installed template with the installed server's own `render_prompt`, in Strata's venv.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from litetui import settings as st, strata_engine as eng
from litetui.settings import Settings

# What engine 0.1.41's strata.exe carries (grep -a of ~/.litesuite/llm/strata/engine/strata.exe), and
# what engine-0.2.10's strata-dsv41.exe lacks: that binary prints "unknown option %s" for a flag it
# does not have, so a flag it never knew must not be sent to it.
ENGINE_WITH_CACHE = (b"MZ ... --conversation-cache-mib N  --serve: RAM budget for parked conversations "
                     b"(default 0 = off) ... --tail-role-token ID --serve: ...")
ENGINE_WITHOUT_CACHE = b"MZ ... --reserve-mib --dspark unknown option %s ..."


def _install(tmp_path, monkeypatch, engine: bytes | None = ENGINE_WITH_CACHE, extra_args=()):
    from test_strata_engine import _install as install
    monkeypatch.setattr(eng, "log_path", lambda: tmp_path / "logs" / "strata.log")
    settings, (path,) = install(tmp_path, "iq3_xxs")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if engine is not None:
        exe = Path(cfg["exe"])
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(engine)
    if extra_args:
        cfg["args"] += list(extra_args)
        path.write_text(json.dumps(cfg), encoding="utf-8")
    return settings.strata_root, path


def _launched(root, **values) -> tuple[dict, list[str]]:
    """The config Strata's server is started with, read back from the file argv names."""
    argv, _cwd, _cfg, _host = eng.launch_plan(Settings(strata_root=root, **values))
    ran = json.loads(Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8"))
    return ran, [str(a) for a in ran["args"]]


def _flag(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


# ── P1: the conversation cache ────────────────────────────────────────────────

def test_the_default_launch_turns_the_conversation_cache_on(tmp_path, monkeypatch):
    root, _path = _install(tmp_path, monkeypatch)
    _cfg, args = _launched(root)
    assert _flag(args, "--conversation-cache-mib") == "8192"
    # 0.1.41 has no such flag; it must never be emitted (the brief, and the binary's help strings).
    assert "--conversation-cache-min-tokens" not in args
    # The slot count stays the engine's own default (4): nothing in the source argues for another.
    assert "--conversation-cache-slots" not in args


def test_an_explicit_zero_turns_the_cache_off_and_blank_keeps_the_models_config(tmp_path, monkeypatch):
    """CONTROL: the user's choice wins over the default. 0 is sent as 0 (off); None ("as configured")
    sends nothing, so the model's own config decides, as for every other catalog option."""
    root, _path = _install(tmp_path, monkeypatch, extra_args=("--conversation-cache-mib", "2048"))
    _cfg, args = _launched(root, strata_conversation_cache_mib=0)
    assert _flag(args, "--conversation-cache-mib") == "0"
    _cfg, args = _launched(root, strata_conversation_cache_mib=None)
    assert _flag(args, "--conversation-cache-mib") == "2048"
    _cfg, args = _launched(root, strata_conversation_cache_mib=4096)
    assert _flag(args, "--conversation-cache-mib") == "4096"


def test_an_engine_without_the_flag_is_never_sent_it(tmp_path, monkeypatch):
    """strata-ista-das-coder.json runs engine-0.2.10/strata-dsv41.exe, which has no
    --conversation-cache-mib and answers an unknown flag with "unknown option": the default would stop
    that model from starting. The flag is left out, and the log says why."""
    root, _path = _install(tmp_path, monkeypatch, engine=ENGINE_WITHOUT_CACHE)
    _cfg, args = _launched(root)
    assert "--conversation-cache-mib" not in args
    _cfg, args = _launched(root, strata_conversation_cache_mib=4096)
    assert "--conversation-cache-mib" not in args
    log = (tmp_path / "logs" / "strata.log").read_text(encoding="utf-8")
    assert "left out --conversation-cache-mib" in log and "does not have it" in log


def test_an_unreadable_engine_is_not_sent_the_flag_either(tmp_path, monkeypatch):
    """The installed server treats an engine it cannot read as one without --tail-role-token
    (server.py effort_end_args); the same rule here."""
    root, _path = _install(tmp_path, monkeypatch, engine=None)
    _cfg, args = _launched(root)
    assert "--conversation-cache-mib" not in args


# ── P6: the effort sentence at the end ────────────────────────────────────────

def test_the_default_launch_puts_the_effort_at_the_end(tmp_path, monkeypatch):
    root, _path = _install(tmp_path, monkeypatch)
    cfg, _args = _launched(root)
    assert cfg["effort_position"] == "end"


def test_an_explicit_start_keeps_the_effort_at_the_top(tmp_path, monkeypatch):
    """CONTROL: the user's start wins; blank leaves the model's config (here: no key, the server's start)."""
    root, _path = _install(tmp_path, monkeypatch)
    cfg, _args = _launched(root, strata_effort_position="start")
    assert cfg["effort_position"] == "start"
    cfg, _args = _launched(root, strata_effort_position=None, strata_conversation_cache_mib=4096)
    assert "effort_position" not in cfg


# ── the defaults live in Settings and are never written as a user's choice ────

def test_the_defaults_are_settings_defaults():
    s = Settings()
    assert s.strata_conversation_cache_mib == 8192
    assert s.strata_effort_position == "end"
    assert s.strata_conversation_cache_slots is None


def test_a_file_without_the_keys_gets_the_defaults_and_a_saved_choice_wins(tmp_path):
    (tmp_path / st.SETTINGS_FILENAME).write_text(json.dumps({"strata_kv": "int8"}), encoding="utf-8")
    s = st.load(tmp_path)
    assert (s.strata_conversation_cache_mib, s.strata_effort_position) == (8192, "end")
    (tmp_path / st.SETTINGS_FILENAME).write_text(json.dumps(
        {"strata_conversation_cache_mib": 0, "strata_effort_position": None}), encoding="utf-8")
    s = st.load(tmp_path)
    assert (s.strata_conversation_cache_mib, s.strata_effort_position) == (0, None)


def test_an_unrelated_save_does_not_write_the_defaults_as_overrides(tmp_path):
    """75ba6fd1's trap: a default must not become a sticky value in the file just because some other
    setting was saved; a later change of the default would then never reach this user."""
    path = tmp_path / st.SETTINGS_FILENAME
    path.write_text(json.dumps({"strata_kv": "int8"}), encoding="utf-8")
    s = st.load(tmp_path)
    s.strata_kv = "q4_0"
    st.save(s, tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["strata_kv"] == "q4_0"
    assert "strata_conversation_cache_mib" not in saved and "strata_effort_position" not in saved


# ── P6 boundary proof: where two prompts differing only in effort first differ ─

STRATA = Path(os.environ.get("USERPROFILE", "~")).expanduser() / ".litesuite" / "llm" / "strata"
STRATA_PY = eng.server_python(STRATA)

# Runs in Strata's own venv (it has jinja2; LiteTUI's does not). Imports only serve/frontend.py (text,
# no model) and takes `render_prompt` and its constants out of server.py by AST, so the code measured
# is the installed server's own, without importing (or starting) the server.
_PROBE = r'''
import ast, json, sys
from pathlib import Path
root = Path(sys.argv[1]); template_path = sys.argv[2]
sys.path.insert(0, str(root))
from serve.frontend import ChatTemplate
tree = ast.parse((root / "serve" / "server.py").read_text(encoding="utf-8"))
keep = []
for node in tree.body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") in ("EFFORT_TURN", "EFFORT_TEXT") for t in node.targets):
        keep.append(node)
    if isinstance(node, ast.ClassDef):
        keep += [f for f in node.body if isinstance(f, ast.FunctionDef) and f.name == "render_prompt"]
names = {}
exec(compile(ast.Module(body=keep, type_ignores=[]), "server.py", "exec"), names)
class Svc:
    template = ChatTemplate(template_path)
    render_prompt = names["render_prompt"]
system = {"role": "system", "content": "You are LiteTUI. " + "Project rules. " * 200}
tools = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
messages = [system, {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"}, {"role": "user", "content": "second question"}]
out = {}
for end in (False, True):
    svc = Svc(); svc.effort_end = end
    a = svc.render_prompt(messages, tools, {})                         # the default effort (xhigh)
    b = svc.render_prompt(messages, tools, {"reasoning_effort": "low"})
    first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    history = Svc.template.render(messages, tools=tools, add_generation_prompt=False)
    out["end" if end else "start"] = {"first_diff": first, "len": len(a), "history_len": len(history)}
print(json.dumps(out))
'''


def _templates() -> list[Path]:
    found = [STRATA / "serve" / "chat_template.jinja"]
    cfg = eng.read_config(STRATA / "strata-uncensored_iq3_xxs.json") if STRATA.is_dir() else None
    if cfg and cfg.get("tokenizer"):
        found.append(Path(cfg["tokenizer"]) / "chat_template.jinja")   # the one server.py:5721 prefers
    return [p for p in found if p.is_file()]


@pytest.mark.skipif(not (STRATA_PY.is_file() and (STRATA / "serve" / "server.py").is_file()),
                    reason="needs the installed Strata (its venv, server.py and template)")
@pytest.mark.parametrize("template", _templates() or [None], ids=lambda p: p.parent.name if p else "none")
def test_with_end_a_change_of_effort_differs_only_after_the_whole_history(template):
    if template is None:
        pytest.skip("no chat_template.jinja installed")
    proc = subprocess.run([str(STRATA_PY), "-I", "-c", _PROBE, str(STRATA), str(template)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    # start (the old default): the two prompts part inside the first system turn, so everything
    # after it (system prompt, tools, history) is re-read when the effort changes.
    assert got["start"]["first_diff"] < 64, got
    # end: the shared prefix covers the whole rendered history; only the tail differs.
    assert got["end"]["first_diff"] >= got["end"]["history_len"] > 2000, got
