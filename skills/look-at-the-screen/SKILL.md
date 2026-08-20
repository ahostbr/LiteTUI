---
name: look-at-the-screen
description: Take a screenshot and actually look at it, instead of guessing what is on screen. Use when asked what is visible, to verify a GUI action landed, or before clicking anything.
---

# Look at the screen

You have two tools that combine into sight: `pccontrol` takes the picture,
`view_image` lets you see it. Neither is useful alone — a screenshot you never
open is a file, not an observation.

## The loop

1. **Capture.**
   ```
   pccontrol(action="screenshot", monitor=0)
   ```
   It returns a path. Monitors are 0-indexed; `pccontrol(action="monitors")`
   lists them if you do not know which one you want.

2. **Look at it.** This is the step that gets skipped.
   ```
   view_image(path="<the path it returned>")
   ```
   Until you call this you have not seen anything. Describing a screenshot you
   have not opened is describing what you expect to be there.

3. **Say what is actually there**, including the parts that surprise you. If
   the window you expected is absent, say so — an absent window is a finding,
   not a gap to fill with an assumption.

## Before you click anything

Capture first, look, and only then act. A click computed from a remembered
layout lands wherever that layout has since moved to.

After a click that was supposed to change something, capture again. "The
command returned successfully" is not evidence that the UI did anything —
`pccontrol` reports that it sent the input, never that the app accepted it.

## What this cannot tell you

A screenshot shows the TOP window on that monitor. A window that is behind
another, minimised, or on a monitor you did not capture is simply not in the
picture — and it looks identical to a window that is not running. If you need
to know whether something is running, ask the process list; do not infer it
from a picture.
