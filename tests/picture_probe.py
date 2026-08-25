"""AN INSTRUMENT, NOT A GATE. A human runs this; the suite never does.

    python -m pytest -q -s tests/picture_probe.py

🔴 WHY IT EXISTS. On 2026-08-24 Ryan hit a P1 — modals off-centre and unstyled,
the sidebar a floating box in the top-right, rows clipped mid-word — with **1362
tests green**. Every one of them asserted BEHAVIOUR (focus, resolution,
teardown, state carry). None asserted that anything RENDERS. A geometry gate was
then written for exactly that class, and **72 tests including that new gate
passed over a swap button whose label does not paint**, because the gate asserts
the BOX and the PANEL and nothing asserts that a child at the BOTTOM of the box
is fully inside it. **A container can be the right size and still clip its last
child.**

This file is what found that. It prints `region` for the interesting widgets and
reconstructs the screen as readable text, so a human can LOOK.

📌 IT IS DELIBERATELY NOT NAMED `test_*.py`. `tests/run_all.py` globs
`test_*.py` (see its `TESTS.glob`) and pytest's default collection does the
same, so this is invisible to both — but `pytest <this path>` collects the
`test_*` functions inside it, because an explicitly-passed file is still
scanned. That is the whole trick: it runs under pytest so `conftest.py` applies,
without becoming a gate.
⇒ The GATE for containment lives in `tests/test_dialog_geometry.py`. The probe
is what you reach for when something LOOKS wrong; the assertion is what runs
when nobody is looking. You need both — commit only the probe and the defect can
come back silently; keep only the assertion and you lose the instrument the next
class of rendering defect will need.

═══════════════════════════════════════════════════════════════════════════════
⚠️  CALIBRATION — READ BEFORE TRUSTING A ROW NUMBER. This is most of the value.
═══════════════════════════════════════════════════════════════════════════════

1. **THE RECONSTRUCTED LINE INDEX IS OFFSET +1 FROM `region.y`.** Measured
   against a case known to render correctly: the sidebar swap button at region
   y=12 painted rows 13/14/15 (border, label, border).
   ⚠️ THAT REFERENCE IS ON BRANCH `feature/swap-button`, NOT ON `main` — the
   button is not wired into the dialogs here yet, so you cannot reproduce that
   exact calibration from this tree. Re-derive it against any widget you can
   already see rendering correctly; do not assume +1 still holds because this
   comment says so. It is UNPROVEN for any widget other than the one measured,
   and uncalibrated this probe names the WRONG element.

2. **DO NOT `grep` A PHRASE ON THE SVG. IT RETURNS 0 EVEN WHEN THE TEXT PAINTS.**
   Textual emits one `<text>` element per CHARACTER, so no label ever appears as
   a contiguous string. `grep "Open as modal"` returns 0 for the host where it
   RENDERS and for the host where it does not — it cannot distinguish them. The
   reconstruction below is what can.

3. **ASSERT ON `region`, NEVER `outer_size`.** They disagree, and only `region`
   renders. (OpenBolt's catch, fixed in 9380225.)

4. **RUN IT UNDER PYTEST, NOT AS A SCRIPT.** As a standalone script this HUNG,
   and a sibling probe that did run standalone photographed the app's FIRST-BOOT
   ENGINE PICKER sitting over the sidebar and reported a CSS bug that did not
   exist — `tests/conftest.py` never applied, so nothing pinned the environment.
   **When a probe and a gate disagree, suspect the probe: it is the one with no
   fixtures.** If you must go standalone, set `LITETUI_NO_HARNESS=1` and
   `LITETUI_BACKEND=lmstudio` yourself.

5. **IN A SIDEBAR, QUERY FROM A SCOPE, NEVER GLOBALLY.** The chat and the dialog
   share one screen, so `screen.query_one("#picker-box")` can match a different
   dialog's box entirely.
"""
from __future__ import annotations

import re

import pytest

from litetui import app as m
from litetui.picker import PickerBody, PickerScreen
from litetui.side_panel import DialogController, SidePanel, SwapButton

from _settle import settle_until

ROWS = [("m-a", "LiteTUI ea9f1  ·  qwen3-30b"), ("m-b", "LiteTUI b8c2  ·  devstral")]


