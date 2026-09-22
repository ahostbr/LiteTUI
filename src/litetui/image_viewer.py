"""An in-sidebar image viewer: render a pasted (or staged) image as real pixels.

The ask (Ryan): pasting an image should paint it in the sidebar, not just attach
it. A terminal cannot paint pixels on its own — only a library that speaks a
graphics protocol can — so ``textual-image`` is the one new dependency. Pillow
(already a dependency) does the decode; this module only hosts the result in
the same sidebar the rest of the codebase already opens dialogs in.

The render backend is NEVER chosen by textual-image's own auto-detect: that
runs a LIVE stdin escape probe (DA1 for sixel, kitty TGP for kitty, plus a
CSI 16 t cell-size query). Inside a running Textual app, Textual owns stdin,
so the terminal's RESPONSE bytes are delivered to Textual as input and land
in the focused Input as typed text (``[?61;4;6;7...c``, ``[<35;33;27M``).
Instead the backend is picked from the environment (Windows Terminal ->
sixel; else the densest unicode mode, half-cell) and the one remaining tty
query (cell size) is run once, pre-run, while we still own the terminal. See
``select_backend`` / ``init_image_backend``.

The choice is made VISIBLE and LOGGED so a blur can never be guessed at
again: the viewer header carries a ``[backend: sixel | SixelImage |
WT_SESSION=da63…]`` tag (the class actually bound at init plus the env value
``select_backend`` saw), and ``init_image_backend`` writes what it saw to the
LiteTUI error sink (``.logs/runtime-errors.log``) — never stdout.

A source that cannot be decoded never crashes the app: it paints a
"cannot render" line instead.
"""
from __future__ import annotations

import base64
import io
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer
from textual.widget import Widget
from textual.widgets import Button, Static

from litetui.side_panel import SwapButton, close_dialog

__all__ = ["ImageViewerBody", "open_image_viewer"]


def _to_pil(source: Any):
    """Decode an image source into a Pillow image, or raise.

    Accepts a Pillow image (returned as-is), raw image bytes, a base64 string
    (the shape ``pending_image`` is stored in), a filesystem path, or anything
    with ``read``. The caller wraps this in try/except; a decode failure is the
    "cannot render" line, not a crash.
    """
    from PIL import Image as PILImage

    # Already a Pillow image: leave it alone.
    if hasattr(source, "width") and hasattr(source, "height") and source.width > 0:
        return source

    if isinstance(source, bytes):
        data = source
    elif isinstance(source, str):
        # A path that exists wins; otherwise treat it as base64. The paste
        # channel stores b64-of-PNG-bytes, so this is the common case.
        p = Path(source)
        if p.is_file():
            with PILImage.open(p) as im:
                return im.copy()
        data = base64.b64decode(source)
    elif hasattr(source, "read"):
        data = source.read()
    else:
        raise TypeError(f"unsupported image source: {type(source).__name__}")

    with PILImage.open(io.BytesIO(data)) as im:
        # copy() gives an image that no longer references the (about-to-close)
        # open handle — the widget can keep painting it after the context exits.
        return im.copy()


# ── backend selection (no tty round-trip) ───────────────────────────────

_IMAGE_WIDGET_CLS: Any = None
#: What ``init_image_backend`` saw and chose — the viewer header's
#: ``[backend: …]`` tag is built from this, so what Ryan sees IS what was bound.
_BACKEND_INFO: dict[str, str] = {}


def select_backend(env: Mapping[str, str] = os.environ) -> str:
    """The graphics backend from the ENVIRONMENT ONLY: ``"sixel"`` | ``"halfcell"``.

    No tty round-trip, deliberately: a live capability probe's terminal
    response is exactly the text that leaked into the Input box while Textual
    owned the TTY. Env is deterministic: Windows Terminal sets ``WT_SESSION``
    (and ``WT_PROFILE_ID``) and has shipped sixel since v1.22 (real pixels);
    everything else gets the densest unicode mode (half-cell, 2 px/cell)
    rather than the probe-broken 1-block-per-cell fallback.

    Both WT variables are accepted: WT sets both, but if the launch chain
    (litetui.exe .venv shim) strips one, the other still says "this is
    Windows Terminal".
    """
    if env.get("WT_SESSION") or env.get("WT_PROFILE_ID"):
        return "sixel"
    return "halfcell"


