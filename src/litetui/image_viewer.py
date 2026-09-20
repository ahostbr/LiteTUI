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

A source that cannot be decoded never crashes the app: it paints a
"cannot render" line instead.
"""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Any, Mapping

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
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


def select_backend(env: Mapping[str, str] = os.environ) -> str:
    """The graphics backend from the ENVIRONMENT ONLY: ``"sixel"`` | ``"halfcell"``.

    No tty round-trip, deliberately: a live capability probe's terminal
    response is exactly the text that leaked into the Input box while Textual
    owned the TTY. Env is deterministic: Windows Terminal sets ``WT_SESSION``
    and has shipped sixel since v1.22 (real pixels); everything else gets the
    densest unicode mode (half-cell, 2 px/cell) rather than the probe-broken
    1-block-per-cell fallback.
    """
    if env.get("WT_SESSION"):
        return "sixel"
    return "halfcell"


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

    from textual_image._terminal import get_cell_size
    from textual_image import widget as ti_widget

    try:
        get_cell_size()  # seed the cache while we still own stdin
    except Exception:
        pass  # no tty at all: render path degrades to env/defaults, same as before

    _IMAGE_WIDGET_CLS = (
        ti_widget.SixelImage
        if select_backend() == "sixel"
        else ti_widget.HalfcellImage
    )
    return _IMAGE_WIDGET_CLS


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
    #iv-img { width: 100%; }

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
        meta = self._title + (f"  ({self._dims})" if self._dims else "")
        yield Static(meta, id="iv-meta", markup=False)
        with VerticalScroll(id="iv-scroll"):
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
        return cls(self._pil, id="iv-img")

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
