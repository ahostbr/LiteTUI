"""Keep an owned engine's prompt cache across a restart (T1306-E, P8 of the prefix-cache audit).

THE MEASURED LOSS (`.scratch/prefix-audit/REPORT.md` §2/§5): every engine start re-reads the
system+tools prefix from zero, and a resumed long conversation re-reads all of it. A saved
engine state put back before the conversation's next request is answered from the cache instead.

THREE STEPS, AND WHY EACH IS WHERE IT IS
  * NOTE: after every main chat round the app hands the request it sent and the usage it got
    back to `note_main`. That request is what a save must cover and what a replay re-sends.
  * SAVE, on a clean stop of an engine LiteTUI started (`save_before_stop`: app quit and
    /engine stop). The card summary (Ryan 2026-09-16: "ask the SAME model") runs on the same
    engine at every turn end, so at a stop the engine's live state is usually that 300-token
    summary, not the conversation. The engine's own token count says which it is; when it is
    provably not the main request, the main request is re-sent once with `max_tokens: 1`
    under `slot_persist_replay_s` (Prefix 431d0c21: default 10 s, the user is quitting), which
    puts the conversation back as the live state, and only then is it saved. A save is kept
    only when the engine's count of saved tokens covers that request; anything else is deleted.
  * RESTORE, lazily, immediately before the first MAIN request after the next owned start whose
    tools + messages[:k] hash equals the record's. Not eagerly at start: the connect-time
    thinking probe is a side request and on Strata it would evict an eager restore before the
    conversation's first turn. Side calls never come through here, so they never consume it.

SAFETY
  * Owned engines only: an attached server (LiteSuite's, a hand-started one) gets no call at all.
  * A restore never loads a model: it runs only after the backend says the model is loaded
    (Strata `/health`; the llama router runs `--no-models-autoload`), and it calls `/slots`, never
    `/models/load` or `/v1/load`.
  * Identity: beside each state a record names the engine binary, the model files (path, size,
    mtime), the context and every launch flag, and the hash of the prompt the state covers. A
    record for the same model whose identity differs is deleted and the reason logged. Strata's
    engine also refuses a session from another model or engine configuration itself (0.1.41
    binary strings "model fingerprint differs", "config fingerprint differs"); llama.cpp does not.
  * Any failure is logged and ignored: the turn proceeds cold.

WHICH ENGINES (verified from source; see the commit body for the lines)
  * Strata: on by default.
  * llama.cpp b9360: off by default. `/slots` refuses whenever an mmproj is loaded, the file holds
    no hybrid checkpoints (so a Qwen3.5/3.8 restore re-reads anyway), and it checks no identity.
  * NInfer: not offered. DeepSeek-V4.1 has no slot path, and `--slot-save-path` moves a Qwen
    artifact off the Hybrid prefix cache.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import runtime_log

SCHEMA = 1
NAME_PREFIX = "lt-"
#: The only names `sweep` and `enforce_cap` ever touch: `lt-<hex>.state|.json`, plus one suffix the
#: writer appends while writing (ours `.json.tmp`; Strata 0.1.41 writes `<name>.tmp` beside the final
#: file: binary string ".tmp" next to "cannot create a temporary file beside"). Anything else in the
#: folder is the user's and is left alone (Prefix 8fb5d719: slot_persist_dir can name any folder).
_OURS = re.compile(r"^lt-[0-9a-f]+\.(?:state|json)(?:\.[A-Za-z0-9_-]+)?$")
#: Written when LiteTUI CREATES an engine's save folder. A folder without it was not made by
#: LiteTUI, so partial files in it are never swept.
MARKER = ".litetui-slot-saves"
STATE_SUFFIX = ".state"
RECORD_SUFFIX = ".json"
GIB = 1024 ** 3
#: How far an engine's token count may sit outside [prompt, prompt + completion] and still be the
#: same request. Strata reports the prompt size exactly and may save its deepest checkpoint, which
#: sits at the prompt's last `<|im_start|>` (a few tokens before its end); llama.cpp's slot holds the
#: prompt plus the generated tokens. A side request is 100-500 tokens against a main request of
#: thousands (REPORT.md §3.1), so 64 cannot confuse the two.
COVER_SLACK_TOKENS = 64
#: One save or restore moves the whole state: 9.5-12.7 GiB for a 74k-129k hybrid conversation
#: (REPORT.md §2). At 1-3 GB/s of disk that is 4-13 s, so 60 s is several times the slowest.
HTTP_TIMEOUT_S = 60.0
#: Message keys LiteTUI keeps on a message but never sends (model_transport.ModelTransport.create
#: drops them before the request leaves), so the prompt hash must drop them too.
_NOT_SENT = ("provider_metadata", "codex_delivery")


def _log(event: str, detail: str) -> None:
    runtime_log.record(f"slot_persist.{event}", detail=detail, site="prefix_persist")


# ── what a state covers ─────────────────────────────────────────────────────

def prompt_key(kwargs: dict, k: int | None = None) -> str:
    """sha256 of what decides the first tokens of a prompt: the model, the tools, the effort and
    template switches, and the messages (the first `k` when given), as the server receives them."""
    extra = kwargs.get("extra_body") or {}
    msgs = list(kwargs.get("messages") or [])
    body = {
        "model": kwargs.get("model"),
        "tools": kwargs.get("tools") or [],
        "reasoning_effort": extra.get("reasoning_effort"),
        "chat_template_kwargs": extra.get("chat_template_kwargs"),
        "messages": [{key: v for key, v in m.items() if key not in _NOT_SENT} if isinstance(m, dict) else m
                     for m in (msgs if k is None else msgs[:k])],
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MainRequest:
    """The last main chat request sent to the engine, and what the engine said it cost."""
    kwargs: dict
    model: str
    messages: int
    key: str
    head: str
    prompt_tokens: int
    completion_tokens: int

    @classmethod
    def of(cls, kwargs: dict, usage: dict | None) -> "MainRequest | None":
        prompt = (usage or {}).get("prompt_tokens")
        if not isinstance(prompt, int) or prompt <= 0:
            return None
        kept = copy.deepcopy(kwargs)
        completion = (usage or {}).get("completion_tokens")
        return cls(kwargs=kept, model=str(kept.get("model") or ""), messages=len(kept.get("messages") or []),
                   key=prompt_key(kept), head=prompt_key(kept, 1), prompt_tokens=prompt,
                   completion_tokens=completion if isinstance(completion, int) and completion > 0 else 0)

    def covered_by(self, tokens) -> bool:
        """Does an engine count of `tokens` describe THIS request's state?"""
        return (isinstance(tokens, int) and not isinstance(tokens, bool)
                and self.prompt_tokens - COVER_SLACK_TOKENS <= tokens
                <= self.prompt_tokens + self.completion_tokens + COVER_SLACK_TOKENS)

    def replay_kwargs(self) -> dict:
        """The same request, byte for byte through the same client, asking for one token."""
        again = copy.deepcopy(self.kwargs)
        again["max_tokens"] = 1
        return again