def _da1_has_sixel(sequence: str) -> bool:
    """True if a DA1 response advertises sixel (attribute ``4``).

    A Primary Device Attributes reply is ``ESC [ ? n ; n ; ... c``; sixel
    capability is attribute ``4`` in that list. Windows Terminal reports it
    (the very leak Ryan saw was ``\\x1b[?61;4;6;7;...c`` — the ``4`` is there).
    """
    body = sequence
    if body.startswith("\x1b[?"):
        body = body[3:]
    if body.endswith("c"):
        body = body[:-1]
    return "4" in body.split(";")


def _probe_sixel_da1(timeout: float = 0.3) -> "bool | None":
    """Ask the terminal itself whether it supports sixel — LAUNCH-INDEPENDENT.

    The env fast-path (``select_backend``) misses when the ``litetui.exe`` .venv
    launcher strips ``WT_SESSION``/``WT_PROFILE_ID`` from the process (measured:
    the header tag then reads ``WT_SESSION=<empty>`` inside real Windows
    Terminal, so the image drops to half-cell and blurs). When env says
    "not sixel", ask the terminal directly with a DA1 query (``ESC[c``).

    MUST run pre-run, while we own stdin (the same window as the cell-size
    query): once Textual starts, its stdin thread grabs the response — that was
    the original Input-leak bug. Returns True/False, or None when there is no
    tty to probe (headless/log capture), which keeps the env choice.
    """
    out, in_ = sys.__stdout__, sys.__stdin__
    try:
        if not (out and in_ and out.isatty() and in_.isatty()):
            return None
        from textual_image._terminal import capture_terminal_response

        with capture_terminal_response("\x1b[?", "c", timeout) as resp:
            out.write("\x1b[c")
            out.flush()
        return _da1_has_sixel(resp.sequence)
    except Exception:
        return None  # unreadable / timeout / unexpected: keep the env choice


def init_image_backend() -> Any:
    """Bind the env-selected render widget class and seed textual-image's
    cell-size cache. Call ONCE before the Textual app takes the terminal
    (the entry points: ``app.main`` / ``cli main``); idempotent.

    The selection probes are never trusted (see ``select_backend``), but the
    CSI 16 t cell-size query still has to run while WE own stdin: on Windows
    its ioctl path always fails, so an unseeded first render would fire it
    in-app and leak the response the same way the DA1 probe did. textual-image
    caches the result on the function itself, so every later call from the
    render path hits the cache and never touches the tty. Headless / non-tty
    contexts degrade to env vars / built-in defaults without probing at all.

    Returns the bound widget class, or ``None`` if textual-image is missing
    (``open_image_viewer`` already degrades in that case).
    """
    global _IMAGE_WIDGET_CLS
    if _IMAGE_WIDGET_CLS is not None:
        return _IMAGE_WIDGET_CLS

    try:
        import textual_image  # noqa: F401 — same guard as open_image_viewer
    except Exception:
        return None

    from textual_image._terminal import CellSize, get_cell_size

    try:
        get_cell_size()  # seed the cache while we still own stdin
    except Exception:
        # Mouse/input escape sequences can interleave with the size reply.
        # The dependency's parser may raise ValueError instead of TerminalError.
        # Seed its documented fallback BEFORE importing widget: that import
        # calls get_cell_size itself, outside our guarded probe. Caching also
        # prevents a retry after Textual takes ownership of terminal input.
        width = os.environ.get("TEXTUAL_CELL_WIDTH", "")
        height = os.environ.get("TEXTUAL_CELL_HEIGHT", "")
        try:
            cell = CellSize(int(width), int(height))
            if cell.width <= 0 or cell.height <= 0:
                raise ValueError("non-positive cell size")
        except ValueError:
            cell = CellSize(10, 20)
        get_cell_size.__dict__["_result"] = cell

    from textual_image import widget as ti_widget

    backend = select_backend()
    # Env fast-path missed (WT vars stripped by the launcher)? Ask the terminal
    # itself — DA1 sixel probe, pre-run, while we still own stdin.
    da1 = ""
    if backend != "sixel":
        probed = _probe_sixel_da1()
        da1 = "yes" if probed else ("no" if probed is False else "n/a")
        if probed:
            backend = "sixel"
    _IMAGE_WIDGET_CLS = ti_widget.SixelImage if backend == "sixel" else ti_widget.HalfcellImage
    # WHAT THE CHOICE SAW — the header tag (``backend_tag``) and the error-sink
    # line below both read from this, never from env at render time.
    _BACKEND_INFO.update(
        backend=backend,
        klass=_IMAGE_WIDGET_CLS.__name__,
        wt_session=os.environ.get("WT_SESSION", ""),
        wt_profile_id=os.environ.get("WT_PROFILE_ID", ""),
        term_program=os.environ.get("TERM_PROGRAM", ""),
        term=os.environ.get("TERM", ""),
        da1_sixel=da1,
    )
    _log_backend_init()
    return _IMAGE_WIDGET_CLS


