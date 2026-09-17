"""Grab the whole screen (or a region) to PNG so the model can see it.

Usage:
  python tools/screenshot.py                 # full screen -> last_shot.png
  python tools/screenshot.py --region x,y,w,h
  python tools/screenshot.py --out shot2.png
Prints the saved path + pixel size on the last line (easy to parse).
"""
import argparse, sys
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", help="x,y,w,h in pixels")
    ap.add_argument("--out", default=str(Path(__file__).parent / "last_shot.png"))
    args = ap.parse_args()

    try:
        import pyautogui
    except ImportError:
        sys.exit("pyautogui missing -> python -m pip install pyautogui pillow")

    if args.region:
        x, y, w, h = (int(v) for v in args.region.split(","))
        img = pyautogui.screenshot(region=(x, y, w, h))
    else:
        img = pyautogui.screenshot()

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"{out}  {img.width}x{img.height}")

if __name__ == "__main__":
    main()
