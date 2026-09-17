---
name: remove-sprite-bg
description: Remove white/checkerboard backgrounds from game sprite PNGs (AI-generated art) and wire them safely into HTML canvas games. Triggers on 'remove background from png', 'sprite has a white box', 'make png transparent', 'pngs never got the background removed', 'game sprites have white backgrounds'. Uses edge-seeded PIL flood fill — no numpy, no rembg, works on this box.
---

# Remove Sprite Backgrounds

AI image generators (LiteImage, stock "transparent" PNGs, etc.) frequently
ship sprites with the background **baked in as RGB**: plain white, or a
checkerboard pattern painted as 255/240-gray squares. On a dark game canvas
they show up as white boxes. This skill removes them cleanly.

## Step 0 — Ground truth first (do not skip)

`view_image` renders REAL transparency as a checkerboard — which looks
identical to a BAKED-IN checkerboard. Never judge from the render. Check:

```python
from PIL import Image
im = Image.open(path)
px = im.convert('RGBA').load()
w, h = im.size
print(im.mode, [px[0,0][3], px[w-1,0][3], px[0,h-1][3], px[w-1,h-1][3]])
# mode RGB            -> no alpha channel at all, background baked in
# corner alphas == 0  -> already transparent, skip removal
# corner alphas == 255-> opaque background, remove it
```

## Step 1 — Backup

Copy every original into `<project>/originals/` before touching anything.
Tolerances are a judgment call; the originals are the only undo.

## Step 2 — Pick tolerances per image

Tolerance = Euclidean distance to white in RGB space, for which a pixel is
eligible to be background. The fill is **edge-seeded**, so it can only eat
regions connected to the border — enclosed same-color art detail is safe as
long as some darker stroke separates it.

| Image type | tol | Why |
|---|---|---|
| Plain white bg, saturated art | 50 | safe margin over AA |
| Baked checkerboard (255 + ~240 squares) | 40 | kills both square colors |
| Art with bright highlights (silver ship ~230) | 40 | ~230 is ~37 from white; 40 clips only edge AA, interior protected by dark outlines |
| Art with pale outlines (gray star ~200) | 40 | ~200 is ~95 from white — well protected |

Rule of thumb: **tol < distance-from-white of the nearest protected color**,
and ≥ ~25 so anti-aliased edge pixels still get eaten.

## Step 3 — Run the removal (pure PIL, no numpy)

Save this as a temp script and run it (adjust `SRC`/`TOL`):

```python
import collections, os, shutil, time
from PIL import Image

def remove_bg(path, tol):
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    px = im.load()
    t2 = tol * tol
    # 1) background-like mask, single pass
    mask = bytearray(w * h)
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b, _ = px[x, y]
            if (255-r)**2 + (255-g)**2 + (255-b)**2 <= t2:
                mask[i] = 1
            i += 1
    # 2) edge-seeded flood fill
    seen = bytearray(w * h)
    dq = collections.deque()
    for x in range(w):
        for y in (0, h-1):
            i = y*w + x
            if mask[i] and not seen[i]:
                seen[i] = 1; dq.append(i)
    for y in range(h):
        for x in (0, w-1):
            i = y*w + x
            if mask[i] and not seen[i]:
                seen[i] = 1; dq.append(i)
    while dq:
        i = dq.popleft()
        x = i % w
        if x > 0:
            j = i - 1
            if mask[j] and not seen[j]:
                seen[j] = 1; dq.append(j)
        if x < w - 1:
            j = i + 1
            if mask[j] and not seen[j]:
                seen[j] = 1; dq.append(j)
        if i >= w:
            j = i - w
            if mask[j] and not seen[j]:
                seen[j] = 1; dq.append(j)
        if i < w * (h - 1):
            j = i + w
            if mask[j] and not seen[j]:
                seen[j] = 1; dq.append(j)
    # 3) punch holes
    for i in range(w * h):
        if seen[i]:
            x, y = i % w, i // w
            r, g, b, _ = px[x, y]
            px[x, y] = (r, g, b, 0)
    im.save(path, "PNG")
    return sum(seen), w*h
```

(Reference implementation with a cleaner 4-neighbour guard:
`C:\Projects\LiteTUI\temp-working-dir\remove_bg.py` if it still exists —
that one is the one that was run and verified.)

Runs in <1s per 1254² image on this box.

## Step 4 — Verify

1. Re-run the Step 0 check: corner alphas must be 0, mode RGBA.
2. `view_image` each result: art intact, no halo of leftover background,
   no holes punched inside the art.
3. If halos remain: bump tol 5-10 and re-run FROM THE BACKUP (the fill is
   not idempotent-friendly). If art got eaten: lower tol, restore, re-run.

## Step 5 — If the sprites feed an HTML canvas game, also check the HTML

The classic companion bug (measured: it froze a whole game):

- **`img.complete` is TRUE for broken images.** If `src` was wrong (e.g.
  `assets/foo.png` but the file sits next to the HTML), every sprite 404s,
  the "loaded" check still passes, and the first `ctx.drawImage` throws
  `IndexSizeError` inside `requestAnimationFrame` — silently killing the
  loop. Game appears "not working" with no visible error.
- Fix pattern: ready check = `img.complete && img.naturalWidth > 0`;
  guard **every** `drawImage` with that check and fall back to a primitive
  so a missing asset degrades instead of killing the loop.
- Verify in real Chrome: `nav` to the `file:///` URL, screenshot start
  screen, click start, wait, screenshot — a nonzero score / game-over
  screen proves the full loop ran.

## Gotchas

- Flood fill only removes border-CONNECTED regions. A background pocket fully
  enclosed by art (rare) survives — inspect, then patch by hand or extend
  the seed set.
- Don't use a global "make white transparent" pass (PIL `convert('RGBA')`
  + point ops) for art with white interiors — the edge-seeded fill is the
  whole trick.
- PNGs stay PNG. If you save as JPG the alpha dies.
