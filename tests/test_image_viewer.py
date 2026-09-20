"""In-sidebar image viewer (image_viewer.py).

Covers the feature's contract end to end:
  * a pasted image's base64 decodes and renders (the happy path),
  * an undecodable source degrades to a "cannot render" line, never a crash,
  * the `image_viewer_enabled` setting (default ON) gates the AUTO-open, and
  * a paste with the setting ON docks a real SidePanel with the image body.

Mirrors the suite's idiom: the full app under `run_test` (the sidebar assertion
needs the real screen), `settle_until` for anything a mount/swap produces
(never a fixed frame count), and `LiteTUI()` with `_connect` stubbed so a
construction stays network-free. Images are generated in memory — nothing is
written to the checkout, so conftest's repo-root guard stays green.

Runner: `python -m pytest tests/test_image_viewer.py -q`  (NOT run_all.py).
"""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import settings as settings_mod
from litetui.image_viewer import ImageViewerBody
from litetui.side_panel import SidePanel
from textual.widgets import Button


def _png_bytes(width: int = 48, height: int = 32, rgb: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    """A real PNG, in memory. No file is ever written to the checkout."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), rgb).save(buf, format="PNG")
    return buf.getvalue()


def _png_b64(width: int = 48, height: int = 32) -> str:
    """The shape `pending_image` is stored in: base64-of-PNG-bytes."""
    return base64.b64encode(_png_bytes(width, height)).decode()


def _make_app():
    """A network-free app: constructed, `_connect` stubbed (suite idiom)."""
    application = app_mod.LiteTUI()
    application._connect = lambda: None
    return application


# ── the decode / degrade contract (no app needed) ─────────────────────────


def test_setting_defaults_on():
    # Default ON is the requirement — a fresh settings object previews.
    assert settings_mod.Settings().image_viewer_enabled is True


def test_bad_source_degrades_to_error():
    # A source that cannot be decoded must not raise out of the body; it paints
    # the "cannot render" line. This is asserted on the constructor, which is
    # where the decode is wrapped in try/except.
    body = ImageViewerBody(b"definitely not an image")
    assert body._error is not None
    assert body._pil is None


def test_valid_source_decodes():
    body = ImageViewerBody(_png_b64())
    assert body._error is None
    assert body._pil is not None
    assert body._dims is not None


# ── rendering under a real screen ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_image_paints_under_a_screen():
    """Mounting a body with a good image yields the image widget, no error."""
    application = _make_app()
    async with application.run_test(size=(120, 34)) as pilot:
        await application.screen.mount(ImageViewerBody(_png_b64()))
        # The image widget is composed into #iv-scroll; a decode failure would
        # have painted #iv-error instead. Both are checkable in the DOM.
        from tests._settle import settle_until

        has_img = await settle_until(
            pilot, lambda: bool(application.screen.query("#iv-img")))
        assert has_img, "a valid image should paint #iv-img"
        assert not application.screen.query("#iv-error")


@pytest.mark.asyncio
async def test_bad_image_paints_error_under_a_screen():
    """Mounting a body with a bad source paints the error line, no crash."""
    application = _make_app()
    async with application.run_test(size=(120, 34)) as pilot:
        await application.screen.mount(ImageViewerBody(b"not an image"))
        from tests._settle import settle_until

        has_err = await settle_until(
            pilot, lambda: bool(application.screen.query("#iv-error")))
        assert has_err, "a bad source should paint #iv-error"
        assert not application.screen.query("#iv-img")


# ── the auto-open gate (the actual feature) ───────────────────────────────


@pytest.mark.asyncio
async def test_paste_auto_opens_sidebar_when_on():
    """The feature: with the setting ON (default), pasting docks the sidebar."""
    application = _make_app()
    async with application.run_test(size=(140, 34)) as pilot:
        application.settings.image_viewer_enabled = True
        # Trigger the exact reactive the real paste drives.
        application.pending_image = _png_b64()

        from tests._settle import settle_until

        ok = await settle_until(
            pilot,
            lambda: bool(application.screen.query(SidePanel))
            and bool(application.screen.query(ImageViewerBody)),
            n=40,
        )
        assert ok, "a paste with image_viewer_enabled ON should mount the sidebar viewer"
        # The indicator (the pre-existing channel) is visible too.
        from textual.widgets import Static

        ind = application.screen.query_one("#image-indicator")
        assert "visible" in ind.classes


@pytest.mark.asyncio
async def test_paste_does_not_auto_open_sidebar_when_off():
    """Setting OFF: the paste still attaches, but nothing auto-opens."""
    application = _make_app()
    async with application.run_test(size=(140, 34)) as pilot:
        application.settings.image_viewer_enabled = False
        application.pending_image = _png_b64()

        # Give the loop a few frames; the sidebar must NOT appear.
        from tests._settle import settle_until

        appeared = await settle_until(
            pilot, lambda: bool(application.screen.query(SidePanel)), n=6)
        assert not appeared, "with the setting OFF a paste must not auto-open the sidebar"
        # ...and the image is still attached (the setting only gates the preview).
        assert application.pending_image is not None


@pytest.mark.asyncio
async def test_sidebar_forced_even_when_dialog_style_is_modal():
    """The viewer is a sidebar unconditionally (the ask), like the other
    sidebar dialogs — independent of the `dialog_style` setting."""
    application = _make_app()
    async with application.run_test(size=(140, 34)) as pilot:
        application.settings.dialog_style = "modal"  # the default is modal
        application.settings.image_viewer_enabled = True
        application.pending_image = _png_b64()

        from tests._settle import settle_until

        ok = await settle_until(
            pilot, lambda: bool(application.screen.query(SidePanel)), n=40)
        assert ok, "the viewer must dock to the sidebar even when dialog_style is modal"


# ── re-click a PAST image (goal #2, GO from Sentinel) ─────────────────────


def _spill_and_assert(application, tmp_path):
    """Set a convo dir, spill a real image, and assert a PNG landed there.

    Returns the spill path so a caller can reuse it for the click test.
    """
    convo = tmp_path / "convo"
    os.makedirs(convo, exist_ok=True)
    application.convo_dir = convo
    path = application._spill_image_for_reclick(_png_b64())
    assert path is not None, "a submit with a conversation dir should spill the image"
    assert os.path.exists(path), "the spilled file must exist on disk"
    with open(path, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n", "the spill must be a real PNG"
    return path


def test_spill_writes_png_into_convo_images():
    # A submit persists the attached image under <convo_dir>/images/ as a PNG,
    # so a past "[Image attached]" has a stable file to re-open.
    application = app_mod.LiteTUI()
    application._connect = lambda: None
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = _spill_and_assert(application, Path(tmp))
    assert os.path.basename(path).startswith("msg-")
    assert "/images/" in path.replace("\\", "/")


@pytest.mark.asyncio
async def test_reclick_affordance_reopens_viewer(tmp_path):
    # The "[Image attached]" bubble carries a clickable affordance; clicking it
    # re-opens the sidebar viewer for the spilled file.
    from tests._settle import settle_until
    from litetui.widgets import UserMessage

    application = _make_app()
    async with application.run_test(size=(140, 34)) as pilot:
        path = _spill_and_assert(application, tmp_path)
        application.screen.mount(UserMessage("[Image attached]", image_path=path))
        ok = await settle_until(
            pilot, lambda: bool(application.screen.query("#user-img-open")))
        assert ok, "a bubble with a spilled image must expose the open affordance"
        application.screen.query_one("#user-img-open", Button).press()
        ok = await settle_until(
            pilot,
            lambda: bool(application.screen.query(SidePanel))
            and bool(application.screen.query(ImageViewerBody)),
            n=40,
        )
        assert ok, "clicking the affordance must re-open the sidebar viewer"


# ── backend selection (the escape-leak fix, 2026-09-19) ─────────────────
#
# The bugs: textual-image's auto-detect runs LIVE stdin escape probes (DA1 /
# kitty TGP / CSI 16 t) at import and on first render. In-app, Textual owns
# stdin, so the terminal's REPLIES ([?61;4;6;7...c, [<35;33;27M) landed in the
# focused Input as typed text, and the image degraded to a mosaic. The fix:
# pick the backend from ENV only, and seed the cell-size cache pre-run.


def test_select_backend_windows_terminal_uses_sixel():
    # WT_SESSION is set by Windows Terminal (sixel since v1.22) -> real pixels.
    from litetui.image_viewer import select_backend

    assert select_backend({"WT_SESSION": "abc123"}) == "sixel"


def test_select_backend_without_wt_uses_halfcell():
    # No WT_SESSION -> the densest unicode mode (half-cell), never the
    # 1-block-per-cell fallback. Pure function: env dict, no tty round-trip.
    from litetui.image_viewer import select_backend

    assert select_backend({}) == "halfcell"
    assert (
        select_backend({"TERM_PROGRAM": "vscode", "TERM": "xterm-256color"})
        == "halfcell"
    )


def test_init_binds_env_selected_class_and_seeds_cell_cache(monkeypatch):
    """init_image_backend binds the env-selected EXPLICIT class (never the
    auto alias) and seeds the cell-size cache, so the render path can never
    fire a tty query while Textual owns stdin."""
    import litetui.image_viewer as iv
    from textual_image._terminal import get_cell_size

    monkeypatch.setenv("WT_SESSION", "test-session")
    saved = iv._IMAGE_WIDGET_CLS
    iv._IMAGE_WIDGET_CLS = None
    try:
        iv.init_image_backend()
        assert iv._IMAGE_WIDGET_CLS.__name__ == "Image"
        assert iv._IMAGE_WIDGET_CLS.__module__ == "textual_image.widget.sixel"
        assert getattr(get_cell_size, "_result", None) is not None, (
            "cell size must be cached pre-run; an uncached first render "
            "would probe the tty while Textual owns it")
    finally:
        iv._IMAGE_WIDGET_CLS = saved


def test_make_image_uses_env_selected_class(monkeypatch):
    """The composed widget is the env-selected class, with the #iv-img id."""
    import litetui.image_viewer as iv

    saved = iv._IMAGE_WIDGET_CLS
    iv._IMAGE_WIDGET_CLS = None
    try:
        monkeypatch.setenv("WT_SESSION", "test-session")
        body = ImageViewerBody(_png_b64())
        img = body._make_image()
        assert img.__class__.__module__ == "textual_image.widget.sixel"
        assert img.id == "iv-img"

        iv._IMAGE_WIDGET_CLS = None
        monkeypatch.delenv("WT_SESSION", raising=False)
        body = ImageViewerBody(_png_b64())
        img = body._make_image()
        assert img.__class__.__name__ == "HalfcellImage"
        assert img.id == "iv-img"
    finally:
        iv._IMAGE_WIDGET_CLS = saved