def svg_to_text(svg: str) -> str:
    """Reconstruct the screen from the SVG: group <text> by y, sort by x.

    THIS IS THE EYEBALL. The regions printed alongside are the measurement; this
    is what lets a human see a box jammed in a corner or a row sliced mid-word.
    See calibration note 2 on why grepping the SVG cannot do this.
    """
    rows: dict[int, list[tuple[float, str]]] = {}
    for mt in re.finditer(r'<text[^>]*\bx="([\d.]+)"[^>]*\by="([\d.]+)"[^>]*>(.*?)</text>',
                          svg, re.S):
        x, y, txt = float(mt.group(1)), round(float(mt.group(2))), mt.group(3)
        txt = re.sub(r"<[^>]+>", "", txt)
        txt = (txt.replace("&#160;", " ").replace("&nbsp;", " ")
                  .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
        rows.setdefault(y, []).append((x, txt))
    return "\n".join(
        "".join(t for _x, t in sorted(rows[y])).rstrip() for y in sorted(rows)
    )


def _subject(scope, box):
    """The widget this probe reports on: the box's LAST CHILD.

    🔴 NOT a named widget. An instrument that needs one specific control only
    works on the tree that has it — this probe originally queried `SwapButton`
    and failed outright on `main`, where that button is not wired in yet. The
    LAST CHILD is the general form of the question it exists to answer: what
    sits at the bottom edge, and does the container actually contain it.
    """
    kids = [w for w in box.children]
    if not kids:
        return box
    swaps = list(scope.query(SwapButton))
    return swaps[0] if swaps else kids[-1]


def report(tag: str, screen, target) -> None:
    r = target.region                      # region, NEVER outer_size — note 3
    s = screen.size
    print(f"  {tag:22} region x={r.x:<4} y={r.y:<4} w={r.width:<4} h={r.height:<4}"
          f"   screen {s.width}x{s.height}")


async def shoot(style: str) -> None:
    a = m.LiteTUI()
    a.available_models = ["m-a"]
    a.model_id = "m-a"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.settings.dialog_style = style
    async with a.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        print(f"\n=== /model as {style.upper()} ===")
        if style == "sidebar":
            ctrl = DialogController(a, lambda: PickerBody("Choose a model", ROWS),
                                    "sidebar", "right")
            a.run_worker(ctrl.open(), name="dlg")
            await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
            panel = a.screen.query_one(SidePanel)
            # SCOPED through the panel — calibration note 5.
            box = panel.query_one("#picker-box")
            btn = _subject(panel, box)
            report("SidePanel", a.screen, panel)
            report("#picker-box", a.screen, box)
            full = panel.region.height == a.screen.size.height
            print(f"  full height? {full}   (the floating-box symptom is False here)")
        else:
            a.push_screen(PickerScreen("Choose a model", ROWS))
            await settle_until(pilot, lambda: bool(a.screen.query(PickerBody)))
            body = a.screen.query_one(PickerBody)
            box = a.screen.query_one("#picker-box")
            btn = _subject(a.screen, box)
            report("PickerBody", a.screen, body)
            report("#picker-box", a.screen, box)
            br, s = body.region, a.screen.size
            print(f"  centred? left={br.x} right={s.width - (br.x + br.width)}"
                  f"   (equal within 1 = centred)")
        report("SUBJECT", a.screen, btn)
        print(f"  SUBJECT {type(btn).__name__} label={str(getattr(btn, 'label', ''))!r}")
        # THE CONTAINMENT QUESTION, printed rather than assumed: does the box's
        # LAST CHILD actually fit inside it? A container can be the right size
        # and still clip what sits at its bottom edge.
        br, cr = btn.region, box.region
        fits = (br.y + br.height) <= (cr.y + cr.height)
        print(f"  last child bottom={br.y + br.height}  box bottom={cr.y + cr.height}"
              f"  FITS={fits}")

        lines = svg_to_text(a.export_screenshot()).splitlines()
        br = btn.region
        # +1 OFFSET, and a window either side so a miscalibration is VISIBLE
        # rather than silently shifting which element you are reading — note 1.
        lo, hi = max(0, br.y - 2), min(br.y + br.height + 3, len(lines))
        print(f"  ---- rows around the button (region.y={br.y}, index is +1) ----")
        for i in range(lo, hi):
            print(f"  {i:>3}|{lines[i]}")


@pytest.mark.asyncio
async def test_picture_sidebar() -> None:
    """Known-good reference. Calibrate the +1 offset against THIS one."""
    await shoot("sidebar")


@pytest.mark.asyncio
async def test_picture_modal() -> None:
    """The default host, and the one that clipped the swap button's label."""
    await shoot("modal")