def _log_backend_init() -> None:
    """The startup diagnostic: what ``init_image_backend`` saw, to the LiteTUI
    error sink (``.logs/runtime-errors.log``), NEVER stdout.

    ``record_error`` is a no-op until the runtime-log plugin installs the sink
    — which happens at app mount, AFTER this pre-run init — so when the sink is
    not active yet the line is appended to the same error file directly. The
    line exists precisely to answer "was WT_SESSION set inside the process?"
    from outside the app (Sentinel reads it; the header tag shows it in-app).
    """
    info = _BACKEND_INFO
    if not info:
        return

    def _v(key: str) -> str:
        value = info.get(key, "")
        return value if value else "<empty>"

    detail = (
        f"backend={info['backend']} class={info['klass']} "
        f"WT_SESSION={_v('wt_session')} WT_PROFILE_ID={_v('wt_profile_id')} "
        f"DA1_sixel={_v('da1_sixel')} "
        f"TERM_PROGRAM={_v('term_program')} TERM={_v('term')}"
    )
    try:
        from litetui import runtime_log

        if runtime_log.record_error("image_viewer.backend_init", detail=detail):
            return
    except Exception:
        pass
    try:
        from datetime import datetime, timezone

        from litetui import paths

        path = paths.data_root() / ".logs" / "runtime-errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{stamp}\nevent=image_viewer.backend_init\n  {detail}\n\n")
    except Exception:
        # Diagnostics are best-effort: a full disk must never take down chat.
        pass


def _current_backend() -> str:
    """The backend in effect right now: the init-time snapshot, else derived
    from the class actually bound at init."""
    backend = _BACKEND_INFO.get("backend")
    if backend:
        return backend
    if _IMAGE_WIDGET_CLS is not None:
        return (
            "sixel"
            if _IMAGE_WIDGET_CLS.__module__ == "textual_image.widget.sixel"
            else "halfcell"
        )
    return "halfcell"  # nothing bound: the render path degrades anyway


def backend_tag() -> str:
    """The one-line ``[backend: …]`` header tag: the backend chosen, the exact
    class bound at init, and the WT env value ``select_backend`` saw. Empty if
    init has not run (the compose path always inits before building the meta
    line, so this is belt-and-braces)."""
    info = dict(_BACKEND_INFO)
    if not info:
        # Belt-and-braces: the tag is built from the class actually bound at
        # init (plus the live env) when the init-time snapshot is unavailable.
        if _IMAGE_WIDGET_CLS is None:
            return ""
        info = {
            "backend": _current_backend(),
            "klass": _IMAGE_WIDGET_CLS.__name__,
            "wt_session": os.environ.get("WT_SESSION", ""),
            "wt_profile_id": os.environ.get("WT_PROFILE_ID", ""),
        }
    if info.get("wt_session"):
        env = f"WT_SESSION={info['wt_session'][:9]}…"
    elif info.get("wt_profile_id"):
        env = f"WT_PROFILE_ID={info['wt_profile_id'][:9]}…"
    elif info.get("da1_sixel") == "yes":
        env = "WT_SESSION=<empty>, DA1 sixel=yes"
    else:
        env = "WT_SESSION=<empty>"
    return f"[backend: {info['backend']} | {info['klass']} | {env}]"


