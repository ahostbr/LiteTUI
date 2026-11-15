"""A fast TEXT report of an image, for a model that cannot see.

`view_image` stages pixels for a vision model. A model without vision (model_type
"llm") gets nothing from that, so the plugin routes it here instead: plain numbers
and positions that a text-only model can reason over. It is the pixel-analysis
script a no-vision agent wrote for itself (background, silhouette width per row,
reference-vs-render compare), made part of the tool.

DESCRIBE, DON'T INTERPRET. The report states colours, shapes and coordinates and
never what they are. A dark region that narrows is reported as one; whether it is
a cone, a reflection or a shadow is the reader's call.

Pure Pillow + stdlib, no numpy (it is not a declared dependency). Everything heavy
runs at C level: JPEG draft decode, BOX downscale, ImageChops, getbbox, histogram.
The only Python loops are over a few dozen bands and a 32x32 grid.

Coordinates are in ORIGINAL pixels; the analysis runs on a copy whose longest side
is at most ANALYSIS_SIDE, so edges are good to about one analysis step.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

ANALYSIS_SIDE = 640
BANDS = 16
GRID = 3            # colour grid is GRID x GRID
CELLS = 32          # bright/dark region grid is CELLS x CELLS
MAX_CHARS = 6000
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

_BRIGHT_L = 225     # luminance >= this is "bright"
_DARK_L = 40        # luminance <= this is "dark"
_UNIFORM_FRAC = 0.60  # at least this share of the border ring matching its median: one background colour
                      # (low on purpose: a subject that spans the frame covers a quarter of the ring)
_NEAR = 24            # channel window around the border median used for the noise estimate
_MIN_FG_DIFF = 40     # summed |channel - background| above this is "not background"


class ImageReportError(Exception):
    """A user-facing reason the report cannot be made."""


# ── loading ───────────────────────────────────────────────────────────────────

class _Loaded:
    __slots__ = ("path", "size", "mode", "fmt", "frames", "alpha_pct", "im", "sx", "sy")


def _resolve(raw: str) -> Path:
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        raise ImageReportError("`path` is required (absolute path to an image file)")
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if not p.exists():
        raise ImageReportError(f"no such file: {p}")
    if not p.is_file():
        raise ImageReportError(f"not a file: {p}")
    if p.suffix.lower() not in IMAGE_SUFFIXES:
        raise ImageReportError(
            f"{p.suffix or 'no extension'} is not a supported image type (png, jpg, jpeg, gif, webp, bmp)")
    return p


def load(path: Path, side: int = ANALYSIS_SIDE) -> _Loaded:
    """Open, flatten alpha onto white, downscale. Never leaves a file handle open."""
    try:
        src = Image.open(path)
        out = _Loaded()
        out.path = path
        out.size = src.size
        out.mode = src.mode
        out.fmt = src.format or path.suffix.lstrip(".").upper()
        out.frames = getattr(src, "n_frames", 1)
        w, h = src.size
        if max(w, h) > side and src.format == "JPEG":
            src.draft("RGB", (side, side))     # decode at 1/2, 1/4 or 1/8 scale: the big win
        im = src
        out.alpha_pct = 0.0
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            a = rgba.getchannel("A")
            hist = a.histogram()
            out.alpha_pct = 100.0 * (sum(hist[:250])) / max(1, sum(hist))
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=a)
            im = flat
        else:
            im = im.convert("RGB")
        iw, ih = im.size
        if max(iw, ih) > side:
            f = side / max(iw, ih)
            im = im.resize((max(1, round(iw * f)), max(1, round(ih * f))), Image.BOX)
        out.im = im
        out.sx = w / im.size[0]
        out.sy = h / im.size[1]
        src.close()
        return out
    except ImageReportError:
        raise
    except Exception as e:  # Pillow raises a zoo: UnidentifiedImageError, OSError, DecompressionBombError...
        raise ImageReportError(f"could not decode {path.name} as an image ({type(e).__name__})") from e


# ── colour helpers ────────────────────────────────────────────────────────────

def _hex(c) -> str:
    return "#%02x%02x%02x" % (int(c[0]), int(c[1]), int(c[2]))


def colour_name(c) -> str:
    """A coarse name for an RGB triple. Naming a colour is describing it."""
    r, g, b = (x / 255.0 for x in c[:3])
    mx, mn = max(r, g, b), min(r, g, b)
    v, d = mx, mx - mn
    s = 0.0 if mx == 0 else d / mx
    if v < 0.14:
        return "black"
    if s < 0.18:
        if v > 0.90:
            return "white"
        if v > 0.68:
            return "light gray"
        if v > 0.34:
            return "gray"
        return "dark gray"
    if mx == r:
        hue = ((g - b) / d) % 6
    elif mx == g:
        hue = (b - r) / d + 2
    else:
        hue = (r - g) / d + 4
    hue *= 60
    if hue < 15 or hue >= 345:
        base = "red"
    elif hue < 40:
        base = "orange"
    elif hue < 70:
        base = "yellow"
    elif hue < 160:
        base = "green"
    elif hue < 200:
        base = "cyan"
    elif hue < 255:
        base = "blue"
    elif hue < 290:
        base = "purple"
    else:
        base = "pink"
    if base in ("orange", "yellow", "red") and v < 0.55 and s > 0.3:
        return "brown" if base != "red" else "dark red"
    if v < 0.35:
        return "dark " + base
    if v > 0.80 and s < 0.50:
        return "light " + base
    return base


def _top_colours(im: Image.Image, n: int) -> list[tuple[tuple[int, int, int], float]]:
    """Representative colours with their share, via median-cut. [(rgb, share 0..1)]"""
    q = im.quantize(colors=max(2 * n, 8), method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette() or []
    total = im.size[0] * im.size[1]
    out: list[list] = []
    for count, idx in sorted(q.getcolors(), reverse=True):
        c = (pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2])
        for o in out:      # median-cut splits one flat colour into several near-twins: fold them
            if sum((p - r) ** 2 for p, r in zip(c, o[0])) < 24 * 24:
                o[1] += count / total
                break
        else:
            out.append([c, count / total])
    out.sort(key=lambda o: -o[1])
    return [(o[0], o[1]) for o in out[:n] if o[1] >= 0.01 or o is out[0]]


def _fmt_col(rgb, share: float | None = None) -> str:
    s = f"{colour_name(rgb)} {_hex(rgb)}"
    return s if share is None else f"{s} {share * 100:.0f}%"


def _mean_rgb(im: Image.Image) -> tuple[int, int, int]:
    one = im.resize((1, 1), Image.BOX).getpixel((0, 0))
    return one[0], one[1], one[2]


# ── analysis pieces ───────────────────────────────────────────────────────────

def _border_stats(im: Image.Image):
    """Median colour and per-channel std of a 4 px ring. Median, so a subject
    that touches an edge does not drag the background estimate."""
    w, h = im.size
    b = max(1, min(4, w // 8, h // 8))
    strips = [im.crop((0, 0, w, b)), im.crop((0, h - b, w, h)),
              im.crop((0, b, b, h - b)), im.crop((w - b, b, w, h - b))]
    strips = [s for s in strips if s.size[0] and s.size[1]]
    hist = [0] * 768
    for s in strips:
        for i, v in enumerate(s.histogram()):
            hist[i] += v
    med, std = [], []
    for ch in range(3):
        hh = hist[ch * 256:(ch + 1) * 256]
        n = sum(hh)
        half, acc, m = n / 2, 0, 0
        for i, c in enumerate(hh):
            acc += c
            if acc >= half:
                m = i
                break
        med.append(m)
        # std of the pixels that sit near the median only: a subject touching the
        # border must not read as background noise.
        lo, hi = max(0, m - _NEAR), min(255, m + _NEAR)
        near = sum(hh[lo:hi + 1]) or 1
        mean = sum(i * hh[i] for i in range(lo, hi + 1)) / near
        std.append((sum(hh[i] * (i - mean) ** 2 for i in range(lo, hi + 1)) / near) ** 0.5)
    # Fraction of the ring that is the background colour (threshold as for the mask).
    bg = tuple(med)
    close = total = 0
    for s in strips:
        d = ImageChops.difference(s, Image.new("RGB", s.size, bg))
        r, g, bl = d.split()
        sm = ImageChops.add(ImageChops.add(r, g), bl).histogram()
        close += sum(sm[:_MIN_FG_DIFF + 1])
        total += s.size[0] * s.size[1]
    return bg, tuple(std), close / max(1, total)


def _fg_mask(im: Image.Image, bg, std) -> Image.Image:
    """255 where the pixel is not the background colour. Threshold rises with
    border noise so a JPEG-speckled background does not read as foreground."""
    diff = ImageChops.difference(im, Image.new("RGB", im.size, bg))
    r, g, b = diff.split()
    total = ImageChops.add(ImageChops.add(r, g), b)
    t = max(_MIN_FG_DIFF, int(3 * sum(std)))
    return total.point(lambda v, t=t: 255 if v > t else 0)


def _band_edges(h: int, bands: int = BANDS) -> list[tuple[int, int]]:
    bands = max(1, min(bands, h))
    return [(i * h // bands, (i + 1) * h // bands) for i in range(bands)]


def _silhouette(mask: Image.Image):
    """Per band: (y0, y1, left, right, fill_px_per_row) in ANALYSIS pixels, or None for empty."""
    w, h = mask.size
    rows = []
    for y0, y1 in _band_edges(h):
        band = mask.crop((0, y0, w, y1))
        bb = band.getbbox()
        if not bb:
            rows.append((y0, y1, None, None, 0.0))
            continue
        fill = band.histogram()[255] / max(1, (y1 - y0))
        rows.append((y0, y1, bb[0], bb[2] - 1, fill))
    return rows


def _regions(mask: Image.Image):
    """4-connected regions of a boolean mask at CELLS x CELLS resolution.
    Returns [(cells, x0, y0, x1, y1)] in analysis pixels, biggest first."""
    w, h = mask.size
    gw, gh = min(CELLS, w), min(CELLS, h)
    g = mask.resize((gw, gh), Image.BOX)
    px = list(g.getdata())
    on = [v >= 128 for v in px]
    seen = [False] * len(on)
    regs = []
    for i in range(len(on)):
        if not on[i] or seen[i]:
            continue
        stack, cells = [i], []
        seen[i] = True
        while stack:
            j = stack.pop()
            cells.append(j)
            cx, cy = j % gw, j // gw
            for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                if 0 <= nx < gw and 0 <= ny < gh:
                    k = ny * gw + nx
                    if on[k] and not seen[k]:
                        seen[k] = True
                        stack.append(k)
        xs = [c % gw for c in cells]
        ys = [c // gw for c in cells]
        regs.append((len(cells), min(xs) * w / gw, min(ys) * h / gh, (max(xs) + 1) * w / gw, (max(ys) + 1) * h / gh))
    regs.sort(key=lambda r: -r[0])
    return regs, gw * gh


def _threshold_mask(lum: Image.Image, lo: bool, fg: Image.Image | None) -> Image.Image:
    m = lum.point(lambda v: 255 if (v <= _DARK_L if lo else v >= _BRIGHT_L) else 0)
    return ImageChops.darker(m, fg) if fg is not None else m   # both 0/255: darker == AND


def _runs(rows):
    """Monotone width runs of >= 3 bands that change the extent by >= 20% of the widest."""
    widths = [(r[3] - r[2] + 1) if r[2] is not None else 0 for r in rows]
    if not widths or max(widths) == 0:
        return []
    wmax = max(widths)
    out, i, n = [], 0, len(widths)
    while i < n - 2:
        sgn = 0
        j = i
        while j + 1 < n:
            d = widths[j + 1] - widths[j]
            s = (d > 0) - (d < 0)
            if s == 0 or (sgn and s != sgn):
                break
            sgn = s
            j += 1
        if j - i >= 2 and abs(widths[j] - widths[i]) >= 0.2 * wmax:
            out.append((i, j, sgn))
            i = j
        else:
            i = max(j, i + 1)
    return out


# ── the report ────────────────────────────────────────────────────────────────

class _Analysis:
    pass


def analyse(ld: _Loaded) -> _Analysis:
    im = ld.im
    a = _Analysis()
    a.ld = ld
    a.bg, a.bg_std, a.bg_frac = _border_stats(im)
    a.uniform = a.bg_frac >= _UNIFORM_FRAC
    a.fg = _fg_mask(im, a.bg, a.bg_std) if a.uniform else None
    w, h = im.size
    a.fg_pct = (100.0 * a.fg.histogram()[255] / (w * h)) if a.fg is not None else None
    a.bbox = a.fg.getbbox() if a.fg is not None else None
    a.rows = _silhouette(a.fg) if (a.fg is not None and a.bbox) else None
    gw, gh = w / GRID, h / GRID
    a.cells = []
    for gy in range(GRID):
        for gx in range(GRID):
            cell = im.crop((round(gx * gw), round(gy * gh), round((gx + 1) * gw), round((gy + 1) * gh)))
            a.cells.append((gy, gx, _top_colours(cell, 2), _mean_rgb(cell)))
    a.top = _top_colours(im, 5)
    lum = im.convert("L")
    a.bright, a.nreg = _regions(_threshold_mask(lum, False, a.fg))
    a.dark, _ = _regions(_threshold_mask(lum, True, a.fg))
    return a


def _ox(ld, x):  # analysis -> original px
    return round(x * ld.sx)


def _oy(ld, y):
    return round(y * ld.sy)


def _head(ld: _Loaded) -> str:
    w, h = ld.size
    extra = []
    if ld.frames > 1:
        extra.append(f"{ld.frames} frames, first analysed")
    if ld.alpha_pct >= 0.5:
        extra.append(f"{ld.alpha_pct:.0f}% transparent, flattened onto white")
    iw, ih = ld.im.size
    step = max(ld.sx, ld.sy)
    return (f"{ld.path.name}: {w}x{h} px, {ld.fmt}, {ld.mode}"
            + (f" ({'; '.join(extra)})" if extra else "")
            + f". Analysed at {iw}x{ih}; coordinates are original pixels, good to about {step:.0f} px.")


def _bg_line(a: _Analysis) -> str:
    pct = a.bg_frac * 100
    if a.uniform:
        touch = "" if pct >= 99.5 else f"; {100 - pct:.0f}% of the border is another colour (something touches the edge)"
        return (f"Background (4 px border median): {_fmt_col(a.bg)}, uniform "
                f"(noise std {max(a.bg_std):.1f}/255{touch}).")
    return (f"Border is not one colour (only {pct:.0f}% of it matches its median {_fmt_col(a.bg)}); "
            "no foreground/background split, so no silhouette.")


def _region_lines(label: str, regs, total_cells: int, ld: _Loaded, limit: int = 4) -> list[str]:
    if not regs:
        return [f"{label}: none."]
    lines = []
    for n, x0, y0, x1, y1 in regs[:limit]:
        lines.append(f"  x {_ox(ld, x0)}..{_ox(ld, x1)}, y {_oy(ld, y0)}..{_oy(ld, y1)} "
                     f"({_ox(ld, x1) - _ox(ld, x0)}x{_oy(ld, y1) - _oy(ld, y0)} px, {100.0 * n / total_cells:.1f}% of image)")
    more = f", {len(regs) - limit} smaller not listed" if len(regs) > limit else ""
    return [f"{label} ({len(regs)} region{'s' if len(regs) != 1 else ''}{more}):"] + lines


_MIN_SIDE = 8   # below this a border ring, a 3x3 grid and row bands mean nothing


def _tiny(ld: _Loaded) -> bool:
    return min(ld.size) < _MIN_SIDE


def report(path: Path) -> str:
    ld = load(path)
    if _tiny(ld):
        return (_head(ld) + "\nToo small for region analysis (under "
                f"{_MIN_SIDE} px on a side). Colours: " + ", ".join(_fmt_col(c, s) for c, s in _top_colours(ld.im, 3)) + ".")
    a = analyse(ld)
    out = [_head(ld), _bg_line(a)]
    out.append("Dominant colours overall: " + ", ".join(_fmt_col(c, s) for c, s in a.top) + ".")
    out.append(f"Colour by region (3x3 grid, row-major top-left to bottom-right; "
               f"cell = {_ox(ld, ld.im.size[0] / GRID)}x{_oy(ld, ld.im.size[1] / GRID)} px):")
    for gy, gx, cols, mean in a.cells:
        out.append(f"  r{gy + 1}c{gx + 1}: " + ", ".join(_fmt_col(c, s) for c, s in cols))
    if a.fg is not None:
        if not a.bbox:
            out.append("Foreground (not background): none; the image is one colour.")
        else:
            x0, y0, x1, y1 = a.bbox
            out.append(f"Foreground (not background): {a.fg_pct:.1f}% of pixels, bounding box "
                       f"x {_ox(ld, x0)}..{_ox(ld, x1 - 1)}, y {_oy(ld, y0)}..{_oy(ld, y1 - 1)} "
                       f"({_ox(ld, x1 - x0)}x{_oy(ld, y1 - y0)} px).")
            out.append(f"Width per row band (left..right extent, width, filled px per row), {BANDS} bands:")
            for y0b, y1b, l, r, fill in a.rows:
                span = f"y {_oy(ld, y0b)}..{_oy(ld, y1b)}"
                if l is None:
                    out.append(f"  {span}: empty")
                else:
                    out.append(f"  {span}: x {_ox(ld, l)}..{_ox(ld, r)}, width {_ox(ld, r - l + 1)}, filled {_ox(ld, fill)}")
            for i, j, sgn in _runs(a.rows):
                wi = _ox(ld, a.rows[i][3] - a.rows[i][2] + 1)
                wj = _ox(ld, a.rows[j][3] - a.rows[j][2] + 1)
                yi = _oy(ld, (a.rows[i][0] + a.rows[i][1]) / 2)    # band centres
                yj = _oy(ld, (a.rows[j][0] + a.rows[j][1]) / 2)
                out.append(f"  extent {'widens' if sgn > 0 else 'narrows'} from {wi} px (y={yi}) "
                           f"to {wj} px (y={yj})")
    scope = "inside the foreground" if a.fg is not None else "in the whole image"
    out += _region_lines(f"Bright regions (luminance >= {_BRIGHT_L}, {scope})", a.bright, a.nreg, ld)
    out += _region_lines(f"Dark regions (luminance <= {_DARK_L}, {scope})", a.dark, a.nreg, ld)
    return _cap("\n".join(out))


def compare(path_a: Path, path_b: Path) -> str:
    """Reference A against candidate B on the same pixel grid (B is resampled to A's size)."""
    la, lb = load(path_a), load(path_b)
    if _tiny(la) or _tiny(lb):
        raise ImageReportError(f"an image under {_MIN_SIDE} px on a side is too small to compare")
    if lb.im.size != la.im.size:
        lb.im = lb.im.resize(la.im.size, Image.BOX)   # A's grid; every coordinate below is A's
    aa, ab = analyse(la), analyse(lb)
    w, h = la.im.size
    out = [f"COMPARE  A (reference) = {la.path.name} {la.size[0]}x{la.size[1]}  |  "
           f"B (candidate) = {lb.path.name} {lb.size[0]}x{lb.size[1]}"]
    if abs(la.size[0] / la.size[1] - lb.size[0] / lb.size[1]) > 0.01:
        out.append("Aspect ratios differ; B was stretched to A's shape for the comparison.")
    out.append(f"Compared on A's grid {w}x{h} (coordinates in A's original pixels, good to about {max(la.sx, la.sy):.0f} px).")
    diff = ImageChops.difference(la.im, lb.im)
    means = [sum(i * c for i, c in enumerate(diff.histogram()[k * 256:(k + 1) * 256])) / (w * h) for k in range(3)]
    mean_all = sum(means) / 3
    out.append(f"Mean absolute pixel difference: {mean_all:.1f}/255 ({mean_all / 2.55:.1f}%), "
               f"per channel R {means[0]:.1f} G {means[1]:.1f} B {means[2]:.1f}.")
    out.append(f"Background  A: {_fmt_col(aa.bg)}{'' if aa.uniform else ' (border varies)'}   "
               f"B: {_fmt_col(ab.bg)}{'' if ab.uniform else ' (border varies)'}")
    if aa.uniform and ab.uniform and sum(abs(p - q) for p, q in zip(aa.bg, ab.bg)) > _MIN_FG_DIFF:
        out.append("Backgrounds differ: foreground is each image's own non-background, so the silhouette rows below compare unlike masks.")
    if aa.fg is not None and ab.fg is not None:
        def bb(x):
            return "none" if not x.bbox else (f"x {_ox(la, x.bbox[0])}..{_ox(la, x.bbox[2] - 1)}, "
                                              f"y {_oy(la, x.bbox[1])}..{_oy(la, x.bbox[3] - 1)}")
        out.append(f"Foreground share  A {aa.fg_pct:.1f}%  B {ab.fg_pct:.1f}%  (difference {ab.fg_pct - aa.fg_pct:+.1f} points)")
        out.append(f"Foreground box    A {bb(aa)}   B {bb(ab)}")
        if aa.rows and ab.rows:
            out.append("Width per row band (extent px; delta = B minus A):")
            out.append(f"  {'y':>11}  {'A x..x (w)':>20}  {'B x..x (w)':>20}  {'delta w':>8}  {'delta left':>10}  {'delta right':>11}")
            for ra, rb in zip(aa.rows, ab.rows):
                span = f"{_oy(la, ra[0])}..{_oy(la, ra[1])}"

                def f(r):
                    return "empty" if r[2] is None else f"{_ox(la, r[2])}..{_ox(la, r[3])} ({_ox(la, r[3] - r[2] + 1)})"
                if ra[2] is None or rb[2] is None:
                    d = ("", "", "")
                else:
                    d = (f"{_ox(la, (rb[3] - rb[2]) - (ra[3] - ra[2])):+d}", f"{_ox(la, rb[2] - ra[2]):+d}", f"{_ox(la, rb[3] - ra[3]):+d}")
                out.append(f"  {span:>11}  {f(ra):>20}  {f(rb):>20}  {d[0]:>8}  {d[1]:>10}  {d[2]:>11}")
    else:
        out.append("One image has no single background colour, so there is no silhouette comparison.")
    out.append("Colour by region (3x3 grid mean colour, A -> B, distance = RGB Euclidean 0..441):")
    for (gy, gx, _, ma), (_, _, _, mb) in zip(aa.cells, ab.cells):
        dist = sum((p - q) ** 2 for p, q in zip(ma, mb)) ** 0.5
        out.append(f"  r{gy + 1}c{gx + 1}: {_fmt_col(ma)} -> {_fmt_col(mb)}  distance {dist:.0f}")
    for label, ra, rb in (("Bright", aa.bright, ab.bright), ("Dark", aa.dark, ab.dark)):
        out.append(f"{label} regions: A {len(ra)}, B {len(rb)}; largest A {_big(ra, aa.nreg)}, B {_big(rb, ab.nreg)}")
    return _cap("\n".join(out))


def _big(regs, total) -> str:
    return "none" if not regs else f"{100.0 * regs[0][0] / total:.1f}% of image"


def _cap(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n[report truncated]"


# ── tool entry ────────────────────────────────────────────────────────────────

NOTE = ("[view_image: this model cannot see images, so this is a measurement report "
        "(colours, shapes, positions), not the picture.]\n")


def describe_tool(args: dict) -> str:
    """The view_image handler for a model without vision. Never raises."""
    try:
        path = _resolve(str(args.get("path") or ""))
        other = str(args.get("compare_to") or "").strip()
        body = compare(path, _resolve(other)) if other else report(path)
        return NOTE + body
    except ImageReportError as e:
        return f"[error] view_image: {e}"
    except Exception as e:  # last resort: a tool result, never a crashed turn
        return f"[error] view_image: could not analyse the image ({type(e).__name__}: {e})"
