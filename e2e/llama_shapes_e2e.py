"""REAL capture of the TWO llama-server control-plane shapes — the provenance
of ``tests/fixtures/llama_single_model_*.json`` and ``llama_router_*.json``.

D12 says a single-model ``llama-server -m <gguf>`` — LiteSuite's shape, and
the default attach target — is not the router shape ``llm_backend`` was
written against. This script proves that against the INSTALLED binary and
rewrites the fixtures the unit tests replay, so "transcribed from the real
server" stays a checkable claim rather than a comment.

It also pins the D2/D11 root facts: what a ROUTER answers when a chat request
names a model that is unloaded, loading, or absent.

Run:  python e2e/llama_shapes_e2e.py [--write]
      --write refreshes tests/fixtures/ from this run.

Exit codes: 0 = every fact held · 2 = precondition missing (LOUD, fix named)
· 1 = a fact did NOT hold, i.e. the installed engine changed shape.

Memory safety: smallest GGUF only (≤1.5 GB), --n-gpu-layers 0 so nothing
lands on the GPU, both servers tree-killed in finally.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import llm_backend  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
ART = Path(__file__).resolve().parent / "artifacts" / "dual-backend"
MAX_BYTES = int(1.5 * 1024**3)

results: list[str] = []


def step(msg: str) -> None:
    results.append(msg)
    print(f"  [ok] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"  [FAIL] {msg}", flush=True)
    sys.exit(code)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _call(url: str, body: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "LiteTUI-e2e", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:1500]
    except Exception as e:  # noqa: BLE001
        return None, repr(e)


def _smallest_gguf() -> Path | None:
    from settings import Settings
    rows = llm_backend.scan_models(Settings())
    paths_ = [Path(r.path) for r in rows if r.path]
    paths_ = [p for p in paths_ if p.stat().st_size <= MAX_BYTES]
    return min(paths_, key=lambda p: p.stat().st_size) if paths_ else None


class Server:
    """Detached llama-server with both streams in a log file. The console IS
    the app here; a server that inherits it shreds the UI (hard rule)."""

    def __init__(self, argv: list[str], log: Path) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        self.log = log
        self._lf = open(log, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=self._lf,
            stderr=subprocess.STDOUT, cwd=str(llm_backend.LLAMA_EXE.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def wait_healthy(self, host: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _call(f"{host}/health", timeout=3)[0] == 200:
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def close(self) -> None:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._lf.close()
        except OSError:
            pass


def main() -> int:
    write = "--write" in sys.argv
    ART.mkdir(parents=True, exist_ok=True)

    if not llm_backend.LLAMA_EXE.exists():
        die(f"no llama-server at {llm_backend.LLAMA_EXE} — run LiteSuite's "
            "Model Hub wizard to install the engine", 2)
    gguf = _smallest_gguf()
    if gguf is None:
        die("no chat GGUF ≤1.5 GB in any scan root — download a small model in "
            "LiteSuite's Model Hub or LM Studio", 2)
    print(f"engine {llm_backend.installed_build()} · model {gguf.name}\n")

    captured: dict = {}

    # ── shape 1: single-model ────────────────────────────────────────────
    port = _free_port()
    host = f"http://127.0.0.1:{port}"
    srv = Server([str(llm_backend.LLAMA_EXE), "-m", str(gguf),
                  "--host", "127.0.0.1", "--port", str(port),
                  "--ctx-size", "4096", "--n-gpu-layers", "0", "--no-webui"],
                 ART / "shapes-single.log")
    try:
        if not srv.wait_healthy(host, 180):
            die(f"single-model server never became healthy — {srv.log}")
        step("single-model server healthy")

        code, models = _call(f"{host}/models")
        if code != 200 or not isinstance(models, dict):
            die(f"/models returned {code}")
        if "models" not in models:
            die("/models has no Ollama-shaped 'models' array — D12's premise "
                "no longer holds for this build")
        step("/models carries an Ollama-shaped 'models' array beside 'data'")

        entry = models["data"][0]
        if "status" in entry or "architecture" in entry:
            die("a single-model /models entry now HAS status/architecture — "
                "router-shaped parsing would work and the fix is stale")
        step("single-model /models entry has no 'status' and no 'architecture'")
        if not entry["id"].lower().endswith(".gguf"):
            print(f"  [note] id is {entry['id']!r} (alias, not necessarily a filename)")
        step(f"served id = {entry['id']!r}")

        code, props = _call(f"{host}/props")
        if code != 200 or "role" in props:
            die(f"single-model /props should have no 'role' — got {props.get('role')!r}")
        for k in ("model_alias", "model_path", "modalities"):
            if k not in props:
                die(f"single-model /props lost {k!r} — the listing reads it")
        if props["default_generation_settings"]["n_ctx"] != 4096:
            die("props n_ctx is not the window we started with")
        step("/props has no 'role', and carries model_path/modalities/n_ctx")

        for route in ("/models/load", "/models/unload"):
            code, _ = _call(f"{host}{route}", {"model": entry["id"]})
            if code != 404:
                die(f"{route} answered {code} on a single-model server — the "
                    "refusal in llm_backend claims the route does not exist")
        step("/models/load and /models/unload are 404: the routes do not exist")

        captured["single_models"] = models
        captured["single_props"] = props
    finally:
        srv.close()

    # ── shape 2: router ──────────────────────────────────────────────────
    port = _free_port()
    host = f"http://127.0.0.1:{port}"
    ini = ART / "shapes-router.ini"
    ini.write_text(f"[tiny]\nmodel = {gguf.as_posix()}\nctx-size = 4096\n"
                   "n-gpu-layers = 0\n\n", encoding="utf-8")
    srv = Server([str(llm_backend.LLAMA_EXE), "--models-preset", str(ini),
                  "--host", "127.0.0.1", "--port", str(port),
                  "--models-max", "2", "--no-models-autoload"],
                 ART / "shapes-router.log")
    try:
        if not srv.wait_healthy(host, 90):
            die(f"router never became healthy — {srv.log}")
        step("router healthy")

        code, props = _call(f"{host}/props")
        if props.get("role") != "router":
            die(f"router /props role is {props.get('role')!r}, not 'router' — "
                "the shape discriminator would misfire")
        step("router /props answers role='router'")

        errors: dict = {}
        code, body = _call(f"{host}/v1/chat/completions", {
            "model": "tiny", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4})
        errors["chat_unloaded"] = [code, body]
        if code != 400 or "not loaded" not in str(body):
            die(f"chatting an UNLOADED model gave {code} {body!r}, expected a "
                "400 'model is not loaded' (D2/D11's raw error)")
        step("chat against an unloaded model → 400 'model is not loaded'")

        code, body = _call(f"{host}/v1/chat/completions", {
            "model": "not-in-preset", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4})
        errors["chat_unknown"] = [code, body]
        if code != 400 or "not found" not in str(body):
            die(f"chatting an ABSENT model gave {code} {body!r}")
        step("chat against a model the router does not have → 400 'not found'")

        code, body = _call(f"{host}/v1/chat/completions", {
            "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4})
        errors["chat_no_model"] = [code, body]
        step(f"chat with no model field → {code}")

        if _call(f"{host}/models/load", {"model": "tiny"})[0] != 200:
            die("load did not start")
        racer: dict = {}

        def _race():
            racer["r"] = _call(f"{host}/v1/chat/completions", {
                "model": "tiny", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 4}, timeout=180)
        t = threading.Thread(target=_race, daemon=True)
        t.start()
        t.join(timeout=200)
        errors["chat_while_loading"] = racer.get("r")
        code = (racer.get("r") or [None])[0]
        if code not in (503, 200):
            die(f"chat during a load gave {code} — expected 503 'Loading model' "
                "(or 200 if the load beat the request)")
        step(f"chat during a load → {code} (a DIFFERENT status from the 400)")

        captured["router_props"] = props
        captured["router_errors"] = errors
    finally:
        srv.close()

    if write:
        (FIXTURES / "llama_single_model_models_b9360.json").write_text(
            json.dumps(captured["single_models"], indent=2) + "\n", encoding="utf-8")
        p = captured["single_props"]
        keep = {k: p[k] for k in ("model_alias", "model_path", "modalities",
                                  "total_slots", "build_info", "is_sleeping")}
        keep["default_generation_settings"] = {
            "n_ctx": p["default_generation_settings"]["n_ctx"]}
        (FIXTURES / "llama_single_model_props_b9360.json").write_text(
            json.dumps(keep, indent=2) + "\n", encoding="utf-8")
        (FIXTURES / "llama_router_props_b9360.json").write_text(
            json.dumps(captured["router_props"], indent=2) + "\n", encoding="utf-8")
        (FIXTURES / "llama_router_chat_errors_b9360.json").write_text(
            json.dumps(captured["router_errors"], indent=2) + "\n", encoding="utf-8")
        step("fixtures rewritten from this run")

    (ART / "shapes-capture.json").write_text(json.dumps(captured, indent=2),
                                             encoding="utf-8")
    print(f"\n{len(results)} facts proved against {llm_backend.installed_build()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
