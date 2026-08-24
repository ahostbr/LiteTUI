"""Stateless helpers lifted off `LiteTUI` — T070 step O2.

They take `app` as their first parameter because they still READ app state. That
is honest rather than elegant: the coupling is visible in the signature instead
of hidden behind `self`.

🔴 THE MEMBERSHIP RULE, AND IT IS DECIDABLE — WHICH THE FIRST VERSION WAS NOT:

    writes no instance state, AND calls no app method outside PURE.

    PURE = {_read_store_file}

The first version read *"every function here wrote no instance state"* — and
**that is a TRANSITIVE property, while every instrument built to check it was a
LOCAL one.** A direct-write detector clears any function that DELEGATES its
writing, so the cleaner the code the higher its false-clean rate. Two
independently-written detectors both gave `append_to_system` a pass while it
wrote through `app._append` one level down. Widening cannot fix that: no finite
widening discharges an unbounded call chain.

One hop, bounded, checkable. **A call to anything not in PURE puts the function
out of this module BY DEFAULT**, rather than pending a proof nobody can complete.

📌 `_edit` is pure on the measurement and is deliberately NOT on the list: the
only caller that reached it has left. An allowlist entry with no caller is a
licence nobody asked for.

⚠️ THE ALLOWLIST'S SIZE IS THE ACCEPTANCE TEST, NOT A CURIOSITY. Six of the
seven survivors call no app method at all; the seventh calls one pure reader. **A
rule that had needed a long allowlist would have been DESCRIBING the coupling
rather than constraining it**, and would have failed — not succeeded with more
entries.

🔴 FOUR FUNCTIONS WERE RETURNED TO THE CLASS after this module shipped, and the
audit that found them was this contract applied to its own module:
`append_to_system` wrote `app.conversation[0]` (48 read sites in `src/`, and
§5c had already withdrawn `_sync_fleet_identity` permanently for the same
channel — two rulings on one object must agree). `_glassbox` wrote `_gb_last`,
and `_glassbox_tool` / `_glassbox_rate` are one-line wrappers that would have had
to call it. See PLAN §3h.

⚠️ WHAT THIS BUYS: `LiteTUI` loses these methods, so the class method count drops
— the metric T070 exists to move. It does NOT reduce plugin reach-through and it
does not shrink the shared-state knot. See PLAN.md §6.
"""

from __future__ import annotations

import base64
import io
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
    token_count_text,
    tps_text,
)
from litetui import skills as skills_mod
from rich.text import Text

MAX_IMAGE_DIM = 1536
STORE_HEADER = "## Your store, loaded once at the start of this conversation"


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
    # memory.md is capped hardest ON PURPOSE: it is an index, and an index
    # that needs more than this has stopped being one.
    parts = []
    for name, cap in (('memory.md', 6000), ('soul.md', 8000), ('handoff.md', 8000)):
        body = app._read_store_file(name, cap)
        if body:
            parts.append(f'### {name} (current contents)\n\n{body}')
    if not parts:
        return ''
    if live:
        return '\n\n## Your store, as it stands right now\n\nRe-read from disk just now.\n\n' + '\n\n'.join(parts) + '\n'
    # RULING (Ryan, 2026-08-19): injected ONCE, not per turn. Re-sending
    # three files every turn is affordable at 1M context and is NOT on a
    # local 27B, where it crowds out the conversation itself. The text
    # below must not promise a per-turn refresh -- an instruction that
    # quietly stopped being true is worse than no instruction at all.
    return '\n\n' + STORE_HEADER + '\n\nThis is a SNAPSHOT taken at the start of the conversation, not a live view, and it is NOT re-sent each turn. If you have written to these files since, or need their current contents, read them with the `read` tool.\n\n' + '\n\n'.join(parts) + '\n'

def append_tps_into(app, t: Text, sep: str) -> None:
    """The generation-stats field: output tokens, then tok/s.

    ⚠️ THE NAME SAYS tps AND IT NOW CARRIES BOTH. Kept rather than renamed
    because test_footer aliases this symbol and the rename would be churn for
    no behavioural gain — but the docstring has to say so, or the next reader
    trusts the name.

    Ryan asked for the count "next to toks", so it shares `footer_show_tps`
    rather than growing its own toggle: LM Studio prints them as one cluster
    ("17 GEN 2,775 tok") and that is what he is comparing against.

    The OUTPUT half of TpsState's partition — the thinking header shows the
    reasoning half. One counting site, so the two surfaces cannot disagree.
    """
    if app.tps is None:
        return
    if t.plain:
        t.append(sep, '#5c6370')
    stats = getattr(app, '_tps', None)
    produced = getattr(stats, 'content', 0) if stats is not None else 0
    if produced:
        # 0 renders as absence, never "0 tok" — a turn that produced only a
        # tool call generated no prose, and painting a zero claims a measured
        # nothing where there is simply nothing to say.
        t.append(token_count_text(produced), '#7d8799')
        t.append(sep, '#5c6370')
    # Coloured by how it FEELS to use, not by an absolute scale: this is a
    # local model on one GPU, and the number that matters is whether the
    # answer arrives faster than you read it.
    style = '#e5534b' if app.tps < 5 else '#e8a33d' if app.tps < 15 else '#7d8799'
    t.append(tps_text(app.tps), style)