def file_identity(path) -> dict | None:
    try:
        p = Path(path)
        st = p.stat()
    except (OSError, TypeError, ValueError):
        return None
    if not p.is_file():
        return None
    return {"path": str(p.resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _files_named(values) -> list[dict]:
    """The identity of every value that names an existing file (model shards, mmproj, exe ...)."""
    out = []
    for v in values:
        if isinstance(v, str) and v and not v.startswith("-") and len(v) < 1024:
            ident = file_identity(v)
            if ident is not None:
                out.append(ident)
    return out


def identity_diff(saved: dict, current: dict) -> list[str]:
    """Top-level identity keys that differ, for the log line."""
    return sorted(k for k in set(saved) | set(current) if saved.get(k) != current.get(k))


def persist_root(settings) -> Path:
    chosen = str(getattr(settings, "slot_persist_dir", "") or "").strip()
    if chosen:
        return Path(chosen).expanduser()
    from .paths import data_root
    return data_root() / ".slot-saves"


# ── the folder ──────────────────────────────────────────────────────────────

def ensure_folder(folder: Path) -> Path:
    """Create an engine's save folder, marking it as LiteTUI's when LiteTUI is the one creating it.
    An existing folder is used as it is and never gains the marker."""
    folder = Path(folder)
    if not folder.is_dir():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / MARKER).write_text("Saved engine prompt caches, written by LiteTUI (prefix_persist.py).\n",
                                     encoding="utf-8")
    return folder


class SaveStore:
    """One engine's saves: `lt-<id>.state` (written by the engine) + `lt-<id>.json` (ours, written
    only once the state's coverage is verified). Only names matching `_OURS` are ever deleted, and
    the partial-file sweep runs only in a folder carrying LiteTUI's `MARKER`."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def state(self, name: str) -> Path:
        return self.folder / f"{name}{STATE_SUFFIX}"

    def record_path(self, name: str) -> Path:
        return self.folder / f"{name}{RECORD_SUFFIX}"

    @staticmethod
    def new_name() -> str:
        return NAME_PREFIX + uuid.uuid4().hex[:20]

    def records(self) -> list[dict]:
        """Complete saves, oldest first."""
        out = []
        if not self.folder.is_dir():
            return out
        for p in self.folder.glob(f"{NAME_PREFIX}*{RECORD_SUFFIX}"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = p.name[: -len(RECORD_SUFFIX)]
            if not _OURS.match(p.name):
                continue
            if isinstance(rec, dict) and rec.get("schema") == SCHEMA and rec.get("name") == name \
                    and self.state(name).is_file():
                out.append(rec)
        return sorted(out, key=lambda r: (r.get("created") or 0, r.get("name") or ""))

    def commit(self, record: dict) -> None:
        dest = self.record_path(record["name"])
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, dest)

    def delete(self, name: str) -> None:
        for p in (self.record_path(name), self.state(name)):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log("delete_failed", f"{p}: {type(exc).__name__}")

    def sweep(self) -> list[str]:
        """Delete OUR files (`_OURS`) that are not half of a complete pair; returns the names removed.
        Nothing else is touched, and a folder without LiteTUI's MARKER is not swept at all.
        Call only while no save is being written (no owned engine, or before the first save)."""
        if not self.folder.is_dir():
            return []
        if not (self.folder / MARKER).is_file():
            _log("sweep_refused", f"{self.folder} was not created by LiteTUI (no {MARKER}); nothing swept")
            return []
        complete = {r["name"] for r in self.records()}
        removed = []
        for p in self.folder.iterdir():
            if not p.is_file() or not _OURS.match(p.name):
                continue
            stem = p.name[: -len(STATE_SUFFIX)] if p.name.endswith(STATE_SUFFIX) else \
                p.name[: -len(RECORD_SUFFIX)] if p.name.endswith(RECORD_SUFFIX) else None
            if stem in complete:
                continue
            try:
                p.unlink()
                removed.append(p.name)
            except OSError as exc:
                _log("sweep_failed", f"{p}: {type(exc).__name__}")
        return removed

    def size(self, name: str) -> int:
        total = 0
        for p in (self.state(name), self.record_path(name)):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def enforce_cap(self, cap_bytes: int, keep: str | None = None) -> list[str]:
        """Delete the oldest saves until the folder fits `cap_bytes`; `keep` goes last."""
        recs = self.records()
        sizes = {r["name"]: self.size(r["name"]) for r in recs}
        total = sum(sizes.values())
        order = [r["name"] for r in recs if r["name"] != keep] + ([keep] if keep in sizes else [])
        evicted = []
        for name in order:
            if total <= cap_bytes:
                break
            self.delete(name)
            total -= sizes[name]
            evicted.append(name)
        return evicted


# ── engine adapters ─────────────────────────────────────────────────────────

def _http(method: str, url: str, body: dict | None = None, timeout: float = 10.0):
    """(status, parsed JSON or None). Never raises for an HTTP status; (None, None) when nothing answered."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "LiteTUI"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception:  # noqa: BLE001 - refused, reset or timed out: nothing usable answered
        return None, None
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, None


class StrataSlots:
    """Strata's server: one slot (0), `POST /slots/0?action=save|restore {"filename"}` behind the
    run config's `slot_save_path` (serve/server.py `Service.slot_action`)."""

    name = "strata"
    label = "Strata"

    def __init__(self, backend) -> None:
        self.backend = backend

    @property
    def settings(self):
        return self.backend._settings

    def why_off(self) -> str | None:
        """None when this engine may persist; else the reason (also used at launch)."""
        s = self.settings
        if getattr(self.backend, "name", "") != "strata":
            return "only Strata's own engine writes session files (Xolotl answers ERR unsupported)"
        if not getattr(s, "slot_persist_strata", False):
            return "slot_persist_strata is off"
        if str(getattr(s, "strata_slot_save_path", "") or "").strip():
            return "strata_slot_save_path names your own folder; automatic saves stay out of it"
        if getattr(s, "strata_parallel", None):
            return "Strata refuses session files with parallel requests (strata_parallel)"
        return None

    def folder(self) -> Path:
        return persist_root(self.settings) / "strata"

    def owned(self):
        return getattr(self.backend, "_owned", None)

    def host(self) -> str:
        return self.owned().host

    def identity(self, model: str) -> dict | None:
        owned = self.owned()
        argv = list(getattr(owned, "args", ()) or ())
        if "--config" not in argv[:-1]:
            return None
        cfg_path = argv[argv.index("--config") + 1]
        try:
            cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        # The run config's own path and mtime change on every start (LiteTUI rewrites the copy), so
        # its CONTENT is the identity, not the file.
        launch = [a if a != cfg_path else "<config>" for a in argv]
        return {"engine": "strata", "model": model, "launch": launch, "launcher": _files_named(argv[:2]),
                "config": cfg, "files": _files_named([cfg.get("exe")] + [str(a) for a in cfg.get("args") or ()])}

    def loaded(self, model: str) -> bool:
        return self.backend._health_state(self.host()) == "loaded"

    def slots(self, model: str) -> list[dict] | None:
        status, body = _http("GET", f"{self.host()}/slots")
        return body if status == 200 and isinstance(body, list) else None

    def post(self, slot: int, action: str, filename: str, model: str, timeout: float):
        return _http("POST", f"{self.host()}/slots/{slot}?action={action}", {"filename": filename}, timeout=timeout)

    @staticmethod
    def saved_tokens(body) -> int | None:
        n = (body or {}).get("n_saved") if isinstance(body, dict) else None
        return n if isinstance(n, int) else None


class LlamaSlots:
    """llama.cpp router (b9360): `POST /slots/:id?action=save|restore {"model", "filename"}`, proxied
    to the model's child with the query string (server-models.cpp:1025-1040, 1196-1205); the router's
    `--slot-save-path` reaches every child because router argv is merged into each preset (:317)."""

    name = "llama"
    label = "llama.cpp"

    def __init__(self, backend) -> None:
        self.backend = backend

    @property
    def settings(self):
        return self.backend._settings

    def why_off(self) -> str | None:
        if not getattr(self.settings, "slot_persist_llama", False):
            return "slot_persist_llama is off"
        return None

    def folder(self) -> Path:
        return persist_root(self.settings) / "llama"

    def owned(self):
        return getattr(self.backend, "_owned", None)

    def host(self) -> str:
        return self.backend._host

    def _section(self, model: str) -> list[str]:
        ini = getattr(self.owned(), "ini", None)
        try:
            lines = Path(ini).read_text(encoding="utf-8").splitlines() if ini else []
        except OSError:
            return []
        out, inside = [], False
        for line in lines:
            if line.startswith("["):
                inside = line.strip() == f"[{model}]"
                continue
            if inside and line.strip():
                out.append(line.strip())
        return out

    def identity(self, model: str) -> dict | None:
        section = self._section(model)
        if not section:
            return None
        from .llm_backend import llama_executable
        values = [line.split("=", 1)[1].strip() for line in section if "=" in line]
        return {"engine": "llama", "model": model, "binary": file_identity(llama_executable(self.settings)),
                "section": section, "files": _files_named(values)}

    def loaded(self, model: str) -> bool:
        try:
            entry = self.backend._server_models().get(model) or {}
        except Exception:  # noqa: BLE001 - no answer is "not loaded": never restore into the unknown
            return False
        status = entry.get("status")
        return isinstance(status, dict) and status.get("value") == "loaded"

    def slots(self, model: str) -> list[dict] | None:
        status, body = _http("GET", f"{self.host()}/slots?model={urllib.parse.quote(model)}")
        return body if status == 200 and isinstance(body, list) else None

    def post(self, slot: int, action: str, filename: str, model: str, timeout: float):
        return _http("POST", f"{self.host()}/slots/{slot}?action={action}",
                     {"model": model, "filename": filename}, timeout=timeout)

    @staticmethod
    def saved_tokens(body) -> int | None:
        n = (body or {}).get("n_saved") if isinstance(body, dict) else None
        return n if isinstance(n, int) else None


# ── the engine-agnostic half ────────────────────────────────────────────────

@dataclass(frozen=True)
class SavePlan:
    reason: str | None = None          # nothing to do, and why (logged)
    slot: int | None = None            # the slot already holding the main request
    replay: dict | None = None         # else: the request to re-send first


def _tokens_of(slot: dict) -> int | None:
    n = slot.get("n_prompt_tokens") if isinstance(slot, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) else None


class SlotPersistence:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.last: MainRequest | None = None
        self._restore_open = False
        self._lock = threading.Lock()

    # -- state ---------------------------------------------------------------

    def active(self) -> bool:
        """An engine we started, with persistence on for it."""
        return self.engine.owned() is not None and self.engine.why_off() is None

    def store(self) -> SaveStore:
        return SaveStore(self.engine.folder())

    def cap_bytes(self) -> int:
        cap = getattr(self.engine.settings, "slot_persist_cap_gib", 32)
        return int(cap if isinstance(cap, (int, float)) and cap > 0 else 32) * GIB

    def note_start(self) -> None:
        """An owned engine just started: clear partial saves a crash left, open the restore window."""
        self.last = None
        if self.engine.why_off() is not None:
            self._restore_open = False
            return
        removed = self.store().sweep()
        if removed:
            _log("swept", f"{self.engine.label}: deleted {len(removed)} partial file(s) left by an "
                          f"interrupted save: {', '.join(removed)}")
        self._restore_open = True

    def note_stopped(self) -> None:
        """Our engine has exited: nothing can still be writing, so any partial save goes."""
        self.last = None
        self._restore_open = False
        if self.engine.why_off() is not None:
            return
        removed = self.store().sweep()
        if removed:
            _log("swept", f"{self.engine.label}: deleted partial save file(s) after the stop "
                          f"(a save was cancelled or failed): {', '.join(removed)}")

    def note_main(self, kwargs: dict, usage: dict | None) -> None:
        if not self.active():
            return
        rec = MainRequest.of(kwargs, usage)
        if rec is not None:
            self.last = rec

    def wants_save(self) -> bool:
        return self.active() and self.last is not None

    # -- save ----------------------------------------------------------------

    def _main_slot(self, last: MainRequest) -> tuple[int | None, bool]:
        """(slot holding `last`, could the engine be asked at all)."""
        slots = self.engine.slots(last.model)
        if slots is None:
            return None, False
        for slot in sorted(slots, key=lambda s: -(_tokens_of(s) or 0)):
            if not slot.get("is_processing") and last.covered_by(_tokens_of(slot)):
                return int(slot.get("id", 0)), True
        return None, True

    def plan(self) -> SavePlan:
        last = self.last
        if last is None or not self.active():
            return SavePlan(reason="no main request to save")
        if not self.engine.loaded(last.model):
            return SavePlan(reason=f"{last.model} is not loaded on the {self.engine.label} engine")
        slot, asked = self._main_slot(last)
        if not asked:
            return SavePlan(reason=f"the {self.engine.label} engine did not answer /slots")
        if slot is not None:
            return SavePlan(slot=slot)
        return SavePlan(replay=last.replay_kwargs())

    def save(self, cancel: threading.Event | None = None) -> str:
        """Save the slot holding the last main request; keep it only if the engine's count covers it."""
        with self._lock:
            return self._save(cancel or threading.Event())

    def _save(self, cancel: threading.Event) -> str:
        last = self.last
        if last is None or not self.active():
            return "nothing to save"
        label = self.engine.label
        slot, asked = self._main_slot(last)
        if slot is None:
            why = ("did not answer /slots" if not asked else
                   "does not hold the conversation (its live state is a different request)")
            _log("save_skipped", f"{label}: the engine {why}; nothing saved")
            return f"the {label} engine {why}; nothing saved"
        identity = self.engine.identity(last.model)
        if identity is None:
            _log("save_skipped", f"{label}: could not read the launch identity; nothing saved")
            return "the engine's launch identity could not be read; nothing saved"
        store = self.store()
        ensure_folder(store.folder)
        name = store.new_name()
        started = time.monotonic()
        status, body = self.engine.post(slot, "save", name + STATE_SUFFIX, last.model, HTTP_TIMEOUT_S)
        took = time.monotonic() - started
        n_saved = self.engine.saved_tokens(body)
        if cancel.is_set():
            store.delete(name)
            _log("save_cancelled", f"{label}: skipped by a second quit after {took:.1f} s; partial save deleted")
            return "save skipped"
        if status != 200:
            store.delete(name)
            msg = (body or {}).get("error", {}).get("message") if isinstance(body, dict) and \
                isinstance(body.get("error"), dict) else None
            _log("save_failed", f"{label}: save answered {status} ({msg or 'no reason'}) after {took:.1f} s; "
                                "partial save deleted")
            return f"the {label} engine did not save the conversation ({status}); nothing kept"
        if not last.covered_by(n_saved) or not store.state(name).is_file():
            store.delete(name)
            _log("save_uncovered", f"{label}: the engine saved {n_saved} tokens, the conversation is "
                                   f"{last.prompt_tokens}+{last.completion_tokens}; deleted")
            return "the saved state did not cover the conversation; deleted"
        record = {"schema": SCHEMA, "name": name, "state": name + STATE_SUFFIX, "engine": self.engine.name,
                  "model": last.model, "identity": identity, "prefix": last.key, "head": last.head,
                  "messages": last.messages, "prompt_tokens": last.prompt_tokens,
                  "completion_tokens": last.completion_tokens, "tokens": n_saved, "slot": slot,
                  "bytes": store.state(name).stat().st_size, "created": time.time(), "save_s": round(took, 3)}
        store.commit(record)
        for older in store.records():      # the same conversation saved before: superseded
            if older["name"] != name and older.get("model") == last.model and older.get("head") == last.head:
                store.delete(older["name"])
        evicted = store.enforce_cap(self.cap_bytes(), keep=name)
        if name in evicted:
            _log("save_over_cap", f"{label}: a {record['bytes'] / GIB:.1f} GiB save is larger than "
                                  f"slot_persist_cap_gib; deleted")
            return "the save was larger than the size cap; deleted"
        if evicted:
            _log("evicted", f"{label}: size cap reached, deleted the oldest save(s): {', '.join(evicted)}")
        _log("saved", f"{label}: {n_saved} tokens of {last.model} in {took:.1f} s "
                      f"({record['bytes'] / GIB:.2f} GiB) as {name}")
        return f"saved the conversation cache ({n_saved} tokens, {record['bytes'] / GIB:.1f} GiB, {took:.1f} s)"

    # -- restore -------------------------------------------------------------

    def restore_pending(self) -> bool:
        return self._restore_open and self.active()

    def restore_for(self, kwargs: dict) -> str | None:
        """Before the first main request after an owned start: put back the save whose prompt is a
        prefix of this one. Returns a sentence on success, None otherwise (always logged)."""
        with self._lock:
            if not self._restore_open:
                return None
            self._restore_open = False          # one attempt per start: the next request changes the live state
            if not self.active():
                return None
            return self._restore(kwargs)

    def _restore(self, kwargs: dict) -> str | None:
        label = self.engine.label
        model = str(kwargs.get("model") or "")
        store = self.store()
        records = [r for r in store.records() if r.get("engine") == self.engine.name and r.get("model") == model]
        if not records:
            return None
        current = self.engine.identity(model)
        if current is None:
            _log("restore_skipped", f"{label}: could not read the launch identity; nothing restored")
            return None
        messages = kwargs.get("messages") or []
        best = None
        for rec in records:
            if rec.get("identity") != current:
                store.delete(rec["name"])
                _log("stale", f"{label}: deleted save {rec['name']}: launch identity changed "
                              f"({', '.join(identity_diff(rec.get('identity') or {}, current))})")
                continue
            k = rec.get("messages")
            if isinstance(k, int) and 0 < k <= len(messages) and prompt_key(kwargs, k) == rec.get("prefix"):
                if best is None or k > best["messages"]:
                    best = rec
        if best is None:
            _log("restore_none", f"{label}: no save is a prefix of this request")
            return None
        if not self.engine.loaded(model):
            _log("restore_skipped", f"{label}: {model} is not loaded; restore skipped (a restore never loads)")
            return None
        slots = self.engine.slots(model) or [{"id": 0}]
        idle = [s for s in slots if not s.get("is_processing")] or slots
        slot = int(min(idle, key=lambda s: _tokens_of(s) or 0).get("id", 0))
        started = time.monotonic()
        status, body = self.engine.post(slot, "restore", best["state"], model, HTTP_TIMEOUT_S)
        took = time.monotonic() - started
        n = (body or {}).get("n_restored") if isinstance(body, dict) else None
        if status != 200:
            _log("restore_failed", f"{label}: restore of {best['name']} answered {status} after {took:.1f} s; "
                                   "the turn proceeds cold")
            return None
        _log("restored", f"{label}: {n} tokens from {best['name']} into slot {slot} in {took:.1f} s")
        return f"restored the conversation cache from disk ({n} tokens, {took:.1f} s)"


def for_backend(backend) -> SlotPersistence | None:
    return getattr(backend, "slot_persistence", None)


# ── the app's side (async) ─────────────────────────────────────────────────

def note_main(backend, kwargs: dict, usage: dict | None) -> None:
    persist = for_backend(backend)
    if persist is not None:
        try:
            persist.note_main(kwargs, usage)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never cost a turn
            _log("note_failed", type(exc).__name__)


def _in_daemon(fn, *args):
    """Run blocking `fn` in a DAEMON thread and await it. Not `asyncio.to_thread`: its pool threads
    are joined at interpreter exit, so a cancelled save would still hold the quit for its whole HTTP
    timeout. A daemon thread is left behind; the engine's own stop ends its request."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def settle(setter, value):
        if not fut.done():
            setter(value)

    def run():
        try:
            result = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - handed to the awaiting task
            try:
                loop.call_soon_threadsafe(settle, fut.set_exception, exc)
            except RuntimeError:
                pass
            return
        try:
            loop.call_soon_threadsafe(settle, fut.set_result, result)
        except RuntimeError:          # the loop has closed (the app quit): nobody is waiting
            pass

    threading.Thread(target=run, name="slot-persist", daemon=True).start()
    return fut


async def replay(create, kwargs: dict, cap_s: float) -> bool:
    """Send `kwargs` through `create` (the app's own transport) and read the stream to its end, or
    give up after `cap_s`. True when the engine finished the request."""
    async def run():
        stream = await create(**kwargs)
        try:
            if kwargs.get("stream"):
                async for _ in stream:
                    pass
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - closing a finished or cancelled stream
                    pass

    try:
        await asyncio.wait_for(run(), timeout=cap_s)
        return True
    except asyncio.TimeoutError:
        return False


async def save_before_stop(app, backend, cancel: threading.Event | None = None) -> str | None:
    """The clean-stop save: replay the main request when the engine provably holds something else,
    then save. None when there is nothing to do for this backend."""
    persist = for_backend(backend)
    if persist is None or not persist.wants_save():
        return None
    cancel = cancel or threading.Event()
    label = persist.engine.label
    try:
        plan = await _in_daemon(persist.plan)
        if plan.reason:
            _log("save_skipped", f"{label}: {plan.reason}")
            return None
        if plan.replay is not None:
            cap = getattr(persist.engine.settings, "slot_persist_replay_s", 10)
            cap = float(cap) if isinstance(cap, (int, float)) and cap > 0 else 10.0
            from . import model_transport
            ok = await replay(model_transport.for_app(app).create, plan.replay, cap)
            if not ok:
                _log("replay_over_cap", f"{label}: re-sending the conversation took over {cap:g} s "
                                        "(slot_persist_replay_s); nothing saved")
                return f"the conversation cache was not saved: re-reading it took over {cap:g} s"
        return await _in_daemon(persist.save, cancel)
    except asyncio.CancelledError:
        cancel.set()
        _log("save_cancelled", f"{label}: skipped by a second quit; a partial save is deleted when the engine stops")
        raise
    except Exception as exc:  # noqa: BLE001 - a failed save never blocks a stop or a quit
        _log("save_error", f"{label}: {type(exc).__name__}")
        return None


async def restore_before_main(app, kwargs: dict) -> None:
    persist = for_backend(getattr(app, "backend", None))
    if persist is None or not persist.restore_pending():
        return
    try:
        said = await asyncio.to_thread(persist.restore_for, kwargs)
    except Exception as exc:  # noqa: BLE001 - a failed restore is logged; the turn proceeds cold
        _log("restore_error", type(exc).__name__)
        return
    if said:
        app.system_message(said)


# Quitting: the save runs before Textual's exit takes effect, so a second quit (or Ctrl+C) can still
# be heard and skips it at once (Prefix 431d0c21). Textual's `_shutdown` runs after the message loop
# has stopped, where no key is read any more.

def quit_save_running(app) -> bool:
    task = getattr(app, "_slot_quit_task", None)
    return task is not None and not task.done()


def defer_exit(app, real_exit) -> bool:
    """True when this exit request was handled here (deferred behind a save, or a second quit)."""
    task = getattr(app, "_slot_quit_task", None)
    if task is not None and not task.done():
        app._slot_quit_skipped = True
        task.cancel()
        real_exit()
        return True
    if getattr(app, "_slot_quit_started", False):
        return False
    persist = for_backend(getattr(app, "backend", None))
    if persist is None or not persist.wants_save():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    app._slot_quit_started = True
    cancel = threading.Event()

    async def save_then_exit():
        try:
            said = await save_before_stop(app, app.backend, cancel)
            if said:
                try:
                    app.system_message(said)
                except Exception:  # noqa: BLE001 - the screen may already be going
                    pass
        finally:
            if not getattr(app, "_slot_quit_skipped", False):
                real_exit()

    try:
        app.system_message("Saving the conversation cache… (quit again or Ctrl+C to skip)")
    except Exception:  # noqa: BLE001 - no screen (headless): the save still runs
        pass
    app._slot_quit_task = loop.create_task(save_then_exit())
    return True


def strata_launch_args(settings, backend_name: str = "strata") -> list[str]:
    """`--slot-save-path <folder>` for serve/server.py when persistence is on (server.py:5638). A server
    flag, not a config key, so a launch with no option set still serves the install's own file. The
    path is absolute: the flag is otherwise read relative to the server's working directory."""
    probe = StrataSlots(type("B", (), {"name": backend_name, "_settings": settings, "_owned": None})())
    if probe.why_off() is not None:
        return []
    folder = probe.folder()
    ensure_folder(folder)
    return ["--slot-save-path", str(folder.resolve())]


def llama_launch_args(settings, flags=None) -> list[str]:
    """`--slot-save-path <folder>` for OUR router when persistence is on (and the build has the flag).
    `flags()` is the installed build's flag census, asked only when persistence is on."""
    if not getattr(settings, "slot_persist_llama", False):
        return []
    known = flags() if callable(flags) else frozenset()
    if known and "slot-save-path" not in known:
        _log("launch_skipped", "llama.cpp: this llama-server build has no --slot-save-path")
        return []
    folder = persist_root(settings) / "llama"
    ensure_folder(folder)
    return ["--slot-save-path", str(folder.resolve())]
