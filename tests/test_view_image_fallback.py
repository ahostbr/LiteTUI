"""T1319: view_image for a model that cannot see.

A text-only model (model_type "llm") used to get no view_image at all. It is now
offered the tool and the result is a fast text report of the picture
(litetui.image_describe); a vision model keeps the staging path untouched.

Two groups: the routing (offer, vision path unchanged) and the report itself, on
synthetic Pillow images whose geometry and colours are known, so every number is
asserted against the truth rather than against a previous run.
"""
import os
import re
import time

import pytest
from PIL import Image, ImageDraw
from test_owner_resume import build as _build

from litetui import image_describe as d

build = _build

WHITE = (255, 255, 255)


def _save(tmp_path, im, name):
    p = tmp_path / name
    im.save(p)
    return str(p)


def _rect(tmp_path, name="rect.png", size=(400, 300), box=(100, 50, 199, 149), fill=(255, 0, 0), bg=WHITE):
    im = Image.new("RGB", size, bg)
    ImageDraw.Draw(im).rectangle(box, fill=fill)
    return _save(tmp_path, im, name)


def _report(path, **kw):
    return d.describe_tool({"path": path, **kw})


# ── routing ───────────────────────────────────────────────────────────────────

def _offered(a) -> list[str]:
    return [s["function"]["name"] for s in a.plugins.tool_specs()]


@pytest.mark.parametrize("model_type", ["llm", "vlm", None])
def test_view_image_is_offered_to_every_model_type(build, model_type):
    a = build(None)
    a.model_type = model_type
    assert "view_image" in _offered(a)


def test_a_model_without_vision_gets_the_report_and_nothing_is_staged(build, tmp_path):
    a = build(None)
    a.model_type = "llm"
    out = a.plugins.dispatch_for("view_image")({"path": _rect(tmp_path)})
    assert "bounding box x 100..199, y 50..149" in out
    assert a._pending_tool_images == [], "pixels must not be staged for a model that cannot take them"


def test_a_vision_model_keeps_the_staging_path_unchanged(build, tmp_path):
    a = build(None)
    a.model_type = "vlm"
    p = _rect(tmp_path)
    out = a.plugins.dispatch_for("view_image")({"path": p})
    assert out.startswith("Attached rect.png (") and "It is in the next message" in out
    assert [x[0] for x in a._pending_tool_images] == [p]
    assert out == a._tool_view_image({"path": p}), "the plugin hands the call to app._tool_view_image as before"


def test_an_unknown_model_type_keeps_the_staging_path(build, tmp_path):
    a = build(None)
    a.model_type = None
    out = a.plugins.dispatch_for("view_image")({"path": _rect(tmp_path)})
    assert out.startswith("Attached rect.png (")


def test_the_choice_is_read_per_call_so_a_model_switch_flips_it(build, tmp_path):
    a = build(None)
    run = a.plugins.dispatch_for("view_image")
    p = _rect(tmp_path)
    a.model_type = "llm"
    assert "bounding box" in run({"path": p})
    a.model_type = "vlm"
    assert run({"path": p}).startswith("Attached")


def test_compare_to_is_ignored_on_the_vision_path(build, tmp_path):
    a = build(None)
    a.model_type = "vlm"
    out = a.plugins.dispatch_for("view_image")({"path": _rect(tmp_path), "compare_to": _rect(tmp_path, "b.png")})
    assert out.startswith("Attached rect.png (") and len(a._pending_tool_images) == 1


def test_the_schema_declares_compare_to_as_optional():
    from litetui.plugins.view_image import VIEW_IMAGE_TOOL_SPEC as spec
    params = spec["function"]["parameters"]
    assert set(params["properties"]) == {"path", "compare_to"}
    assert params["required"] == ["path"]


# ── the report: numbers against known geometry ───────────────────────────────

def test_report_states_size_background_and_foreground_box(tmp_path):
    out = _report(_rect(tmp_path))
    assert "rect.png: 400x300 px, PNG, RGB" in out
    assert "Background (4 px border median): white #ffffff, uniform" in out
    assert "Foreground (not background): 8.3% of pixels, bounding box x 100..199, y 50..149 (100x100 px)" in out
    assert "red #ff0000" in out, "the rectangle's colour is named and given in hex"


def test_report_width_per_row_band_matches_the_rectangle(tmp_path):
    out = _report(_rect(tmp_path))
    assert len(re.findall(r"^  y \d+\.\.\d+: ", out, re.M)) == d.BANDS
    bands = re.findall(r"y (\d+)\.\.(\d+): x (\d+)\.\.(\d+), width (\d+), filled (\d+)", out)
    inside = [b for b in bands if int(b[0]) >= 56 and int(b[1]) <= 150]
    assert inside, "some bands lie wholly inside the rectangle"
    for _, _, left, right, width, filled in inside:
        assert (int(left), int(right), int(width), int(filled)) == (100, 199, 100, 100)
    assert any("empty" in ln for ln in out.splitlines()), "bands above and below the rectangle are empty"


