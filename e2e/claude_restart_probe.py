"""Opt-in OS-RESTART resume gate: a second process must resume the same native
Claude session, remember what the first one told it, and not absorb another
provider's intervening message.

This is the acceptance row "Restart / missing native session: exact resume or
explicit recovery; no silent fresh-session substitution", and it cannot be shown
in one process. A single process could pass by keeping the SDK client alive in
memory; only a real `exit` and a second `python -m litetui.cli` proves the
resume came off disk. So phase 1 runs to completion, shuts down cleanly and is
WAITED ON, and phase 2 is a separate OS process over the same data root.

WHAT EACH ASSERTION IS FOR:
  * the nonce      — phase 2 asks for a word only phase 1 ever said. A fresh
                     native session cannot answer it, so this is the difference
                     between "resumed" and "silently started over".
  * the session id — read from the ledger on disk before and after. The nonce
                     could in principle survive a re-imported transcript; the id
                     proves it is the SAME native session, which is the actual
                     contract (resume by exact native ID, plan 3.1).
  * the sentinel   — must NOT come back. See the limitation below.
  * owned children — after a clean shutdown there must be no surviving child of
                     THIS probe's process. Only the recorded pid tree is ever
                     examined; never "all claude.exe", which would target the
                     operator's own CLI session.

🔴 LIMITATION, STATED BECAUSE IT CHANGES WHAT THE SENTINEL PROVES. The
intervening "other provider" exchange is written DIRECTLY into convo.jsonl
rather than produced by running a second backend. Running LM Studio or
llama.cpp here would load a model into VRAM, which this box does not permit
without explicit approval, and a probe is not the place to ask. What is under
test is the IMPORT BOUNDARY — whether Claude's native context absorbs foreign
display history on resume — and a `{"type": "msg", ...}` record is exactly what
a real other-provider turn leaves behind for that boundary to see. It does NOT
exercise the other provider's own turn machinery. Treat a pass as "the boundary
holds against foreign transcript history", not "cross-provider switching works".

RUN:  set LITETUI_CLAUDE_LIVE=1  and  python e2e/claude_restart_probe.py
Writes artifacts/claude-restart-probe.json either way.
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "claude-restart-probe.json"


class Rpc:
    """One actual `--rpc` child, read on a thread. Mirrors claude_rpc_smoke.py."""

    def __init__(self, root: Path, convo_id: str | None = None):
        # Both public resume entry points are probeable: GUI RPC and --convo.
        self.command = [sys.executable, "-m", "litetui.cli", "--rpc",
                        "--backend", "claude"]
        if convo_id:
            self.command += ["--convo", convo_id]
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ, LITETUI_DATA_ROOT=str(root), LITETUI_NO_HARNESS="1"),
            cwd=root, text=True, encoding="utf-8")
        self.lines: queue.Queue = queue.Queue()
        self.events: list[dict] = []
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    @property
    def pid(self) -> int:
        return self.process.pid

    def _read(self):
        for line in self.process.stdout:
            try:
                self.lines.put(json.loads(line))
            except ValueError:
                pass
        self.lines.put({"type": "process_exit"})

    def _read_err(self):
        self.stderr.extend(self.process.stderr)

    def send(self, type, **values):
        self.process.stdin.write(json.dumps({"type": type, **values}) + "\n")
        self.process.stdin.flush()

    def until(self, type, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            self.events.append(event)
            if event.get("type") == "process_exit":
                raise AssertionError("RPC exited early: " + "".join(self.stderr)[-2500:])
            if event.get("type") == "submit_refused":
                raise AssertionError(f"submit refused: {event}")
            if event.get("type") == type:
                return event
        raise AssertionError(f"No {type} within {timeout}s")

    def request(self, type, **values):
        """One gui.* management call, awaited by its own id."""
        request_id = f"req-{type}-{len(self.events)}"
        self.send(type, id=request_id, **values)
        return self.until_response(request_id)

    def until_response(self, request_id, timeout=120):
        for event in self.events:          # it may already have gone past
            if event.get("type") == "response" and event.get("id") == request_id:
                return self._checked(event)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            self.events.append(event)
            if event.get("type") == "process_exit":
                raise AssertionError("RPC exited early: " + "".join(self.stderr)[-2500:])
            if event.get("type") == "response" and event.get("id") == request_id:
                return self._checked(event)
        raise AssertionError(f"No response to {request_id} within {timeout}s")

    @staticmethod
    def _checked(event):
        if not event.get("ok"):
            raise AssertionError(f"request failed: {event.get('error')}")
        return event.get("result")

    def text(self) -> str:
        return "".join(str(e.get("text", "")) for e in self.events
                       if e.get("type") == "text_delta")

    def shutdown(self, timeout=30) -> int:
        self.send("shutdown", id="shutdown")
        code = self.process.wait(timeout=timeout)
        if self.process.stdin:
            self.process.stdin.close()
        return code

    def kill_if_alive(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)


# ---- owned-process accounting -------------------------------------------------
# A pid alone is not an identity: Windows reuses pids, so every recorded child
# carries its creation time and is only called "still alive" when BOTH match.

def _descendants_psutil(pid):
    import psutil
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    out = []
    for child in parent.children(recursive=True):
        try:
            out.append({"pid": child.pid, "name": child.name(),
                        "create_time": child.create_time()})
        except psutil.Error:
            pass
    return out


def _descendants_cim(pid):
    """One CIM query, walked in Python.

    `Win32_Process` costs seconds on this box and a `-Filter` does not help, so
    it is queried ONCE and the tree is built here rather than per level.
    """
    script = ("Get-CimInstance Win32_Process | Select-Object ProcessId,"
              "ParentProcessId,Name,CreationDate | ConvertTo-Json -Compress")
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                           "-Command", script],
                          capture_output=True, text=True, timeout=120,
                          check=False)
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return []
    rows = rows if isinstance(rows, list) else [rows]
    by_parent: dict[int, list[dict]] = {}
    for row in rows:
        by_parent.setdefault(int(row.get("ParentProcessId") or 0), []).append(row)
    out, frontier = [], [pid]
    while frontier:
        current = frontier.pop()
        for row in by_parent.get(current, []):
            child = int(row["ProcessId"])
            out.append({"pid": child, "name": row.get("Name"),
                        "create_time": str(row.get("CreationDate"))})
            frontier.append(child)
    return out


def owned_children(pid):
    try:
        import psutil  # noqa: F401
        return _descendants_psutil(pid), "psutil"
    except ModuleNotFoundError:
        return _descendants_cim(pid), "Win32_Process (psutil absent)"


def still_alive(recorded, source):
    """Which recorded children are STILL the same process. Never a broad scan."""
    if not recorded:
        return []
    if source == "psutil":
        import psutil
        alive = []
        for child in recorded:
            try:
                process = psutil.Process(child["pid"])
                if process.create_time() == child["create_time"]:
                    alive.append(child)
            except psutil.Error:
                pass
        return alive
    # 🔴 A PID IS NOT AN IDENTITY, AND THIS RUN PROVED IT: phase 2's claude.exe
    # came back on pid 47844, the very pid phase 1's had used, 6.4s later. A
    # pid-only liveness check (what `tasklist /FI "PID eq n"` can offer) would
    # have reported phase 1's child as a surviving leak. So the creation time is
    # compared too, and only a match on BOTH counts as the same process.
    current = _cim_create_times()
    return [child for child in recorded
            if str(current.get(child["pid"], "")) == str(child["create_time"])]


def _cim_create_times():
    script = ("Get-CimInstance Win32_Process | Select-Object ProcessId,"
              "CreationDate | ConvertTo-Json -Compress")
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                           "-Command", script],
                          capture_output=True, text=True, timeout=120,
                          check=False)
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return {}
    rows = rows if isinstance(rows, list) else [rows]
    return {int(r["ProcessId"]): str(r.get("CreationDate")) for r in rows
            if r.get("ProcessId") is not None}


def read_ledger(convo_dir: Path) -> dict:
    path = convo_dir / "claude_ledger.json"
    if not path.is_file():
        raise AssertionError(f"no ledger at {path}; nothing was persisted")
    return json.loads(path.read_text(encoding="utf-8"))


def selected(ledger: dict) -> dict:
    segment = ledger["segments"].get(ledger.get("selected_segment") or "")
    if segment is None:
        raise AssertionError(f"ledger has no selected segment: {ledger}")
    return segment


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--via-cli", action="store_true", help="Resume phase 2 with --convo instead of GUI RPC")
    args = parser.parse_args()
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1; no model call made")
    from litetui import settings

    nonce = "ORCHID-" + secrets.token_hex(4).upper()
    sentinel = "MAGPIE-" + secrets.token_hex(4).upper()
    evidence: dict = {"nonce": nonce, "sentinel": sentinel,
                      "python": sys.version.split()[0], "resume_entry": "cli" if args.via_cli else "gui"}
    first = second = None
    # NOT TemporaryDirectory: a failure here is only diagnosable with the data
    # root still on disk. Removed at the end only when everything passed.
    root = Path(tempfile.mkdtemp(prefix="litetui-claude-restart-"))
    evidence["data_root"] = str(root)
    if True:
        settings.save(settings.Settings(
            backend="claude", backend_chosen=True, default_model="default",
            tools_enabled=False, skills_enabled=False, mcp_enabled=False,
            plugins_disabled=["scheduler", "skills", "harness", "glassbox"],
            tool_policy_profile="strict"), root=root)
        try:
            # ---- PHASE 1: teach it the nonce, then really exit ---------------
            first = Rpc(root)
            first.until("ready")
            first.send("prompt", id="teach",
                       message=f"Remember this token: {nonce}. Reply only READY.")
            first.until("turn_start")
            assert first.until("turn_end").get("stopReason") == "stop", first.events[-1]
            children, source = owned_children(first.pid)
            evidence["child_source"] = source
            evidence["phase1_children"] = children
            evidence["phase1_exit"] = first.shutdown()
            assert evidence["phase1_exit"] == 0, evidence["phase1_exit"]
            leaked = still_alive(children, source)
            evidence["phase1_leaked_children"] = leaked
            assert not leaked, f"owned children survived a clean shutdown: {leaked}"

            convos = sorted((root / ".convos").glob("*/convo.jsonl"))
            assert len(convos) == 1, [str(p) for p in convos]
            convo_dir = convos[0].parent
            convo_id = convo_dir.name
            before = selected(read_ledger(convo_dir))
            evidence["convo_id"] = convo_id
            evidence["segment_before"] = before
            assert before.get("session_id"), (
                "phase 1 never bound a native session id; a resume cannot be "
                f"exact: {before}")

            # ---- an intervening OTHER-PROVIDER exchange (see LIMITATION) -----
            with (convo_dir / "convo.jsonl").open("a", encoding="utf-8") as handle:
                for role in ("user", "assistant"):
                    record = {"type": "msg", "ts": time.time(), "message": {
                        "role": role,
                        "content": f"[other provider] the secret word is {sentinel}"}}
                    if role == "assistant":
                        record["model"] = "qwen/qwen3-8b"
                        record["provider"] = "lmstudio"
                    handle.write(json.dumps(record) + "\n")

            # ---- PHASE 2: a NEW process, same conversation -------------------
            second = Rpc(root, convo_id if args.via_cli else None)
            second.until("ready")
            if not args.via_cli:
                second.request("gui.hello", protocol_version=1)
                opened = second.request("gui.conversations.open", session_id=convo_id)
                evidence["reopened_session_id"] = (opened or {}).get("session_id")
                assert evidence["reopened_session_id"] == convo_id, opened
            second.send("prompt", id="recall", message=(
                "Two questions, one line each. 1) What token did I ask you to "
                "remember? 2) Do you know a secret word beginning with MAGPIE? "
                "Answer NO-SECRET if you do not."))
            second.until("turn_start")
            assert second.until("turn_end").get("stopReason") == "stop", second.events[-1]
            reply = second.text()
            evidence["phase2_reply"] = reply[:2000]

            # 🔴 THE BEFORE/AFTER COMPARISON ALONE IS NOT A TEST. Reading the
            # same file twice passes trivially when phase 2 wrote somewhere
            # else entirely, which is exactly what the first live run did: a
            # second conversation was created, the original ledger was never
            # touched, and "resumed_same_session_id" was True by comparing a
            # record with itself. So enumerate the conversations, and require
            # phase 2's own prompt to appear in THIS segment.
            after_convos = sorted((root / ".convos").glob("*/convo.jsonl"))
            evidence["convo_dirs_after"] = [p.parent.name for p in after_convos]
            evidence["ledgers_after"] = {
                p.parent.name: json.loads((p.parent / "claude_ledger.json").read_text(encoding="utf-8"))
                for p in after_convos if (p.parent / "claude_ledger.json").is_file()}
            evidence["phase2_system_notices"] = [
                str(e.get("text") or e.get("message") or e)[:300]
                for e in second.events
                if e.get("type") in {"system", "system_message", "notice", "error"}][:20]
            after = selected(read_ledger(convo_dir))
            evidence["segment_after"] = after
            evidence["phase2_started_a_second_conversation"] = len(after_convos) != 1
            recall_entries = [e for e in after["entries"]
                              if e.get("operation_id") == "recall"]
            evidence["phase2_prompt_in_original_segment"] = bool(recall_entries)
            evidence["resumed_same_segment"] = after["id"] == before["id"]
            evidence["resumed_same_session_id"] = after["session_id"] == before["session_id"]
            evidence["remembered_nonce"] = nonce in reply
            evidence["sentinel_absent"] = sentinel not in reply
            ledger_text = (convo_dir / "claude_ledger.json").read_text(encoding="utf-8")
            evidence["sentinel_absent_from_ledger"] = sentinel not in ledger_text

            children2, _ = owned_children(second.pid)
            evidence["phase2_children"] = children2
            evidence["phase2_exit"] = second.shutdown()
            leaked2 = still_alive(children2, source)
            evidence["phase2_leaked_children"] = leaked2

            assert not evidence["phase2_started_a_second_conversation"], (
                "phase 2 did not resume the conversation, it created another: "
                f"{evidence['convo_dirs_after']}")
            assert evidence["phase2_prompt_in_original_segment"], (
                "phase 2 answered without recording a delivery in the resumed "
                "segment, so it did not run the Claude native turn for this "
                "conversation at all")
            assert evidence["resumed_same_segment"], (
                f"phase 2 selected a different segment: {before['id']} -> {after['id']}")
            assert evidence["resumed_same_session_id"], (
                "phase 2 did not resume the same native session: "
                f"{before['session_id']} -> {after['session_id']}")
            assert evidence["remembered_nonce"], (
                f"the resumed session does not remember {nonce}; reply was {reply[:500]!r}")
            assert evidence["sentinel_absent"], (
                f"the intervening other-provider message WAS imported: {reply[:500]!r}")
            assert evidence["sentinel_absent_from_ledger"], (
                "the intervening message reached the delivery ledger")
            assert evidence["phase2_exit"] == 0, evidence["phase2_exit"]
            assert not leaked2, f"owned children survived phase 2: {leaked2}"
            evidence["result"] = "PASS"
        except BaseException as exc:
            evidence["result"] = "FAIL"
            evidence["error"] = f"{type(exc).__name__}: {exc}"
            for name, rpc in (("phase1", first), ("phase2", second)):
                if rpc is not None:
                    evidence[f"{name}_stderr_tail"] = "".join(rpc.stderr)[-1500:]
                    evidence[f"{name}_last_events"] = rpc.events[-25:]
            raise
        finally:
            for rpc in (first, second):
                if rpc is not None:
                    rpc.kill_if_alive()
            ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
            artifact = ARTIFACT.with_name("claude-restart-cli-probe.json") if args.via_cli else ARTIFACT
            artifact.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
            if evidence.get("result") == "PASS":
                shutil.rmtree(root, ignore_errors=True)
            else:
                print(f"data root kept for diagnosis: {root}")
            print(f"{evidence.get('result', 'FAIL')} {artifact}")


if __name__ == "__main__":
    main()
