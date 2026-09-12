"""Native lifecycle hooks. Configuration and process execution, without UI policy.

The host authorizes every invocation before calling run_hook. This runner never
dispatches tools and cannot recursively invoke hooks.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from litetui import jobkill, paths, row_store, ttyguard

EVENTS = ("app_start", "app_shutdown", "conversation_start", "conversation_resume",
          "conversation_leave", "prompt_before", "tool_before", "tool_after",
          "completion_before", "completion_after")
GATES = frozenset({"prompt_before", "tool_before", "completion_before"})
SOURCES = ("typed", "queued", "interrupted", "rpc", "scheduled", "harness")
OUTPUT_CAP = 64 * 1024
TEXT_CAP = 256 * 1024


class HookError(ValueError):
    pass


def config_paths(workspace: Path) -> tuple[Path, Path]:
    return paths.data_root() / "hooks.json", workspace / ".litetui" / "hooks.json"


def _strings(value, field):
    if not isinstance(value, list) or any(not isinstance(s, str) or "\0" in s for s in value):
        raise HookError(f"{field} must be an array of strings")
    return tuple(value)


@dataclass(frozen=True)
class Hook:
    id: str
    events: tuple[str, ...]
    executable: str
    argv: tuple[str, ...] = ()
    enabled: bool = True
    mode: str = "observe"
    cwd: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    timeout: float = 10
    sources: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    scope: str = "global"

    @classmethod
    def parse(cls, data, scope="global") -> Hook:
        if not isinstance(data, dict):
            raise HookError("hook must be an object")
        unknown = set(data) - (set(cls.__dataclass_fields__) - {"scope"})
        if unknown:
            raise HookError(f"unknown hook fields: {', '.join(sorted(unknown))}")
        for field in ("id", "executable"):
            if not isinstance(data.get(field), str) or not data[field].strip():
                raise HookError(f"{field} is required")
            if "\0" in data[field]:
                raise HookError(f"{field} contains a null byte")
        events = _strings(data.get("events", []), "events")
        mode = data.get("mode", "observe")
        if not events or set(events) - set(EVENTS):
            raise HookError("select at least one supported event")
        if mode not in ("observe", "gate") or (mode == "gate" and set(events) - GATES):
            raise HookError("gate mode supports prompt_before, tool_before, completion_before")
        timeout = data.get("timeout", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
            raise HookError("timeout must be between 1 and 300 seconds")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise HookError("enabled must be a boolean")
        cwd = data.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or "\0" in cwd):
            raise HookError("cwd must be a path string or null")
        env = data.get("env", {})
        if not isinstance(env, dict) or any(
            not isinstance(k, str) or not k or "=" in k or "\0" in k
            or not isinstance(v, str) or "\0" in v for k, v in env.items()
        ):
            raise HookError("env must map valid variable names to strings")
        sources = _strings(data.get("sources", []), "sources")
        if set(sources) - set(SOURCES):
            raise HookError("unsupported prompt source")
        return cls(data["id"], events, data["executable"],
                   _strings(data.get("argv", []), "argv"), enabled, mode, cwd,
                   tuple(sorted(env.items())), timeout, sources,
                   _strings(data.get("tools", []), "tools"), scope)

    def document(self) -> dict:
        data = asdict(self)
        data.pop("scope")
        data["env"] = dict(self.env)
        for field in ("events", "argv", "sources", "tools"):
            data[field] = list(data[field])
        return data

    def approval_name(self, workspace: Path) -> str:
        # Scope includes its absolute location, so project A cannot approve B.
        execution = (self.scope, str(workspace), self.id, self.executable,
                     self.argv, self.cwd, self.env)
        digest = hashlib.sha256(json.dumps(execution).encode()).hexdigest()[:24]
        return f"hook:{self.id}:{digest}"


@dataclass(frozen=True)
class Snapshot:
    hooks: tuple[Hook, ...] = ()
    error: str = ""
    disabled: bool = False

    def matching(self, event: str, source="", tool="") -> tuple[Hook, ...]:
        return tuple(h for h in self.hooks if h.enabled and event in h.events
                     and (not h.sources or source in h.sources)
                     and (not h.tools or any(re.fullmatch(
                         re.escape(p).replace(r"\*", ".*").replace(r"\?", "."), tool)
                         for p in h.tools)))


@contextmanager
def _file_lock(path: Path):
    """Lock a persistent sibling, not the file replaced by atomic save."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