def test_report_describes_a_narrowing_in_numbers(tmp_path):
    im = Image.new("RGB", (400, 400), WHITE)
    # symmetric about x=200: 300 wide at the top, 100 wide at the bottom
    ImageDraw.Draw(im).polygon([(50, 0), (350, 0), (250, 399), (150, 399)], fill=(30, 30, 30))
    out = _report(_save(tmp_path, im, "taper.png"))
    m = re.search(r"extent narrows from (\d+) px \(y=(\d+)\) to (\d+) px \(y=(\d+)\)", out)
    assert m, out
    assert int(m[1]) > 250 and int(m[3]) < 130 and int(m[2]) < int(m[4])


def test_report_places_bright_and_dark_regions(tmp_path):
    im = Image.new("RGB", (640, 640), (128, 128, 128))
    dr = ImageDraw.Draw(im)
    dr.rectangle((100, 100, 259, 259), fill=(0, 0, 0))        # dark square
    dr.rectangle((400, 380, 559, 539), fill=(255, 255, 255))  # bright square
    out = _report(_save(tmp_path, im, "regions.png"))
    dark = re.search(r"Dark regions.*?\n  x (\d+)\.\.(\d+), y (\d+)\.\.(\d+)", out)
    bright = re.search(r"Bright regions.*?\n  x (\d+)\.\.(\d+), y (\d+)\.\.(\d+)", out)
    assert dark and bright, out
    cell = 640 / d.CELLS
    for got, want in ((dark, (100, 260, 100, 260)), (bright, (400, 560, 380, 540))):
        for g, w in zip(got.groups(), want):
            assert abs(int(g) - w) <= cell, (got.groups(), want)


def test_a_noisy_border_is_not_a_background(tmp_path):
    im = Image.new("RGB", (400, 400))
    px = im.load()
    for x in range(400):                         # a hard horizontal gradient: no single border colour
        for y in range(400):
            px[x, y] = (x * 255 // 399, 40, 255 - x * 255 // 399)
    out = _report(_save(tmp_path, im, "grad.png"))
    assert "Border is not one colour" in out and "no silhouette" in out
    assert "Width per row band" not in out
    assert "Dominant colours overall" in out, "the colour report still stands without a silhouette"


def test_a_subject_touching_the_border_still_finds_the_background(tmp_path):
    out = _report(_rect(tmp_path, size=(400, 300), box=(150, 0, 249, 299)))   # touches top and bottom
    assert "white #ffffff, uniform" in out and "another colour (something touches the edge)" in out
    assert "bounding box x 150..249, y 0..299" in out


def test_transparency_is_flattened_onto_white_and_said_so(tmp_path):
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle((50, 50, 149, 149), fill=(0, 0, 255, 255))
    out = _report(_save(tmp_path, im, "alpha.png"))
    assert "75% transparent, flattened onto white" in out
    assert "bounding box x 50..149, y 50..149" in out


def test_a_large_jpeg_reports_original_pixel_coordinates(tmp_path):
    im = Image.new("RGB", (2400, 1600), WHITE)
    ImageDraw.Draw(im).rectangle((600, 400, 1799, 1199), fill=(0, 120, 0))
    out = _report(_save(tmp_path, im, "big.jpg"))
    assert "big.jpg: 2400x1600 px, JPEG" in out
    m = re.search(r"bounding box x (\d+)\.\.(\d+), y (\d+)\.\.(\d+)", out)
    assert m, out
    for got, want in zip(m.groups(), (600, 1799, 400, 1199)):
        assert abs(int(got) - want) <= 8, (m.groups(), want)      # 1/4 draft + 640 cap: a few px


# ── compare ───────────────────────────────────────────────────────────────────

def test_compare_puts_the_silhouettes_side_by_side_with_deltas(tmp_path):
    a = _rect(tmp_path, "ref.png", box=(100, 50, 199, 149))
    b = _rect(tmp_path, "cand.png", box=(100, 50, 239, 149))      # 40 px wider, same left edge
    out = _report(a, compare_to=b)
    assert out.splitlines()[1].startswith("COMPARE  A (reference) = ref.png 400x300  |  B (candidate) = cand.png 400x300")
    assert re.search(r"100\.\.199 \(100\)\s+100\.\.239 \(140\)\s+\+40\s+\+0\s+\+40", out), out
    m = re.search(r"Mean absolute pixel difference: ([\d.]+)/255", out)
    assert m and float(m[1]) > 0
    assert "Foreground box    A x 100..199, y 50..149   B x 100..239, y 50..149" in out


def test_compare_of_an_image_with_itself_is_zero(tmp_path):
    p = _rect(tmp_path)
    out = _report(p, compare_to=p)
    assert "Mean absolute pixel difference: 0.0/255 (0.0%)" in out
    rows = [ln for ln in out.splitlines() if re.search(r"\(\d+\)\s+\S+\.\.\S+ \(\d+\)", ln)]
    assert rows and all(re.search(r"\+0\s+\+0\s+\+0$", ln) for ln in rows), rows
    assert re.search(r"distance (?!0\b)\d+", out) is None, "every region matches itself"


def test_compare_resamples_the_candidate_to_the_references_size(tmp_path):
    a = _rect(tmp_path, "ref.png", size=(400, 300), box=(100, 50, 199, 149))
    b = _rect(tmp_path, "half.png", size=(200, 150), box=(50, 25, 99, 74))   # the same picture at half size
    out = _report(a, compare_to=b)
    m = re.search(r"Mean absolute pixel difference: ([\d.]+)/255", out)
    assert float(m[1]) < 1.5, out
    assert "Aspect ratios differ" not in out


def test_compare_warns_when_the_shapes_differ(tmp_path):
    out = _report(_rect(tmp_path, size=(400, 300)), compare_to=_rect(tmp_path, "sq.png", size=(300, 300), box=(80, 50, 179, 149)))
    assert "Aspect ratios differ" in out


# ── bounds, errors, honesty ───────────────────────────────────────────────────

def test_report_stays_a_few_kb_even_for_noise(tmp_path):
    im = Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3))
    p = _save(tmp_path, im, "noise.png")
    assert len(_report(p)) <= d.MAX_CHARS + 200
    assert len(_report(p, compare_to=p)) <= d.MAX_CHARS + 200


