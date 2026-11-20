"""view_image: the tool that must NOT return what it was asked for.

the user: "if i send him a path to a image to view he gets confused ... but when i
paste it he sees it right away." Both halves are true and the reason is
structural: a tool result is a role:"tool" message whose content is a STRING.
Images reach the model ONLY as an image_url block on a role:"user" message.

So the obvious implementation -- return base64, or a data URI, or anything at
all -- cannot work. It would describe nothing, confidently, after spending a
megabyte of context. This tool stages the image and the tool loop injects it
through the same door the paste path already uses.

The tests that matter are therefore about SHAPE, not about happy paths:
  * the tool result must NOT contain the image
  * the injected turn must be role:"user" with an image_url block
  * it must be drained AFTER the tool results (every tool_call_id answered
    before a non-tool turn appears) and exactly once
"""
import base64
import io as _io
import sys
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as app_mod
from litetui.plugins.view_image import VIEW_IMAGE_TOOL_SPEC
from litetui import appsvc

ok = []
#: The labels that FAILED. `ok` is bools, which is all an exit status needed; a
#: pytest arm has to be able to say WHICH check failed, and a bool cannot.
#: Recorded alongside rather than by changing `ok`, so `sum(ok)` / `all(ok)` /
#: `len(ok)` keep meaning exactly what they meant (T700).
failures: list[str] = []


def chk(label, cond):
    ok.append(bool(cond))
    if not cond:
        failures.append(label)
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


TMP = Path(__file__).parent / ".tmp_view_image"
TMP.mkdir(exist_ok=True)


def make_png(name="probe.png", size=(64, 48)):
    from PIL import Image
    p = TMP / name
    Image.new("RGB", size, (10, 120, 200)).save(p)
    return p


class FakeApp:
    _tool_view_image = app_mod.LiteTUI._tool_view_image
    _load_image_file = appsvc.load_image_file
    _all_tools = app_mod.LiteTUI._all_tools
    _dispatch_for = app_mod.LiteTUI._dispatch_for

    def __init__(self, model_type=None):
        self.model_type = model_type
        self.model_id = "qwen/qwen3.8-27b"
        self._pending_tool_images = []
        self.skills = None
        self.mcp = type("M", (), {"tool_specs": lambda self: []})()
        self._mcp_dispatch = {}
        self.seat = type("S", (), {"registered": False})()
        # The offer gate lives in the view_image PLUGIN since the split —
        # register it against THIS fake so the gate closes over the fake's
        # model_type, exactly as the real loader does against the real app.
        from litetui.plugins import PluginContext, PluginRegistry
        from litetui.plugins import view_image as _vi
        self.plugins = PluginRegistry()
        _vi.PLUGIN.register(PluginContext(self, self.plugins, "view-image"))


print("=== the spec tells the model where to look ===")
fn = VIEW_IMAGE_TOOL_SPEC["function"]
chk("named view_image", fn["name"] == "view_image")
chk("requires a path", fn["parameters"]["required"] == ["path"])
chk("🔴 says the RESULT is not the picture",
    "not the picture" in fn["description"] and "next message" in fn["description"])

print("\n=== 🔴 the result must NOT carry the image ===")
img = make_png()
a = FakeApp("vlm")
out = a._tool_view_image({"path": str(img)})
b64 = a._pending_tool_images[0][1]
chk("returns a short confirmation", len(out) < 200)
chk("🔴 the base64 is NOT in the tool result", b64 not in out)
chk("...nor is a data URI", "data:image" not in out)
chk("it points the model at the next message", "next message" in out)
chk("the image WAS staged", len(a._pending_tool_images) == 1)
chk("staged with its resolved path", a._pending_tool_images[0][0] == str(img))

print("\n=== the vision precondition is reported, not left to a raw 400 ===")
a = FakeApp("llm")
out = a._tool_view_image({"path": str(img)})
chk("🔴 refuses on a non-vision model", out.startswith("[error]"))
chk("...names the actual cause (mmproj / vision projector)",
    "mmproj" in out or "projector" in out)
