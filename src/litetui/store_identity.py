"""Descriptive store provenance, never settings or launch authority.

New prompts record mode and folder as data. Legacy prompts are readable only
when their complete host address occupies a known composition slot; arbitrary
index and memory bodies are never searched for identity markers.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from litetui.owner_mode import OwnerModeError

UNRECORDED = object()


def capture(app) -> dict:
    owner = getattr(app, "_owner_memory_dir", None)
    session = getattr(app, "_agent_session", None)
    root = owner if owner is not None else session.memory_root if session is not None else app.convo_dir
    return {"version": 1, "mode": "owner" if owner is not None else "home", "folder": str(root)}


def _validated(value) -> dict:
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 1 or value.get("mode") not in ("home", "owner")
            or not isinstance(value.get("folder"), str) or not Path(value["folder"]).is_absolute()):
        raise ValueError("recorded store provenance is invalid or unsupported")
    return value


def _host_prefixes(app, *, native: bool) -> list[str]:
    """Exact BASE/[PLAN] prefixes preceding the host MEMORY slot.

    No scanning: each candidate is matched at offset zero. Both host plan modes
    are recognized, but an unknown pre-memory plugin or changed base is not.
    Native launch text is recognized only when the same launch text is supplied.
    """
    from litetui.plugins import PROMPT_ORDER

    bases = [""]
    for section in app.plugins.sections_sorted():
        if section.order >= PROMPT_ORDER["MEMORY"]:
            break
        if section.owner != "host" or section.order not in (PROMPT_ORDER["BASE"], PROMPT_ORDER["PLAN"]):
            return []
        enabled = section.enabled is None or section.enabled()
        if section.order == PROMPT_ORDER["PLAN"]:
            try:
                rendered = section.render()
            except OSError:
                continue
            bases += [(base + rendered).strip() for base in bases]
        elif enabled:
            bases = [(base + section.render()).strip() for base in bases]
    launches = tuple(dict.fromkeys((*getattr(app, "_legacy_launch_candidates", ()), getattr(app, "_cli_system_prompt", None)))) if native else ()
    prefixes = [(base + "\n\n" if base else "") for base in bases]
    prefixes += [launch + "\n\n" + (base + "\n\n" if base else "")
                 for launch in launches if isinstance(launch, str) and launch for base in bases]
    from litetui.seat_prompts import launch_text
    composed_launch = launch_text(app)
    if composed_launch:
        prefixes += [composed_launch + "\n\n" + (base + "\n\n" if base else "") for base in bases]
    return prefixes


def legacy_identity(app, prompt: str, *, native: bool = False) -> dict:
    """Read the full generated address only at the known host slot.

    Prefix/template drift and custom or preset native prompts cannot establish
    provenance. Nothing is migrated or written back when this read succeeds.
    """
    from litetui.textfmt import memory_prompt

    for prefix in _host_prefixes(app, native=native):
        if not prompt.startswith(prefix):
            continue
        for owner in (False, True):
            template = memory_prompt("CONVO_ID_SLOT", Path("STORE_ROOT_SLOT"), owner=owner).strip()
            pattern = re.escape(template).replace("CONVO_ID_SLOT", r"[^`\r\n]+")
            pattern = pattern.replace("STORE_ROOT_SLOT", r"(?P<folder>[^\r\n]+?)", 1)
            pattern = pattern.replace("STORE_ROOT_SLOT", r"(?P=folder)")
            match = re.match(pattern, prompt[len(prefix):])
            if match is not None:
                return _validated({"version": 1, "mode": "owner" if owner else "home",
                                   "folder": match["folder"]})
    raise ValueError("legacy prompt has no verifiable host store slot")


def launch_message_matches(app, content: str) -> bool:
    """Recognize only the supplied launch and its first generated suffix opening.

    This proves only that the message begins as the host composed it. Everything
    after that opening is opaque, never parsed for identity, section order or cuts.
    """
    supplied = getattr(app, "_cli_system_prompt", None)
    launches = tuple(dict.fromkeys((*getattr(app, "_legacy_launch_candidates", ()), supplied)))
    return any(_launch_content_matches(app, launch, content) for launch in launches if isinstance(launch, str) and launch)


def _launch_content_matches(app, launch: str, content: str) -> bool:
    from litetui.appsvc import FLEET_OPENING, INDEX_OPENING, LORE_OPENING, STORE_OPENING, extend_system_text
    if not isinstance(content, str):
        return False
    if content.startswith(extend_system_text(launch, FLEET_OPENING)):
        return True
    if not content.startswith(launch):
        return False
    suffix = content[len(launch):]
    if not suffix:
        return True
    index_opening = re.escape(INDEX_OPENING).replace(re.escape("{where}"), r"[^\r\n]+")
    if re.match(index_opening, suffix):
        return True
    if suffix.startswith(LORE_OPENING):
        return True
    names = (("SOUL.md", "NOW.md", "DOMAIN.md") if getattr(app, "_owner_memory_dir", None) is not None
             else ("memory.md", "soul.md", "handoff.md"))
    return any(suffix.startswith(STORE_OPENING + f"### {name} (current contents)\n\n") for name in names)


def transcript_store_match(app, recorded=UNRECORDED, *, messages: list[dict], convo_id: str) -> dict:
    """Read either host slot zero or the exact shipped launch+host pair, never later."""
    prompt = messages[0].get("content", "") if messages and messages[0].get("role") == "system" else ""
    if recorded is UNRECORDED and len(messages) > 1 and messages[1].get("role") == "system":
        prompt = messages[1].get("content", "") if launch_message_matches(app, prompt) else ""
    return require_store_match(app, recorded, prompt=prompt if isinstance(prompt, str) else "", convo_id=convo_id)


def require_store_match(app, recorded=UNRECORDED, *, prompt: str, convo_id: str, native: bool = False) -> dict:
    """Compare data, using the bounded legacy reader only when data is absent."""
    expected = capture(app)
    session = getattr(app, "_agent_session", None)
    name = session.authority.name if session is not None else "LiteTUI"
    command = ["litetui"]
    try:
        saved = legacy_identity(app, prompt, native=native) if recorded is UNRECORDED else _validated(recorded)
    except ValueError as exc:
        reason = str(exc)
        if expected["mode"] == "owner":
            command += ["--owner", name, "--memory", expected["folder"]]
        else:
            command += ["--agent", name]
        remedy = "start a new conversation (omit --convo) with"
    else:
        if (saved["mode"] == expected["mode"]
                and os.path.normcase(os.path.abspath(saved["folder"]))
                == os.path.normcase(os.path.abspath(expected["folder"]))):
            return saved
        reason = "saved conversation and launch use different memory stores"
        if saved["mode"] == "home":
            command += ["--agent", name]
            remedy = f"select the saved home {saved['folder']} and resume without --owner and --memory using"
        else:
            command += ["--owner", name, "--memory", saved["folder"]]
            remedy = "resume with the saved owner flags using"
        command += ["--convo", convo_id]
    rendered = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    raise OwnerModeError(f"Store mismatch: {reason}; {remedy}: {rendered}")
