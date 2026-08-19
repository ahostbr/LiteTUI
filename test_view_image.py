"""view_image: the tool that must NOT return what it was asked for.

Ryan: "if i send him a path to a image to view he gets confused ... but when i
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

sys.path.insert(0, str(Path(__file__).parent))
import app as app_mod

ok = []


def chk(label, cond):
    ok.append(bool(cond))
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
    _load_image_file = app_mod.LiteTUI._load_image_file
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


print("=== the spec tells the model where to look ===")
fn = app_mod.VIEW_IMAGE_TOOL_SPEC["function"]
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

print("\n=== it is offered only when the model can possibly see ===")
names = lambda app: [t["function"]["name"] for t in app._all_tools()]
chk("vlm    -> offered", "view_image" in names(FakeApp("vlm")))
chk("None   -> offered (unknown is not 'no')", "view_image" in names(FakeApp(None)))
chk("🔴 llm -> NOT offered", "view_image" not in names(FakeApp("llm")))
chk("dispatch resolves it", FakeApp("vlm")._dispatch_for("view_image") is not None)

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

print(f"\n{sum(ok)}/{len(ok)} passed")
import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if all(ok) else 1)