chk("...and nothing is staged", a._pending_tool_images == [])
chk("unknown model type still tries (the tool reports, absence would be silent)",
    not FakeApp(None)._tool_view_image({"path": str(img)}).startswith("[error]"))

print("\n=== it is offered to every model (T1319: a text-only model gets the text report) ===")
names = lambda app: [t["function"]["name"] for t in app._all_tools()]
chk("vlm    -> offered", "view_image" in names(FakeApp("vlm")))
chk("None   -> offered (unknown is not 'no')", "view_image" in names(FakeApp(None)))
chk("🔴 llm -> offered too (was NOT offered before T1319)", "view_image" in names(FakeApp("llm")))
chk("dispatch resolves it", FakeApp("vlm")._dispatch_for("view_image") is not None)

print("\n=== a text-only model gets the report, and a backend that knows why says so ===")
from litetui import image_describe

run = lambda app, args: app._dispatch_for("view_image")(args)
a = FakeApp("llm")
out = run(a, {"path": str(img)})
chk("no backend opinion -> the plain 'cannot see images' header", out.startswith(image_describe.NOTE))
chk("...and nothing is staged", a._pending_tool_images == [])
WHY = "[view_image: this server was started with image input off: /engine stop, then /engine start.]\n"
a = FakeApp("llm")
a.settings = object()
a.backend = type("B", (), {"image_input_off_note": lambda self, settings=None: WHY})()
out = run(a, {"path": str(img)})
chk("🔴 a backend that knows why replaces the header with what to do", out.startswith(WHY))
chk("...the old header is gone, the measurements stay",
    "this model cannot see images" not in out and "64x48 px" in out)
a.backend = type("B", (), {"image_input_off_note": lambda self, settings=None: None})()
chk("a backend with nothing to add keeps the plain header", run(a, {"path": str(img)}).startswith(image_describe.NOTE))
a.backend = type("B", (), {"image_input_off_note": lambda self, settings=None: WHY})()
chk("an [error] result is never given the header",
    run(a, {"path": str(TMP / "nope.png")}).startswith("[error]"))
a = FakeApp("vlm")
a.backend = type("B", (), {"image_input_off_note": lambda self, settings=None: WHY})()
chk("a model that can see is untouched: the picture is staged", "next message" in run(a, {"path": str(img)}))

print("\n=== 🔴 T1371: `read` on a picture opens the picture, it does not say 'use bash' ===")
# Ryan's Basalt seat had images on and view_image in its tool list. It called `read` on a
# .jpg first, was told "[binary file ... use bash to inspect]", and never saw the picture.
from litetui.plugins import PluginContext as _Ctx
from litetui.plugins import core_tools as _core


def with_read(app):
    _core.PLUGIN.register(_Ctx(app, app.plugins, "core-tools"))
    return app


a = with_read(FakeApp("vlm"))
out = a._dispatch_for("read")({"path": str(img)})
chk("vlm: read on a .png stages the picture", len(a._pending_tool_images) == 1 and a._pending_tool_images[0][0] == str(img))
chk("...and answers like view_image: look in the next message", "next message" in out)
chk("...never the old dead end", "binary file" not in out and "use bash" not in out)
a = with_read(FakeApp(None))
chk("unknown model type: read on a picture still tries to attach it",
    "next message" in a._dispatch_for("read")({"path": f'"{img}"'}) and len(a._pending_tool_images) == 1)