class ImageViewerBody(Widget):
    """An image that fits a sidebar: a one-line caption, the pixels below.

    No ModalScreen assumptions — the same contract every dialog body has, so it
    docks to the sidebar (the ask) and the ``SwapButton`` pops it to a modal.
    """

    DEFAULT_CSS = """
    ImageViewerBody { height: 100%; layout: vertical; }

    #iv-meta { height: auto; color: $text-muted; margin: 0 0 1 0;
               overflow: hidden; text-overflow: ellipsis; }

    #iv-scroll { height: 1fr; padding: 0 1; border: solid $foreground 15%;
                 align: left top; }

    #iv-error { height: auto; color: $error; margin: 1 0; }

    #iv-close { height: auto; margin: 1 0 1 0; }
    #iv-close Button { width: 1fr; }
    """

    def __init__(self, source: Any, *, title: str = "Image") -> None:
        super().__init__()
        self._source = source
        self._title = title
        self._pil = None
        self._dims: str | None = None
        self._error: str | None = None
        # Decode up front, inside the body, so a bad image degrades to the
        # "cannot render" line rather than blowing up mid-compose.
        try:
            self._pil = _to_pil(source)
            self._dims = f"{self._pil.width}×{self._pil.height}"
        except Exception as e:  # noqa: BLE001 — a bad image must degrade, not crash
            self._pil = None
            self._error = f"{type(e).__name__}: {e}"

    # ── composition ──────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        init_image_backend()  # idempotent; the tag below must reflect a real choice
        tag = backend_tag()
        meta = self._title + (f"  ({self._dims})" if self._dims else "")
        if tag:
            meta += "  " + tag
        yield Static(meta, id="iv-meta", markup=False)
        # BOTH-axis scroll: sixel renders at the image's native pixel size
        # (near 1:1), which can exceed the sidebar's cell box.
        with ScrollableContainer(id="iv-scroll"):
            if self._error is not None:
                yield Static(
                    f"Cannot render image — {self._error}",
                    id="iv-error", markup=False,
                )
            else:
                yield self._make_image()
        with Horizontal(id="iv-close"):
            yield Button("Close", variant="primary", id="iv-close")
        yield SwapButton()

    def _make_image(self):
        # NEVER the auto ``textual_image.widget.Image`` alias: importing its
        # package re-runs the live stdin capability probes at compose time —
        # in-app, where Textual owns the TTY (the Input-box leak). The
        # explicit SixelImage / HalfcellImage classes carry no selection
        # probe, and ``init_image_backend`` (idempotent; the entry points
        # call it pre-run) has already seeded the cell-size cache, so the
        # render path touches the tty never.
        cls = init_image_backend()
        if cls is None:
            # Unreachable: open_image_viewer already degraded for a missing
            # textual-image before this body was composed.
            raise RuntimeError("textual-image is not installed")
        img = cls(self._pil, id="iv-img")
        if _current_backend() != "sixel":
            # HALFCELL is a 2 px/cell downsample, so fit the panel width (the
            # pre-sixel behaviour). SIXEL deliberately keeps its width style
            # auto: textual-image then renders the source at its own dimensions
            # (cells derived from the real cell size, near 1:1) and #iv-scroll
            # scrolls — downscaling into the sidebar's cell box is exactly what
            # made the sixel look as blurry as half-cell.
            img.styles.width = "100%"
        return img

    # ── the swap contract (carry the source so a pop to modal re-renders) ──
    def get_state(self) -> dict:
        return {"source": self._source, "title": self._title}

    def set_state(self, state: dict) -> None:
        self._source = state.get("source", self._source)
        self._title = state.get("title", self._title)

    # ── events ───────────────────────────────────────────────────────────
    def on_button_pressed(self, event: "Button.Pressed") -> None:
        # No terminal-mode restore needed on close: the sixel / half-cell
        # render paths emit only graphics/printable segments — they enable
        # no mouse tracking or DECSET. Textual owns mouse reporting itself
        # (and resets it at app exit), and the one pre-run raw-input capture
        # restores the console mode in its own finally. Input stays clean.
        if event.button.id == "iv-close":
            close_dialog(self, None)


def open_image_viewer(app, source: Any, *, title: str = "Image", on_close=None) -> bool:
    """Open the sidebar image viewer. ``style="sidebar"`` is forced, exactly
    like the dialogs that already dock there: the image goes to the side panel
    regardless of the ``dialog_style`` setting.

    ``on_close`` (the dialog answer callback) fires when the viewer is
    dismissed. Returns ``True`` if the viewer opened, ``False`` if
    ``textual-image`` is missing and it degraded to a notice instead.
    """
    try:
        import textual_image  # noqa: F401 — the guard: one new dep, absent -> degrade
    except Exception:
        app.notify(
            "textual-image is not installed (pip install textual-image); "
            "the image is still attached, it just won't preview.",
            severity="warning", timeout=5,
        )
        return False

    from functools import partial
    from litetui.side_panel import open_dialog

    open_dialog(app, partial(ImageViewerBody, source, title=title), on_close,
                style="sidebar")
    return True
