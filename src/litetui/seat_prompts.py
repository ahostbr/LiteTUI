"""One role, one method, one seat-home prompt layer.

Prompt text is editable guidance; tier and executable authority remain launch
state. No cross-checkout imports or guessed method-wrapper splitting.
"""
from __future__ import annotations

import re
from pathlib import Path

from litetui import prompt_overrides as prompts

ALIASES = {"owner": "leader", "builder": "worker", "critic": "reviewer", "scout": "thinker"}
DISPLAY_ROLES = {"leader": "owner", "worker": "builder", "reviewer": "critic", "thinker": "scout", "orchestrator": "orchestrator"}
ROLES = frozenset({"orchestrator", "leader", "worker", "thinker", "reviewer"})
ROLE_HEADER = re.compile(r"^\ufeff*#\s+([A-Z]+) ROLE[^\n]*$", re.MULTILINE)
MODE_HEADER = re.compile(r"^\ufeff*#\s+POLYMATHIC[^\n]*\s[\u2014-]\s[^\n]*Mode[^\n]*$", re.MULTILINE | re.IGNORECASE)
METHOD_HEADER = re.compile(r"^\ufeff*#\s+POLYMATHIC[^\n]*$", re.MULTILINE)


def authored_text(text: str) -> str:
    """One UTF-8 encoding BOM is not authored role/method content.

    This is a parsing/rendering view only. Legacy source matching and managed
    publication hashes keep their raw text, including any encoding marker.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
        if text.startswith("\ufeff"):
            raise prompts.PromptError("Multiple leading encoding BOMs are refused")
    return text


def role_name(value: str) -> str:
    role = ALIASES.get(value, value)
    if role not in ROLES:
        raise prompts.PromptError(f"Unknown actual seat role: {value}")
    return role


def home(app) -> Path | None:
    session = getattr(app, "_agent_session", None)
    if session is not None:
        # The agent HOME, never its externally selected owner memory folder.
        return session.memory_root
    return getattr(app, "convo_dir", None)  # legacy non-owned readers only


def role_body(text: str, role: str) -> str:
    text = authored_text(text)
    headers = list(ROLE_HEADER.finditer(text))
    if len(headers) > 1 or MODE_HEADER.search(text):
        raise prompts.PromptError("Duplicate or embedded role-mode scaffold refused; use method-only text")
    if headers:
        declared = role_name(headers[0][1].lower())
        if declared != role:
            raise prompts.PromptError(f"Role scaffold {declared} disagrees with actual role {role}")
        # Canonical preambles have a generated-source HTML comment before the header.
        # It is metadata, not another role body.
        prefix = text[:headers[0].start()].strip()
        if prefix and not re.fullmatch(r"<!--.*?-->", prefix, re.DOTALL):
            raise prompts.PromptError("Unexpected text before role scaffold")
        text = text[headers[0].end():]
    if METHOD_HEADER.search(text):
        raise prompts.PromptError("Role body contains a polymath method; select method separately")
    return text.strip()


def method_text(source: Path) -> str:
    source = prompts.checked(source)
    if not source.is_file():
        raise prompts.PromptError(f"Selected cognitive source does not exist: {source}")
    # The shipped catalog has explicit pure counterparts, including Miyamoto
    # whose method has no POLYMATHIC heading. Never infer a split from headings.
    if source.parent.name in {"workers", "thinkers", "reviewers", "leaders", "orchestrator", "pure"}:
        pure = prompts.checked(source.parent.parent / "pure" / source.name)
        if pure.is_file():
            source = pure
    text = authored_text(prompts.read_text(source))
    validate_method(text, selected=True)
    return text


def method_selected(app) -> bool:
    if getattr(app, "_seat_method_selected", False):
        return True
    snapshot = getattr(app, "_prompt_snapshot", None)
    if snapshot is not None and authored_text(snapshot.texts.get("method.md", "")).strip():
        return True
    root = home(app)
    path = prompts.checked(root / "prompts" / "method.md") if root is not None else None
    return path is not None and path.is_file() and bool(authored_text(prompts.read_text(path)).strip())


def validate_method(text: str, *, selected: bool = False) -> None:
    text = authored_text(text)
    if selected and not text.strip():
        raise prompts.PromptError("Selected polymath method cannot be blank")
    if ROLE_HEADER.search(text) or MODE_HEADER.search(text) or len(METHOD_HEADER.findall(text)) > 1:
        raise prompts.PromptError("Cognitive source must be one method without role-mode scaffold; select pure catalog file")


def prepare(app, *, role_file: str | None = None, cognitive_file: str | None = None,
            directory: str | None = None) -> None:
    """Seed only after AgentSession lease acquisition, before first composition.

    A prepared home is read directly on subsequent launches. Explicit inputs
    replace their named files; a full-folder seed is intentionally exclusive.
    No home creation or launch identity is inferred from prompt contents.
    """
    from litetui.prompt_commands import publish

    custom = getattr(app, "_cli_system_prompt", None)
    if directory and any(value is not None for value in (role_file, cognitive_file, custom)):
        raise prompts.PromptError("--prompts-dir cannot overlap role, system or cognitive launch inputs")
    session = getattr(app, "_agent_session", None)
    if session is None:
        if role_file or cognitive_file or directory:
            raise prompts.PromptError("Seat prompt sources require an owned agent home")
        return
    root = home(app)
    assert root is not None  # acquired AgentSession owns this home
    app._seat_prompt_role = role_name(app.seat.tier)
    selected = {}
    if directory:
        source = prompts.checked(Path(directory).expanduser().absolute())
        if not source.is_dir():
            raise prompts.PromptError("--prompts-dir must be an existing directory")
        for file in source.iterdir():
            if not file.is_file():
                raise prompts.PromptError("Prompt seed must contain only prompt files")
            prompts.mutable(file.name)
            selected[file.name] = prompts.read_text(file)
    else:
        if custom is not None:
            selected["role.md"] = custom
        elif role_file:
            selected["role.md"] = prompts.read_text(Path(role_file).absolute())
        if cognitive_file:
            selected["method.md"] = method_text(Path(cognitive_file).absolute())
    if "role.md" in selected:
        selected["role.md"] = role_body(selected["role.md"], app._seat_prompt_role)
    if "method.md" in selected:
        if not selected["method.md"].strip():
            raise prompts.PromptError("Selected cognitive method is empty")
        validate_method(selected["method.md"], selected=True)
    for name, text in selected.items():
        if len(text.encode("utf-8")) > prompts.MAX_BYTES or "\0" in text:
            raise prompts.PromptError(f"Launch prompt {name} is oversized or contains NUL")
    import json
    launch_manifest = prompts.checked(root / ".prompt-launch.json")
    supplied = {name: prompts.sha(text) for name, text in selected.items()}
    recorded = {}
    if launch_manifest.exists():
        recorded = json.loads(prompts.read_text(launch_manifest))
        if not isinstance(recorded, dict) or any(name not in prompts.MUTABLE or not isinstance(digest, str)
                                               or not re.fullmatch(r"[0-9a-f]{64}", digest)
                                               for name, digest in recorded.items()):
            raise prompts.PromptError("Invalid per-home launch-source manifest")
        if any(recorded.get(name) != digest for name, digest in supplied.items()):
            raise prompts.PromptError("Launch prompt source changed for an initialized home; use owner prompt command or a new agent home")
        # Repeated producer defaults must not erase live edits on restart.
        selected = {}
    app._legacy_launch_candidates = legacy_launches(custom, role_file, cognitive_file)
    folder = prompts.checked(root / "prompts")
    folder.mkdir(exist_ok=True)
    # Full catalog is per-home even before the first conversation materializes.
    # Global fallbacks seed missing files, not a half-created agent home.
    from litetui import paths
    for name in sorted(prompts.MUTABLE):
        destination = prompts.checked(folder / name)
        if name in selected:
            if prompts.metadata(root, name) is not None:
                raise prompts.PromptError(f"Managed prompt {name} already exists; use owner prompt command to revise it")
            publish(destination, selected[name])
        elif not destination.exists():
            global_path = paths.SYSTEM_PROMPT_FILE if name == "systemprompt.md" else paths.prompt_file(name)
            publish(destination, prompts.read_text(global_path) if global_path.is_file() else "")
    if not launch_manifest.exists():
        publish(launch_manifest, json.dumps(supplied, sort_keys=True))
    app._prompt_snapshot = None
    prompts.refresh(app)
    app._prompt_revision_at_launch = revision(app)
    app._prompt_baseline_texts = dict(app._prompt_snapshot.texts)
    app._seat_method_selected = ("method.md" in supplied or "method.md" in recorded
                                 or bool(authored_text(app._prompt_snapshot.texts["method.md"]).strip()))
    app._seat_prompt_policy = (any(value is not None for value in (role_file, cognitive_file, custom, directory))
                               or bool(authored_text(app._prompt_snapshot.texts["role.md"]).strip())
                               or bool(authored_text(app._prompt_snapshot.texts["method.md"]).strip()))


def launch_text(app) -> str:
    if not hasattr(app, "_seat_prompt_role"):
        return getattr(app, "_cli_system_prompt", None) or ""
    snapshot = getattr(app, "_prompt_snapshot", None) or prompts.refresh(app)
    role = role_name(app.seat.tier)
    if role != app._seat_prompt_role:
        raise prompts.PromptError("Actual role changed after prompt initialization")
    for name, text in snapshot.texts.items():
        text = authored_text(text)
        if name not in {"role.md", "method.md"} and (
                ROLE_HEADER.search(text) or MODE_HEADER.search(text) or METHOD_HEADER.search(text)):
            raise prompts.PromptError(f"Role/method scaffold belongs only in role.md/method.md, not {name}")
    body = role_body(snapshot.texts["role.md"], role)
    method = authored_text(snapshot.texts["method.md"]).strip()
    validate_method(method, selected=method_selected(app))
    if method:
        app._seat_method_selected = True
    # Empty method is supported for ordinary unselected interactive seats.
    # A selected source is required to be nonempty at the launch boundary.
    return "\n\n".join(p for p in (f"# {DISPLAY_ROLES[role].upper()} ROLE \u2014 {DISPLAY_ROLES[role].title()} Mode", body, method) if p)


def legacy_launches(custom, role_file, cognitive_file) -> tuple[str, ...]:
    """Exact catalog-derived former argv compositions, never a saved-text scan."""
    preamble = custom if custom is not None else prompts.read_text(Path(role_file).absolute()) if role_file else ""
    methods = [""]
    if cognitive_file:
        path = Path(cognitive_file).absolute()
        methods = [prompts.read_text(path)]
        if path.parent.name in {"pure", "workers", "thinkers", "reviewers", "leaders", "orchestrator"}:
            for role in ("workers", "thinkers", "reviewers", "leaders", "orchestrator"):
                wrapped = prompts.checked(path.parent.parent / role / path.name)
                if wrapped.is_file():
                    methods.append(prompts.read_text(wrapped))
    return tuple(dict.fromkeys((preamble + "\n\n" if preamble and method else preamble) + method
                               for method in methods if preamble or method))


def rehydrate(app) -> bool:
    """Migrate a proved owned launch prefix, preserving opaque frozen suffix bytes.

    Store provenance has already been checked by _resume. No archive activation,
    heuristic heading cuts, or memory/lore reads happen here.
    """
    from types import SimpleNamespace

    from litetui.store_identity import launch_message_matches
    messages = app.conversation
    if not messages or messages[0].get("role") != "system":
        return False
    first = messages[0]
    if first.get("litetui_prompt_version") == 2:
        return isinstance(first.get("litetui_prompt_base"), str) and first["content"].startswith(first["litetui_prompt_base"])
    candidates = getattr(app, "_legacy_launch_candidates", ())
    for launch in candidates:
        reader = SimpleNamespace(_cli_system_prompt=launch, _owner_memory_dir=getattr(app, "_owner_memory_dir", None))
        if not launch_message_matches(reader, first.get("content", "")):
            continue
        suffix = first["content"][len(launch):]
        remove = 1
        if len(messages) > 1 and messages[1].get("role") == "system":
            host = messages[1]
            old_base = host.get("litetui_prompt_base")
            if not isinstance(old_base, str):
                old_base = app._with_ordinary_instructions(app.plugins.compose_prompt()) + (
                    "\n\nEnd each FINAL answer with <recap>two short lines: what you did and result, about 40 tokens</recap>. Do not put this tag in interim tool calls.")
            if not isinstance(host.get("content"), str) or not host["content"].startswith(old_base):
                raise prompts.PromptError("Legacy host prefix cannot be proved; launch migration refused")
            suffix += host["content"][len(old_base):]
            remove = 2
        base = app._compose_system_text()
        messages[:remove] = [{"role": "system", "content": base + suffix,
                              "litetui_prompt_owner": 1, "litetui_prompt_version": 2,
                              "litetui_prompt_base": base}]
        app._snapshot("owned exact launch prefix canonicalized; frozen suffix preserved")
        return True
    return not candidates and not any(m.get("role") == "system" for m in messages[1:])


def set_system_body(app, text: str) -> None:
    """The existing human /system door changes body, never actual role authority."""
    from uuid import uuid4

    from litetui import prompt_commands
    root = home(app)
    if root is None:
        raise prompts.PromptError("System body update requires an owned home")
    role_body(text, role_name(app.seat.tier))
    command = {"type": prompt_commands.APPLY, "id": str(uuid4()), "action": "set",
               "seat": app.seat.agent_id, "convo": app.convo_id, "name": "role.md",
               "reason": "direct human /system command", "text": text,
               "global_sha": prompts.sha(""), "actor": app.seat.agent_id, "notification_sent": None}
    prompt_commands.apply(root, command, actor_provenance="direct human command in owning app")
    prompt_commands.apply(root, {**command, "notification_sent": True})
    prompts.refresh(app)


COMPOSER_POLICY_VERSION = 2


def revision(app, *, snapshot=None) -> str:
    snapshot = snapshot or getattr(app, "_prompt_snapshot", None) or prompts.refresh(app)
    tier = getattr(getattr(app, "seat", None), "tier", None)
    role = role_name(tier) if tier is not None else "unbound"
    prefix = f"litetui-seat-composer/{COMPOSER_POLICY_VERSION}\0role={role}\0"
    return prompts.sha(prefix + "\0".join(name + "\0" + text for name, text in sorted(snapshot.texts.items())))
