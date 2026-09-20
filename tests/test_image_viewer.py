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


@pytest.fixture(autouse=True)
def _data_root_to_tmp(tmp_path, monkeypatch):
    """init_image_backend writes a startup diagnostic to <data_root>/.logs
    (the fallback path when the runtime-log sink is not installed yet) — keep
    it out of the checkout so conftest's repo-root guard stays green."""
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))


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
        monkeypatch.delenv("WT_PROFILE_ID", raising=False)  # WT sets both; a real WT shell leaks this
        body = ImageViewerBody(_png_b64())
        img = body._make_image()
        assert img.__class__.__name__ == "HalfcellImage"
        assert img.id == "iv-img"
    finally:
        iv._IMAGE_WIDGET_CLS = saved


# ── sixel follow-up (2026-09-19): visible tag, startup log, native size ──
#
# The escape-leak fix kept input clean, but the image stayed blurry: the
# render had been sized into the sidebar's cell box. Now: (1) the header shows
# the EXACT backend + class + env value the choice saw, (2) init logs what it
# saw to the error sink (never stdout) so WT_SESSION presence is measurable
# from outside the app, (3) sixel renders at native pixel size (near 1:1) and
# scrolls instead of downscaling, and (4) WT_PROFILE_ID is a second WT signal
# in case the launch chain strips WT_SESSION.


def test_select_backend_wt_profile_id_also_selects_sixel():
    # WT sets WT_SESSION AND WT_PROFILE_ID; if the launch chain strips one,
    # the other still says "Windows Terminal".
    from litetui.image_viewer import select_backend

    assert select_backend({"WT_PROFILE_ID": "profile-1"}) == "sixel"
    assert select_backend({"WT_SESSION": "s", "WT_PROFILE_ID": "p"}) == "sixel"
    # Neither set -> halfcell, as before.
    assert select_backend({"TERM_PROGRAM": "vscode"}) == "halfcell"


def test_init_logs_backend_diagnostics(monkeypatch, tmp_path):
    """init_image_backend logs what it saw (WT_SESSION value included) to the
    LiteTUI error file — the pre-run sink is not installed yet, so this must
    exercise the direct-append fallback — never stdout."""
    import litetui.image_viewer as iv
    from litetui import runtime_log

    # The autouse fixture already points LITETUI_DATA_ROOT at tmp_path.
    monkeypatch.setenv("WT_SESSION", "da63a2ce-0000-0000-0000-000000000000")
    monkeypatch.delenv("WT_PROFILE_ID", raising=False)
    monkeypatch.setattr(runtime_log, "_ACTIVE", None, raising=False)  # force the fallback
    saved = iv._IMAGE_WIDGET_CLS
    iv._IMAGE_WIDGET_CLS = None
    try:
        cls = iv.init_image_backend()
        assert cls.__module__ == "textual_image.widget.sixel"
        # The in-process state the header tag is built from.
        assert iv._BACKEND_INFO["backend"] == "sixel"
        assert iv._BACKEND_INFO["wt_session"].startswith("da63a2ce-")
        # ...and the on-disk line, where Sentinel reads it from outside.
        err = tmp_path / ".logs" / "runtime-errors.log"
        assert err.is_file(), "the startup diagnostic must land in .logs/runtime-errors.log"
        text = err.read_text(encoding="utf-8")
        assert "image_viewer.backend_init" in text
        assert "da63a2ce-0000-0000-0000-000000000000" in text
    finally:
        iv._IMAGE_WIDGET_CLS = saved