a = with_read(FakeApp("llm"))
out = a._dispatch_for("read")({"path": str(img)})
from litetui import image_describe as _describe
chk("llm: read on a picture gives the measurement report", out.startswith(_describe.NOTE) and "64x48 px" in out)
chk("...and stages nothing", a._pending_tool_images == [])
note = TMP / "note.txt"
note.write_text("line one\nline two\n", encoding="utf-8")
a = with_read(FakeApp("vlm"))
chk("a text file is read as text, as before", "line two" in a._dispatch_for("read")({"path": str(note)}))
blob = TMP / "blob.bin"
blob.write_bytes(b"\x00\x01\x02" * 50)
chk("a binary file that is not a picture keeps its own answer", a._dispatch_for("read")({"path": str(blob)}).startswith("[binary file"))
fake_png = TMP / "not-really.png"
fake_png.write_bytes(b"\x00\x01 this is not an image")
out = a._dispatch_for("read")({"path": str(fake_png)})
chk("a .png that does not decode is an [error], not a crash and not a staged image",
    out.startswith("[error]") and a._pending_tool_images == [])
chk("a missing .png is still 'file not found'", "file not found" in a._dispatch_for("read")({"path": str(TMP / "gone.png")}))
a = with_read(FakeApp("vlm"))
a.plugins.tools_disabled = lambda: frozenset({"view_image"})
chk("view_image switched off in the tool list: read does not bring it back",
    a._dispatch_for("read")({"path": str(img)}).startswith("[binary file") and a._pending_tool_images == [])

print("\n=== bad input never raises, always explains ===")
a = FakeApp("vlm")
for label, args in [
    ("missing path", {}),
    ("empty path", {"path": "   "}),
    ("non-existent", {"path": str(TMP / "nope.png")}),
    ("a directory", {"path": str(TMP)}),
    ("wrong extension", {"path": __file__}),
    ("a number", {"path": 12345}),
]:
    try:
        r = a._tool_view_image(args)
        chk(f"{label:16s} -> [error], no exception", isinstance(r, str) and r.startswith("[error]"))
    except Exception as e:
        chk(f"{label:16s} -> RAISED {type(e).__name__}", False)
chk("none of the bad calls staged anything", a._pending_tool_images == [])

print("\n=== quoted paths (models love to quote) ===")
a = FakeApp("vlm")
chk('"path" in double quotes is accepted',
    not a._tool_view_image({"path": f'"{img}"'}).startswith("[error]"))

print("\n=== the injected turn is the shape the model can actually see ===")
src = Path(app_mod.__file__).read_text(encoding="utf-8")
loop = src.split("_pending_tool_images:", 1)[1] if "_pending_tool_images:" in src else src
inject = src.split("if self._pending_tool_images:", 1)[1][:1600]
chk("🔴 injects role:'user', not role:'tool'", '"role": "user"' in inject)
chk("...carrying an image_url block", '"type": "image_url"' in inject)
chk("...as a data URI", "data:image/png;base64," in inject)
chk("...with text alongside so the turn is not image-only", '"type": "text"' in inject)
chk("🔴 drains the queue so one image is not sent twice",
    "self._pending_tool_images = []" in inject)
chk("...and is INSIDE the tool loop, after the results are appended",
    src.index('"role": "tool"') < src.index("if self._pending_tool_images:"))

# ⚠️ THE CLEANUP RUNS IN BOTH MODES, THE REPORT ONLY AS A SCRIPT. `TMP` is a
# real temp tree this file creates; leaving it behind under pytest would make
# the suite a litterer, so the rmtree stays at module level while the tally and
# the exit move (T700).
import shutil

shutil.rmtree(TMP, ignore_errors=True)


# ── the same checks, as a pytest arm (T700) ─────────────────────────────
#
# 🔴 THIS FILE IS NAMED `test_*` AND NOTHING HAS EVER RUN IT. A module-level
# exit raises SystemExit during collection, which pytest reports as
# INTERNALERROR and which abandons the WHOLE invocation — not just this file.
# Ten files in this directory were in that state (T699 fixed two, T700 the
# rest); each abort hid the others, which is why the class kept looking small.


def test_every_check_in_this_file_passed() -> None:
    assert ok, "no check ran — the body above did not execute"
    assert failures == [], failures


if __name__ == "__main__":
    print(f"\n{sum(ok)}/{len(ok)} passed")
    sys.exit(0 if all(ok) else 1)
