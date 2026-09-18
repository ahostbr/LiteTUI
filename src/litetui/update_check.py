"""The update check — ported from the LiteSuite desktop updater.

The desktop app (LiteSuite `apps/desktop/src/litesuite/services/updater.ts`)
drives electron-updater with a full state machine: check, download, progress
percent, explicit install, relaunch-on-failed-install. This module is the same
DESIGN, ported to what a pip-installed CLI can actually do:

    LiteSuite desktop                        LiteTUI (this module)
    ──────────────────────────────────────── ─────────────────────────────────
    feed: litesuite.dev/api/updates/latest   feed: pypi.org/pypi/litetui/json
    .yml → 302 to the GitHub CDN (the repo   (one JSON document, `info.version`
    is private, so the site proxies it)      is the release channel — the
                                             ls-release-litesuite pipeline ends
                                             in `twine upload dist/*`)
    15 s delay after launch                  STARTUP_DELAY_SECONDS = 15
    4 h poll interval in a long-lived        CHECK_TTL_SECONDS = 4 h, as a cache
    process                                  TTL — CLI launches are frequent
                                             and short, so "poll every 4 h"
                                             becomes "don't fetch again for 4 h"
    LITESUITE_DISABLE_AUTO_UPDATE=1          LITETUI_DISABLE_UPDATE_CHECK=1
    dev/unpackaged build ⇒ off               running from a git checkout ⇒ off
    pre-downloads, EXPLICIT install          notify + the exact pip command
    downloading / percent states             N/A — nothing downloads in-process
    app.relaunch() on failed install         N/A — the user's relaunch is it

The install is deliberately NOT automatic, for the same reason the desktop app
keeps `autoInstallOnAppQuit` off (updater.ts): auto-retrying a failed install
re-enters the same failure and loops. The user runs the command; that never
loops.

Failure is silent by design. A dead network, a 404, a JSON hiccup must never
surface: the worst outcome of a broken check is that nobody hears about a new
version. The desktop app logs to a console nobody reads; this one returns
`{"status": "error"}` and moves on.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

from litetui import paths
from litetui.version import __version__

#: The release channel. Public, no auth, and one document answers the question
#: "what is the newest litetui" — `info.version` is the newest stable release.
PYPI_URL = "https://pypi.org/pypi/litetui/json"

#: Ported straight from updater.ts (AUTO_UPDATE_STARTUP_DELAY_MS = 15_000):
#: the first check lands after the UI has settled and the user has stopped
#: staring at the splash.
STARTUP_DELAY_SECONDS = 15.0

#: Ported from AUTO_UPDATE_POLL_INTERVAL_MS (4 h). The desktop app polls a
#: long-lived process; a CLI cannot hold a timer across sessions, so the
#: interval becomes a cache TTL: a fetch younger than this suppresses the next
#: launch's fetch entirely.
CHECK_TTL_SECONDS = 4 * 60 * 60

FETCH_TIMEOUT_SECONDS = 5.0

#: Sits beside settings.json in data_root(), so the LITETUI_DATA_ROOT
#: override (tests, isolation) moves it too.
CACHE_FILENAME = "litetui-update-check.json"

_DISABLE_ENV = "LITETUI_DISABLE_UPDATE_CHECK"
_FORCE_ENV = "LITETUI_FORCE_UPDATE_CHECK"
_USER_AGENT = f"litetui-update-check/{__version__}"

_started = False

# ── Version ordering ─────────────────────────────────────────────────────────

_VERSION_CORE = re.compile(r"^\s*(\d+(?:\.\d+)*)")


def version_key(v: str) -> tuple:
    """A sort key for X.Y.Z versions — just enough, and only just.

    Numeric per segment, so 0.10.0 > 0.9.0 (a lexicographic compare gets that
    wrong and ships a "newer available" lie every minor bump past 9). A
    release sorts above its own prerelease (0.24.0 > 0.24.0.dev1). Full PEP 440
    ordering between two prereleases is out of scope: PyPI's `info.version`
    is the newest STABLE release, and the only ordering this feature needs is
    "is what they just published newer than what I am running".
    """
    m = _VERSION_CORE.match(v or "")
    if not m:
        return ((0, 0, 0), 0)  # unparseable sorts below everything
    core = [int(x) for x in m.group(1).split(".")]
    while len(core) < 3:
        core.append(0)
    prerelease = (v or "")[m.end():].lstrip(".-")
    return (tuple(core), 0 if prerelease else 1)


def is_newer(candidate: str, current: str) -> bool:
    """True when `candidate` is a release worth telling the user about."""
    return version_key(candidate) > version_key(current)


# ── Opt-outs, ported from getAutoUpdateDisabledReason() ─────────────────────

def _is_dev_tree() -> bool:
    """True when running from a git checkout (source / editable install).

    paths.ROOT is the repo root — three parents up from this file — and only
    a checkout has a `.git` there; an installed wheel does not. This is the
    port of the desktop updater's `isDevelopment / !isPackaged` gate
    ("Automatic updates are only available in packaged production builds"):
    a dev box is where versions come FROM, so asking it about newer versions
    is a question to a mirror.
    """
    return (paths.ROOT / ".git").exists()


def disabled_reason() -> str | None:
    """A reason string means OFF; None means ON (updater.ts shape).

    LITETUI_FORCE_UPDATE_CHECK=1 overrides the dev-tree gate only — it exists
    so the notice path can be exercised on a checkout. It never overrides the
    user's own opt-out.
    """
    if os.environ.get(_DISABLE_ENV) == "1":
        return f"automatic updates are disabled by {_DISABLE_ENV}=1"
    if _is_dev_tree() and os.environ.get(_FORCE_ENV) != "1":
        return "running from a source checkout"
    return None


# ── Cache ────────────────────────────────────────────────────────────────────

def _cache_path() -> Path:
    return paths.data_root() / CACHE_FILENAME


def _read_cache() -> dict | None:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "checked_at" in raw:
            return raw
    except (OSError, ValueError):
        pass  # a missing/corrupt cache is not worth surfacing (updater-prefs.ts)
    return None


def _write_cache(latest: str, outcome: str, current: str) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "latest": latest,
                    "outcome": outcome,
                    "current": current,
                    "checked_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # a failed write just means the next launch re-fetches


# ── The check itself ─────────────────────────────────────────────────────────

def fetch_latest_version(timeout: float = FETCH_TIMEOUT_SECONDS) -> str:
    """The newest litetui on PyPI, or raise. Callers decide what a failure
    means; the background runner swallows it (see the module docstring)."""
    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    version = payload["info"]["version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"PyPI response had no usable version: {payload.get('info')!r}")
    return version.strip()


def maybe_check(current: str | None = None, *, now: float | None = None) -> dict:
    """One check, through the TTL gate. Returns a status dict:

        {"status": "disabled"}                      — an opt-out applies
        {"status": "up-to-date"|"available", ...}   — the outcome
        {"status": "error", "error": ...}           — the fetch failed

    A fresh cache (younger than CHECK_TTL_SECONDS, and for the SAME running
    version) suppresses the network call entirely — that IS the port of the
    4-hour poll interval. The version matters: a user who just upgraded must
    not be told an update is available that they are now ON.
    """
    reason = disabled_reason()
    if reason is not None:
        return {"status": "disabled", "reason": reason}

    current = current if current is not None else __version__
    forced = os.environ.get(_FORCE_ENV) == "1"
    if not forced:
        cache = _read_cache()
        if cache is not None and cache.get("current") == current:
            age = (now if now is not None else time.time()) - float(cache.get("checked_at", 0.0))
            if 0 <= age < CHECK_TTL_SECONDS:
                return {
                    "status": cache.get("outcome", "up-to-date"),
                    "latest": cache.get("latest"),
                    "cached": True,
                }

    try:
        latest = fetch_latest_version()
    except Exception as exc:  # noqa: BLE001 — every failure class is silent by design
        return {"status": "error", "error": str(exc)}

    outcome = "available" if is_newer(latest, current) else "up-to-date"
    _write_cache(latest, outcome, current)
    return {"status": outcome, "latest": latest}


# ── The background runner, ported from initializeAutoUpdater() ──────────────

def start_background_check(app) -> None:
    """Called from on_mount. One check, 15 s after launch, on a daemon thread.

    The port of initializeAutoUpdater()'s scheduling half: the delay, the
    "check in flight" guard (_started), and delivery to the UI on the app's
    own thread (call_from_thread is the port of the UPDATE_STATUS_CHANNEL
    broadcast). A session shorter than the delay simply never checks — the
    daemon thread dies with the process, which is the correct behaviour, not
    a leak: no timer, no handle to clean up.
    """
    global _started
    if _started:
        return
    _started = True

    def _run() -> None:
        time.sleep(STARTUP_DELAY_SECONDS)
        if disabled_reason() is not None:
            return
        try:
            result = maybe_check()
        except Exception:  # noqa: BLE001 — a check that raises must not kill the thread
            return
        if result.get("status") == "available":
            try:
                app.call_from_thread(app._update_available_notice, result.get("latest") or "?")
            except Exception:
                pass  # the app is gone (the session ended inside the delay) — tell no one

    threading.Thread(target=_run, name="litetui-update-check", daemon=True).start()
