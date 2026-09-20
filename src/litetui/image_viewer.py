"""An in-sidebar image viewer: render a pasted (or staged) image as real pixels.

The ask (Ryan): pasting an image should paint it in the sidebar, not just attach
it. A terminal cannot paint pixels on its own — only a library that speaks a
graphics protocol can — so ``textual-image`` is the one new dependency. It picks
the best available protocol at import time (sixel / kitty's TGP where the
terminal has one, coloured half-cells otherwise, plain unicode when there is no
tty at all) and therefore always shows *something*. Pillow (already a
dependency) does the decode; this module only hosts the result in the same
sidebar the rest of the codebase already opens dialogs in.

A source that cannot be decoded never crashes the app: it paints a
"cannot render" line instead.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

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
        # Imported here, not at module import, for two reasons: the guard
        # (a missing textual-image degrades to the text line instead of an
        # ImportError at import time), and because its cell-size probe is best
        # run once Textual owns the terminal, not while the module loads.
        from textual_image.widget import Image as ImageWidget

        return ImageWidget(self._pil, id="iv-img")

    # ── the swap contract (carry the source so a pop to modal re-renders) ──
    def get_state(self) -> dict:
        return {"source": self._source, "title": self._title}

    def set_state(self, state: dict) -> None:
        self._source = state.get("source", self._source)
        self._title = state.get("title", self._title)

    # ── events ───────────────────────────────────────────────────────────
    def on_button_pressed(self, event: "Button.Pressed") -> None:
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
