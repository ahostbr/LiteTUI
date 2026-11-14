"""T1306-B (P2): the router LiteTUI spawns keeps a conversation through side calls.

Measured problem (prefix-audit REPORT.md §2, `slot_steal.py`): on the b9360 router, 12 full
re-reads of 51k-127k tokens, 595 s of 1,628 s of prefill, each after a ~200-token side call.
Three llama-server flags fix it, and the router must get them by default:

    --cache-ram 32768   --slot-prompt-similarity 0.5   --no-cache-idle-slots

WHERE THEY GO is decided by llama.cpp source, identical at b9360 (6b4e4bd58) and b12078
(19e8a86fe):
  * `--models-preset` ini has a GLOBAL section `[*]` (common/preset.cpp:356-358 b9360,
    :339 b12078), cascaded UNDER every model section (`cascade(global, ...)`,
    server-models.cpp:299-302 b9360, :747-749 b12078; preset.cpp:479-487 copies the global
    then merges the section over it). So a per-model value wins.
  * The router's OWN argv is merged OVER every model last (`preset.merge(base_preset)`,
    server-models.cpp:315-318 b9360, :774 b12078). Router argv would beat the per-model
    value the user typed, so it is the wrong place.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from litetui import llm_backend, paths
from litetui.llm_backend import BackendError, LlamaCppBackend, ModelRow, render_preset_ini
from litetui.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
ALL_FLAGS = frozenset(llm_backend.FLAG_FOR.values())
ROUTER_FLAGS = ("cache-ram", "slot-prompt-similarity", "cache-idle-slots")


@pytest.fixture(autouse=True)
def _sandboxed(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "LLAMA_DIR", tmp_path / ".llama")
    monkeypatch.setattr(llm_backend, "installed_flags", lambda *a: ALL_FLAGS)
    monkeypatch.setattr(llm_backend, "sibling_mmproj", lambda path: (None, ()))


def _rows() -> list[ModelRow]:
    return [ModelRow(key="a-model", path="C:/m/a.gguf", source="custom"),
            ModelRow(key="b-model", path="C:/m/b.gguf", source="custom")]


# ── A faithful transcription of how the router reads the ini ────────────────────────
def _parse_ini(body: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    cur = None
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = sections.setdefault(line[1:-1], {})
        else:
            k, v = (s.strip() for s in line.split("=", 1))
            cur[k] = v
    return sections


def _effective(body: str, argv_flags: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """What each child is started with: `[*]` first, the model's section over it
    (preset.cpp:479-487), then the router's own argv over everything
    (server-models.cpp:315-318). `[*]` itself is not a model (preset.cpp:356-360)."""
    sections = _parse_ini(body)
    glob = sections.pop("*", {})
    return {name: {**glob, **opts, **(argv_flags or {})} for name, opts in sections.items()}


# ── 1. The rendered ini carries the three flags by default ─────────────────────────
def test_router_defaults_are_in_the_global_section():
    body = render_preset_ini(_rows(), Settings())
    glob = _parse_ini(body).get("*")
    assert glob == {"cache-idle-slots": "false", "cache-ram": "32768", "slot-prompt-similarity": "0.5"}, body
    # The global section comes before any model section, and every child inherits it.
    assert body.index("[*]") < body.index("[a-model]")
    for opts in _effective(body).values():
        assert opts["cache-ram"] == "32768"
        assert opts["slot-prompt-similarity"] == "0.5"
        assert opts["cache-idle-slots"] == "false"


def test_router_defaults_follow_settings():
    s = Settings()
    s.llama_cache_ram_mib = 65536
    s.llama_slot_prompt_similarity = 0.6
    s.llama_cache_idle_slots = True
    glob = _parse_ini(render_preset_ini(_rows(), s))["*"]
    assert glob == {"cache-idle-slots": "true", "cache-ram": "65536", "slot-prompt-similarity": "0.6"}


def test_control_a_per_model_value_wins():
    s = Settings()
    s.llama_load_settings = {"a-model": {"cache_ram": 4096, "slot_similarity": 0.2, "cache_idle_slots": True}}
    eff = _effective(render_preset_ini(_rows(), s))
    assert eff["a-model"]["cache-ram"] == "4096"
    assert eff["a-model"]["slot-prompt-similarity"] == "0.2"
    assert eff["a-model"]["cache-idle-slots"] == "true"
    assert eff["b-model"]["cache-ram"] == "32768"          # the neighbour keeps the default


def test_control_router_argv_would_beat_the_per_model_value():
    """Why the flags are NOT on the router argv: base_preset merges last."""
    s = Settings()
    s.llama_load_settings = {"a-model": {"cache_ram": 4096}}
    eff = _effective(render_preset_ini(_rows(), s), argv_flags={"cache-ram": "32768"})
    assert eff["a-model"]["cache-ram"] == "32768"


def test_control_a_build_without_a_flag_gets_no_line_for_it(monkeypatch):
    monkeypatch.setattr(llm_backend, "installed_flags", lambda *a: ALL_FLAGS - {"cache-idle-slots"})
    glob = _parse_ini(render_preset_ini(_rows(), Settings()))["*"]
    assert "cache-idle-slots" not in glob
    assert glob == {"cache-ram": "32768", "slot-prompt-similarity": "0.5"}


def test_control_an_unknown_census_emits_no_global_section(monkeypatch):
    """No --help answer proves nothing, so no router default is written."""
    monkeypatch.setattr(llm_backend, "installed_flags", lambda *a: frozenset())
    assert "[*]" not in render_preset_ini(_rows(), Settings())


def test_the_spawned_router_reads_the_defaults_from_its_ini_not_its_argv(monkeypatch, tmp_path):
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"")
    s = Settings()
    s.llama_executable = str(exe)
    s.llama_attach_hosts = []
    monkeypatch.setattr(llm_backend, "scan_models", lambda settings: _rows())
    seen: list[list[str]] = []

    def popen(argv, **kw):
        seen.append(list(argv))
        raise RuntimeError("stop before a process exists")

    monkeypatch.setattr(llm_backend.ttyguard, "popen", popen)
    with pytest.raises(RuntimeError):
        LlamaCppBackend(s)._spawn()
    argv = seen[0]
    ini = Path(argv[argv.index("--models-preset") + 1])
    assert _parse_ini(ini.read_text(encoding="utf-8"))["*"]["cache-ram"] == "32768"
    assert not [a for a in argv if a.lstrip("-") in ROUTER_FLAGS or a.lstrip("-") == "no-cache-idle-slots"]


class _Health:
    """A server that answers /health and nothing else: an attach target."""

    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"status": "ok"} if self.path == "/health" else {"error": "no"}).encode()
                self.send_response(200 if self.path == "/health" else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"


def test_control_an_attached_server_gets_nothing(monkeypatch):
    srv = _Health()
    try:
        s = Settings()
        s.llama_host = "http://127.0.0.1:1"
        s.llama_attach_hosts = [srv.host]
        b = LlamaCppBackend(s)
        asyncio.run(b.ensure_running())
        assert b.attached
        writes: list[str] = []
        monkeypatch.setattr(llm_backend, "_write_preset", lambda body, dest=None: writes.append(body))
        spawns: list = []
        monkeypatch.setattr(llm_backend.ttyguard, "popen", lambda *a, **k: spawns.append(a))
        with pytest.raises(BackendError):
            b._regen_ini()
        with pytest.raises(BackendError):
            asyncio.run(b.apply_load_settings("a-model", {"cache_ram": 4096}))
        assert writes == [] and spawns == []
        assert not paths.LLAMA_DIR.exists() or not list(paths.LLAMA_DIR.glob("*.ini"))
    finally:
        srv.server.shutdown()


# ── 2. Census from the real --help text of both installed builds ────────────────────
@pytest.mark.parametrize("build", ["b9360", "b12078"])
def test_both_builds_advertise_the_three_flags(build):
    flags = llm_backend.parse_supported_flags((FIXTURES / f"llama_server_help_{build}.txt").read_text(encoding="utf-8"))
    for flag in ROUTER_FLAGS:
        assert flag in flags, f"--{flag} missing from {build} --help"
    assert "models-preset" in flags


def test_census_control_a_help_without_the_flags_reports_none():
    flags = llm_backend.parse_supported_flags("--ctx-size N  set context\n--models-preset PATH  ini\n")
    assert not set(ROUTER_FLAGS) & flags


# ── 3. Boundary proof: a transcription of b9360 slot choice and the RAM prompt cache ─
# Every rule below is transcribed from llama.cpp 6b4e4bd58 (b9360); llamacpp.md Q2/Q5/Q10.
MAIN_TOKENS = 127_000
MAIN_MIB = 12_700.0      # a 127k hybrid entry with 32 checkpoints: 12.4-12.7 GiB (llamacpp.md Q10b-1)
SIDE_MIB = 326.511       # a 436-token side entry, from the log (llamacpp.md Q10b-2)
N_CTX = 150_016          # the child's token cap: ctx 150000 padded (server-context.cpp:1104)


def _lcp(a: list[int], b: list[int]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _select(slots: list[dict], task: list[int], thold: float) -> tuple[int, bool]:
    """get_available_slot, server-context.cpp:1247-1313. Returns (slot index, update_cache)."""
    best, sim_best = None, 0.0
    if thold != 0.0:                                                   # :1253
        for i, s in enumerate(slots):
            if not s["tokens"]:                                        # :1264-1267 empty skipped
                continue
            sim = _lcp(s["tokens"], task) / len(task)                  # :1270 denominator = NEW prompt
            if sim > sim_best and sim > thold:                         # :1273 strict
                best, sim_best = i, sim
        if best is not None:
            f_keep = sim_best * len(task) / len(slots[best]["tokens"])  # :1280
            return best, f_keep < 0.5                                  # :1286-1288
    t_last, pick = None, None
    for i, s in enumerate(slots):                                      # :1293-1313 LRU, `<=` = last tie
        if t_last is None or s["t_last"] <= t_last:
            t_last, pick = s["t_last"], i
    return pick, True


class _PromptCache:
    """server_prompt_cache alloc/update, server-task.cpp:2001-2054, :2131-2170."""

    def __init__(self, limit_mib: float):
        self.limit_mib = limit_mib
        self.states: list[dict] = []

    def save(self, slot: dict) -> None:
        if slot["tokens"]:
            self.states.append({"name": slot["name"], "tokens": len(slot["tokens"]), "mib": slot["mib"]})

    def update(self) -> None:
        size = lambda: sum(s["mib"] for s in self.states)        # noqa: E731
        while len(self.states) > 1 and size() > self.limit_mib:   # :2133-2141 oldest first, keep one
            self.states.pop(0)
        if not self.states:                                       # empty: nothing to cap
            return
        n_tok = sum(s["tokens"] for s in self.states)
        per_tok = size() / n_tok
        cap = max(N_CTX, self.limit_mib / per_tok) if self.limit_mib > 0 else N_CTX   # :2146-2149
        while len(self.states) > 1 and sum(s["tokens"] for s in self.states) > cap:   # :2152-2161
            self.states.pop(0)


def _after_side_call(thold: float, cache_ram_mib: float, idle_slots: bool) -> str:
    """Where the 127k conversation is after one 200-token side call: resident (VRAM),
    restorable (RAM prompt cache), or lost. Start: 4 unified slots (server.cpp:106-110),
    main in slot 0, an earlier 436-token side call idle in slot 1, two empty slots."""
    main = list(range(MAIN_TOKENS))
    head = main[:40]                                                    # ~40 shared tokens (Q2)
    slots = [
        {"name": "main", "tokens": main, "mib": MAIN_MIB, "t_last": 2},
        {"name": "side0", "tokens": head + [-1 - i for i in range(396)], "mib": SIDE_MIB, "t_last": 3},
        {"name": None, "tokens": [], "mib": 0.0, "t_last": -1},
        {"name": None, "tokens": [], "mib": 0.0, "t_last": -1},
    ]
    side = head + [-10_000 - i for i in range(160)]
    cache = _PromptCache(cache_ram_mib)
    i, update_cache = _select(slots, side, thold)
    if update_cache:                                                    # :1316-1342 save, then load
        cache.save(slots[i])                                            # (entries need f_keep >= 0.25
        cache.update()                                                  #  of their own length: none here)
    slots[i] = {"name": "side1", "tokens": side, "mib": 150.0, "t_last": 4, "busy": True}
    if idle_slots:                                                      # :2077-2083 save + clear every idle slot
        for k, s in enumerate(slots):
            if not s.get("busy") and s["tokens"]:
                cache.save(s)
                slots[k] = {"name": None, "tokens": [], "mib": 0.0, "t_last": s["t_last"]}
                cache.update()
    if any(s["name"] == "main" for s in slots):
        return "resident"
    if any(s["name"] == "main" for s in cache.states):
        return "restorable"
    return "lost"


def test_a_side_prompt_scores_between_the_two_thresholds():
    main = list(range(MAIN_TOKENS))
    side = main[:40] + [-1 - i for i in range(160)]
    sim = _lcp(main, side) / len(side)
    assert 0.10 < sim < 0.5, sim           # 0.2: picks the main slot at 0.10, cannot at 0.5
    slots = [{"tokens": main, "t_last": 1}, {"tokens": [], "t_last": -1}]
    assert _select(slots, side, 0.10)[0] == 0
    assert _select(slots, side, 0.5)[0] == 1
    # The main conversation's own next turn still finds its slot at 0.5.
    assert _select(slots, main + list(range(-1, -2001, -1)), 0.5)[0] == 0


def test_control_old_defaults_lose_the_conversation():
    assert _after_side_call(0.10, 8192, True) == "lost"


def test_the_chosen_defaults_keep_it_resident():
    assert _after_side_call(0.5, 32768, False) == "resident"


@pytest.mark.parametrize("thold,ram,idle,where", [
    (0.5, 32768, True, "restorable"),   # idle clearing on: back from RAM, seconds of copy each turn
    (0.5, 8192, True, "lost"),          # 8192 alone cannot hold a 12.7 GiB entry next to anything
    (0.10, 32768, True, "restorable"),  # the hijack saves it to RAM, 32768 keeps it
    (0.10, 32768, False, "restorable"), # hijacked even with clearing off: similarity is what keeps it in VRAM
    (0.5, 8192, False, "resident"),     # clearing off + 0.5 is what keeps it; 32768 is the backstop
])
def test_each_flag_alone(thold, ram, idle, where):
    assert _after_side_call(thold, ram, idle) == where
