"""Stateless helpers lifted off `LiteTUI` — T070 step O2.

Every function here was a method that WROTE NO INSTANCE STATE: it read a few
attributes off the app and returned, or emitted. That is what made them liftable
at all, and it is the property to preserve — if one of these ever needs to write
`app.<something>`, it belongs back on the class or behind a real service object,
not here.

They take `app` as their first parameter because they still READ app state. That
is honest rather than elegant: the coupling is now visible in the signature
instead of hidden behind `self`.

⚠️ WHAT THIS BUYS: `LiteTUI` loses these methods, so the class method count drops
— the metric T070 exists to move. It does NOT reduce plugin reach-through and it
does not shrink the shared-state knot. See PLAN.md §6.
"""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path
from litetui import paths
from litetui.textfmt import (  # noqa: F401  (re-exported for existing callers)
    TOOL_NAME_DEFAULT,
    _markdown_to_text,
    is_reliable_rate_sample,
    load_prompt,
    memory_prompt,
    midturn_action,
    render_progress,
    thinking_header_text,
    tool_display_parts,
    tps_text,
)
from litetui import skills as skills_mod
from rich.text import Text

MAX_IMAGE_DIM = 1536
STORE_HEADER = "## Your store, loaded once at the start of this conversation"
GLASSBOX_MIN_INTERVAL_S = 0.2


def glassbox(app, channel: str, intensity: float=1.0, label: str='', *, discrete: bool=False) -> None:
    """Fire one channel at whatever plugins are watching.

    THE EMPTY-OBSERVER SHORT CIRCUIT IS FIRST AND MUST STAY FIRST. The
    thinking and output branches call this once per token, so with no
    observers the whole feature has to cost one attribute read and a falsy
    list check — not a clock read, not a dict write. A user who has not
    installed the brain must not pay for it.

    `discrete` means "this is an event, not a level". A tool call, a store
    write, a ledger and a window-fill change are things that HAPPENED;
    throttling one loses it. thinking and output are levels sampled per
    token, where two consecutive samples say the same thing.

    Never raises: PluginRegistry.emit already swallows a failing observer
    and records it on that plugin's status row.
    """
    reg = getattr(app, 'plugins', None)
    if reg is None or not reg.observers:
        return
    if not discrete:
        now = time.monotonic()
        if now - app._gb_last.get(channel, 0.0) < GLASSBOX_MIN_INTERVAL_S:
            return
        app._gb_last[channel] = now
    reg.emit({'channel': channel, 'intensity': float(intensity), 'label': label})

def glassbox_tool(app, name: str) -> None:
    """A dispatch fires ONE channel, never both.

    `write` is store_write rather than tool_call because the design treats
    the agent changing durable state as a different event from the agent
    calling something — and firing both would double-count every write in
    whatever the observer is drawing.
    """
    channel = 'store_write' if name == 'write' else 'tool_call'
    glassbox(app, channel, 1.0, name, discrete=True)

def glassbox_rate(app, channel: str) -> None:
    """Continuous channel whose intensity is the live tok/s, normalised
    against a fast-but-reachable ceiling so the common case has headroom
    rather than sitting pinned at 1.0."""
    tps = app.tps or 0.0
    glassbox(app, channel, min(1.0, tps / 60.0), tps_text(tps) if tps else '')

def load_skills(app):
    """The skill index, from cache when there is one.

    Discovery walks every configured library on every boot, which is why a
    folder added while the app was running never appeared: the answer was
    computed once at startup and nothing could ask again. The cache does not
    change that -- it makes the recomputation an explicit, cheap act
    (/skills refresh) instead of a restart.

    Returns (skills, generated_at). generated_at is 0.0 for a fresh scan,
    which is how /skills knows whether it is showing cached data and how old
    it is: a cache nobody can date is a cache nobody can distrust.
    """
    if not app.settings.skills_enabled:
        return ([], 0.0)
    cached = skills_mod.read_cache(paths.ROOT)
    if cached is not None:
        return cached
    found = skills_mod.discover_all(paths.ROOT, app.settings.skill_roots)
    skills_mod.write_cache(paths.ROOT, found)
    return (found, 0.0)

def append_to_system(app, text: str) -> None:
    """Extend the FIRST system message rather than adding another one.

    Multiple role:"system" turns are not portable. qwen/qwen3.8-27b's chat
    template raises "System message must be at the beginning" and the request
    fails with a 500; other builds of the same model accept it. Anything the
    model must know belongs in the one system turn it is guaranteed to read.
    """
    if not app.conversation or app.conversation[0].get('role') != 'system':
        app._append({'role': 'system', 'content': text})
        return
    current = app.conversation[0].get('content') or ''
    if text in current:
        return
    app.conversation[0] = {**app.conversation[0], 'content': current.rstrip() + '\n\n' + text if current else text}
    if not getattr(app, '_convo_loading', False):
        app._edit(0, 'system prompt extended')

def load_image_file(app, path: Path) -> str | None:
    try:
        from PIL import Image as PILImage
        img = PILImage.open(path)
        if max(img.size) > MAX_IMAGE_DIM:
            img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None

def store_block(app, live: bool=False) -> str:
    parts = []
    for name, cap in (('memory.md', 6000), ('soul.md', 8000), ('handoff.md', 8000)):
        body = app._read_store_file(name, cap)
        if body:
            parts.append(f'### {name} (current contents)\n\n{body}')
    if not parts:
        return ''
    if live:
        return '\n\n## Your store, as it stands right now\n\nRe-read from disk just now.\n\n' + '\n\n'.join(parts) + '\n'
    return '\n\n' + STORE_HEADER + '\n\nThis is a SNAPSHOT taken at the start of the conversation, not a live view, and it is NOT re-sent each turn. If you have written to these files since, or need their current contents, read them with the `read` tool.\n\n' + '\n\n'.join(parts) + '\n'

def append_tps_into(app, t: Text, sep: str) -> None:
    if app.tps is None:
        return
    if t.plain:
        t.append(sep, '#5c6370')
    style = '#e5534b' if app.tps < 5 else '#e8a33d' if app.tps < 15 else '#7d8799'
    t.append(tps_text(app.tps), style)