def test_report_describes_and_does_not_interpret(tmp_path):
    im = Image.new("RGB", (400, 400), WHITE)
    dr = ImageDraw.Draw(im)
    dr.polygon([(100, 0), (300, 0), (250, 200), (150, 200)], fill=(20, 20, 20))     # a dark cone
    dr.ellipse((150, 220, 250, 380), fill=(250, 120, 40))
    out = _report(_save(tmp_path, im, "shapes.png")).lower()
    for word in ("mirror", "reflect", "glass", "metal", "lamp", "cone", "shadow", "object", "looks like",
                 "appears", "person", "face", "button", "logo", "background image", "likely", "probably"):
        assert word not in out, word
    assert "dark region" in out and "extent narrows" in out, "the shape IS stated, as numbers"


def test_it_is_fast(tmp_path):
    p = _save(tmp_path, Image.new("RGB", (1536, 1536), (200, 30, 30)), "fast.png")
    _report(p)                                  # warm imports
    t = time.perf_counter()
    _report(p)
    assert time.perf_counter() - t < 1.0


@pytest.mark.parametrize("make", ["missing", "directory", "suffix", "corrupt", "empty_arg"])
def test_failures_are_a_tool_result_not_an_exception(tmp_path, make):
    if make == "missing":
        args = {"path": str(tmp_path / "nope.png")}
    elif make == "directory":
        args = {"path": str(tmp_path)}
    elif make == "suffix":
        (tmp_path / "x.txt").write_text("hi")
        args = {"path": str(tmp_path / "x.txt")}
    elif make == "corrupt":
        (tmp_path / "bad.png").write_bytes(b"not a png at all")
        args = {"path": str(tmp_path / "bad.png")}
    else:
        args = {"path": "   "}
    out = d.describe_tool(args)
    assert out.startswith("[error] view_image: "), out
    assert "Traceback" not in out


def test_a_tiny_image_gets_a_short_report_and_cannot_be_compared(tmp_path):
    p = _save(tmp_path, Image.new("RGB", (1, 1), (9, 9, 9)), "dot.png")
    out = _report(p)
    assert "1x1 px" in out and "Too small for region analysis" in out
    assert _report(p, compare_to=_rect(tmp_path)).startswith("[error] view_image: an image under 8 px")


def test_a_bad_compare_to_is_an_error_naming_it(tmp_path):
    out = d.describe_tool({"path": _rect(tmp_path), "compare_to": str(tmp_path / "gone.png")})
    assert out.startswith("[error] view_image: no such file: ") and "gone.png" in out


def test_relative_paths_resolve_against_cwd(tmp_path, monkeypatch):
    _rect(tmp_path, "rel.png")
    monkeypatch.chdir(tmp_path)
    assert "rel.png: 400x300 px" in d.describe_tool({"path": "rel.png"})