def test_backend_tag_names_class_and_env():
    """The header tag shows the backend, the exact class bound at init, and
    the WT env value — the guessing ends here."""
    import litetui.image_viewer as iv

    iv._BACKEND_INFO.update(
        backend="sixel", klass="SixelImage",
        wt_session="da63a2ce-0000-0000-0000-000000000000", wt_profile_id="",
        term_program="", term="",
    )
    try:
        tag = iv.backend_tag()
        assert "sixel" in tag and "SixelImage" in tag
        assert "WT_SESSION=da63a2ce-…" in tag

        iv._BACKEND_INFO.update(
            backend="halfcell", klass="HalfcellImage",
            wt_session="", wt_profile_id="", term_program="", term="",
        )
        tag = iv.backend_tag()
        assert "halfcell" in tag and "HalfcellImage" in tag
        assert "WT_SESSION=<empty>" in tag
    finally:
        iv._BACKEND_INFO.clear()


@pytest.mark.asyncio
async def test_header_shows_backend_tag_under_a_screen():
    """The composed #iv-meta line carries the [backend: …] tag."""
    application = _make_app()
    async with application.run_test(size=(120, 34)) as pilot:
        await application.screen.mount(ImageViewerBody(_png_b64()))
        from tests._settle import settle_until
        from textual.widgets import Static

        ok = await settle_until(pilot, lambda: bool(application.screen.query("#iv-meta")))
        assert ok, "the meta line must compose"
        meta = str(application.screen.query_one("#iv-meta", Static).render())
        assert "[backend:" in meta, f"the header must show the backend tag, got: {meta}"


def test_sixel_widget_keeps_native_size_halfcell_fits_width(monkeypatch):
    """Sixel must render at the image's native pixel size (near 1:1 — the
    scroll container moves), NOT be downscaled into the sidebar's cell box;
    half-cell, a 2 px/cell downsample, keeps the fit-to-width behaviour."""
    import litetui.image_viewer as iv

    saved = iv._IMAGE_WIDGET_CLS
    iv._IMAGE_WIDGET_CLS = None
    try:
        monkeypatch.setenv("WT_SESSION", "test-session")
        img = ImageViewerBody(_png_b64())._make_image()
        assert img.__class__.__module__ == "textual_image.widget.sixel"
        # Unrendered widgets expose the raw style value: a width style of
        # None means "native pixel size / real cell size" — the near-1:1 path.
        assert img.styles.width is None, (
            "sixel must NOT be squeezed to the panel width — that downscale "
            "is what made it look as blurry as half-cell")

        iv._IMAGE_WIDGET_CLS = None
        monkeypatch.delenv("WT_SESSION", raising=False)
        monkeypatch.delenv("WT_PROFILE_ID", raising=False)  # WT sets both; a real WT shell leaks this
        img = ImageViewerBody(_png_b64())._make_image()
        assert img.__class__.__name__ == "HalfcellImage"
        assert img.styles.width is not None, "half-cell still fits the panel width"
    finally:
        iv._IMAGE_WIDGET_CLS = saved


def test_da1_has_sixel_detects_attribute_4():
    """DA1 sixel detection: attribute 4 present -> sixel, absent -> not.

    The exact leak Ryan saw (`\x1b[?61;4;6;7;...c`) advertises sixel; a reply
    without a standalone 4 does not (and 40/14 must not false-positive)."""
    from litetui.image_viewer import _da1_has_sixel

    assert _da1_has_sixel("\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c") is True
    assert _da1_has_sixel("\x1b[?62;1;6;9;15;22c") is False   # no sixel
    assert _da1_has_sixel("\x1b[?40;14c") is False            # 40/14, not a bare 4
    assert _da1_has_sixel("\x1b[?4c") is True                 # sixel-only


def test_da1_tag_shows_probe_upgrade():
    """When env is stripped but the DA1 probe found sixel, the header tag says so."""
    import litetui.image_viewer as iv

    saved = dict(iv._BACKEND_INFO)
    try:
        iv._BACKEND_INFO.clear()
        iv._BACKEND_INFO.update(
            backend="sixel", klass="SixelImage",
            wt_session="", wt_profile_id="", da1_sixel="yes",
        )
        assert "DA1 sixel=yes" in iv.backend_tag()
        assert "[backend: sixel" in iv.backend_tag()
    finally:
        iv._BACKEND_INFO.clear()
        iv._BACKEND_INFO.update(saved)