class HookConfig:
    def __init__(self, global_path: Path, project_path: Path):
        self.global_path, self.project_path = global_path, project_path

    def path(self, scope):
        if scope not in ("global", "project"):
            raise HookError("scope must be global or project")
        return self.global_path if scope == "global" else self.project_path

    def read(self, scope) -> tuple[Hook, ...]:
        path = self.path(scope)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return ()
        except (OSError, ValueError) as exc:
            raise HookError(f"{scope} hooks could not be read: {exc}") from exc
        return self.parse_document(data, scope)

    @staticmethod
    def parse_document(data, scope):
        if not isinstance(data, dict) or data.get("version") != 1 or set(data) != {"version", "hooks"}:
            raise HookError(f"{scope}: expected version 1 and hooks array")
        if not isinstance(data["hooks"], list):
            raise HookError(f"{scope}: hooks must be an array")
        hooks = tuple(Hook.parse(d, scope) for d in data["hooks"])
        if len({h.id for h in hooks}) != len(hooks):
            raise HookError(f"{scope}: duplicate hook IDs")
        return hooks

    def snapshot(self) -> Snapshot:
        if os.environ.get("LITETUI_HOOKS", "").lower() == "off":
            return Snapshot(disabled=True)
        try:
            global_hooks, project = self.read("global"), self.read("project")
            overrides = {h.id for h in project}
            return Snapshot(tuple(h for h in global_hooks if h.id not in overrides) + project)
        except HookError as exc:
            return Snapshot(error=str(exc))

    def save(self, scope, edited, baseline) -> tuple[Hook, ...]:
        edited = tuple(Hook.parse(json.loads(json.dumps(h.document())), scope) for h in edited)
        if len({h.id for h in edited}) != len(edited):
            raise HookError("duplicate hook IDs")
        path = self.path(scope)
        with _file_lock(path):
            current = self.read(scope)
            old, new, disk = ({h.id: h for h in rows} for rows in (baseline, edited, current))
            changed = {id for id in old.keys() | new.keys() if old.get(id) != new.get(id)}
            conflicts = {id for id in changed if disk.get(id) != old.get(id) and disk.get(id) != new.get(id)}
            if conflicts:
                raise HookError(f"edit conflict: {', '.join(sorted(conflicts))}; reload before saving")
            old_order = [h.id for h in baseline if h.id in new]
            new_order = [h.id for h in edited if h.id in old]
            reordered = old_order != new_order
            if reordered and [h.id for h in current if h.id in old] != [h.id for h in baseline if h.id in disk]:
                raise HookError("order conflict; reload before saving")
            # Reuse the shared row delta after enforcing the hooks contract's
            # stronger same-entry conflict rule (row_store itself is last-writer).
            merged_rows = row_store.apply_delta(
                [h.document() for h in baseline], [h.document() for h in edited],
                [h.document() for h in current])
            disk = {d["id"]: Hook.parse(json.loads(json.dumps(d)), scope) for d in merged_rows}
            order = [h.id for h in (edited if reordered else current) if h.id in disk]
            order += [h.id for h in edited if h.id not in order and h.id in disk]
            order += [id for id in disk if id not in order]
            merged = tuple(disk[id] for id in order)
            self.atomic_write(path, merged)
            return merged

    @staticmethod
    def atomic_write(path, rows):
        fd, tmp = tempfile.mkstemp(prefix="hooks-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"version": 1, "hooks": [h.document() for h in rows]}, out, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def repair(self, scope, text, expected_bytes):
        """Explicit repair of the exact malformed file the editor displayed."""
        rows = self.parse_document(json.loads(text), scope)
        path = self.path(scope)
        with _file_lock(path):
            if path.read_bytes() != expected_bytes:
                raise HookError("repair conflict; file changed since reload")
            self.atomic_write(path, rows)
        return rows



def event_document(event, workspace, data, *, source="", conversation_id=None, turn_id=None):
    truncated = []
    remaining = TEXT_CAP

    def bounded(value, key="data"):
        nonlocal remaining
        if isinstance(value, str):
            if value.startswith("data:image/"):
                return {"image": True, "media_type": value.split(";", 1)[0][5:], "omitted": True}
            raw = value.encode("utf-8")
            room = remaining
            remaining = max(0, remaining - len(raw))
            if len(raw) > room:
                truncated.append({"field": key, "original_bytes": len(raw)})
                return raw[:room].decode("utf-8", errors="ignore")
            return value
        if isinstance(value, dict):
            return {k: bounded(v, f"{key}.{k}") for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [bounded(v, f"{key}.{i}") for i, v in enumerate(value)]
        return value

    return {"version": 1, "event_id": str(uuid.uuid4()), "event": event,
            "workspace": str(workspace), "source": source,
            "conversation_id": conversation_id, "turn_id": turn_id,
            "data": bounded(data), "truncation": truncated}


@dataclass(frozen=True)
class HookResult:
    allowed: bool
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    seconds: float = 0
    timed_out: bool = False
    truncated: bool = False


async def run_hook(hook: Hook, event: dict, workspace: Path, *, cancelled=lambda: False) -> HookResult:
    """Run an already-authorized script, draining bounded pipes off the UI thread."""
    cancel = threading.Event()

    def execute():
        started = time.monotonic()
        cwd = Path(hook.cwd) if hook.cwd else workspace
        if not cwd.is_absolute():
            cwd = workspace / cwd
        proc = ttyguard.popen([hook.executable, *hook.argv], cwd=cwd,
                              env={**os.environ, **dict(hook.env)}, kill_on_close=True)
        captured = [bytearray(), bytearray()]
        overflow = [False, False]

        def drain(pipe, index):
            try:
                while chunk := pipe.read(4096):
                    raw = chunk.encode("utf-8")
                    room = OUTPUT_CAP - len(captured[index])
                    captured[index].extend(raw[:room])
                    overflow[index] |= len(raw) > room
            except (OSError, ValueError):
                pass
            finally:
                pipe.close()

        def feed():
            try:
                proc.stdin.write(json.dumps({**event, "invocation_id": str(uuid.uuid4())}))
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass  # child closed stdin; the exit/verdict still decides

        workers = [threading.Thread(target=drain, args=(proc.stdout, 0), daemon=True),
                   threading.Thread(target=drain, args=(proc.stderr, 1), daemon=True),
                   threading.Thread(target=feed, daemon=True)]
        for worker in workers:
            worker.start()
        timed_out = False
        while proc.poll() is None or any(w.is_alive() for w in workers):
            if cancelled():
                cancel.set()
            timed_out = time.monotonic() - started >= hook.timeout
            if cancel.is_set() or timed_out:
                ttyguard.kill_tree(proc.pid, proc)
                break
            cancel.wait(.025)
        for worker in workers:
            worker.join(timeout=1)
        # Job handles are integers, not RAII objects. Release even on success;
        # a hook cannot leave detached children or one kernel handle per event.
        job = getattr(proc, "_litetui_job", None)
        if job is not None:
            jobkill.close(job)
            proc._litetui_job = None
        stdout, stderr = (bytes(b).decode("utf-8", errors="ignore") for b in captured)
        result = HookResult(False, stdout=stdout, stderr=stderr, exit_code=proc.poll(),
                            seconds=time.monotonic() - started, timed_out=timed_out,
                            truncated=any(overflow))
        if cancel.is_set():
            return replace(result, reason="hook cancelled")
        if timed_out:
            return replace(result, reason="hook timed out")
        if result.exit_code != 0:
            return replace(result, reason=f"hook exited with {result.exit_code}")
        if hook.mode == "observe":
            return replace(result, allowed=True)
        if any(overflow):
            return replace(result, reason="hook output exceeded 64 KiB")
        try:
            verdict = json.loads(stdout)
            if not isinstance(verdict, dict) or verdict.get("decision") not in ("allow", "deny"):
                raise ValueError("expected an allow or deny decision")
            if verdict["decision"] == "deny":
                reason = verdict.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("deny requires a reason")
                return replace(result, reason=reason)
            return replace(result, allowed=True)
        except ValueError:
            return replace(result, reason="invalid hook verdict; expected allow or deny JSON")

    future = asyncio.create_task(asyncio.to_thread(execute))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        cancel.set()
        await asyncio.shield(future)
        raise
    except (OSError, ValueError) as exc:
        return HookResult(False, f"hook launch failed: {type(exc).__name__}: {exc}")
