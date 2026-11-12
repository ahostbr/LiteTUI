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

    A prepared home retains live edits across default-only launches. Explicit
    custom role input is an audited revision; other sources seed once and remain
    pinned. A full-folder seed is intentionally exclusive.
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
    explicit_role = selected.pop("role.md") if custom is not None else None
    recorded = {}
    role_source = ("seed" if directory else "custom" if custom is not None else "default") if "role.md" in supplied else "none"
    if launch_manifest.exists():
        manifest = json.loads(prompts.read_text(launch_manifest))
        recorded_role_source = None  # Legacy hash-only manifests cannot prove source origin.
        recorded = manifest
        if isinstance(manifest, dict) and "version" in manifest:
            if (set(manifest) != {"version", "sources", "role_source"}
                    or type(manifest["version"]) is not int or manifest["version"] != 2
                    or manifest["role_source"] not in ("custom", "default", "seed", "none")):
                raise prompts.PromptError("Invalid per-home launch-source manifest")
            recorded = manifest["sources"]
            recorded_role_source = manifest["role_source"]
        if not isinstance(recorded, dict) or any(name not in prompts.MUTABLE or not isinstance(digest, str)
                                               or not re.fullmatch(r"[0-9a-f]{64}", digest)
                                               for name, digest in recorded.items()):
            raise prompts.PromptError("Invalid per-home launch-source manifest")
        if recorded_role_source is not None and ((recorded_role_source == "none") != ("role.md" not in recorded)):
            raise prompts.PromptError("Invalid per-home launch-source manifest")
        for name, digest in supplied.items():
            # Explicit custom text is an intentional audited role revision. A
            # producer default remains fallback, never a replacement for that body.
            # All supplied role text was checked against actual tier above.
            if name == "role.md" and (custom is not None or (
                    role_file and not directory and recorded_role_source in ("custom", "seed"))):
                continue
            if recorded.get(name) != digest:
                raise prompts.PromptError("Launch prompt source changed for an initialized home; use owner prompt command or a new agent home")
        # Repeated producer defaults must not erase live edits on restart.
        selected = {}
    app._legacy_launch_candidates = legacy_launches(custom, role_file, cognitive_file)
    folder = prompts.checked(root / "prompts")
    # Validate other seed destinations before an explicit role revision. Publication
    # uncertainty must reach the shared audit barrier before any seed file writes.
    for name in selected:
        if prompts.metadata(root, name) is not None:
            raise prompts.PromptError(f"Managed prompt {name} already exists; use owner prompt command to revise it")
    if explicit_role is not None:
        assert custom is not None  # explicit_role was selected only from custom input
        _apply_launch_role(app, root, explicit_role, custom)
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
    if explicit_role is not None or not launch_manifest.exists():
        sources = {**recorded, **supplied} if explicit_role is not None else supplied
        publish(launch_manifest, json.dumps({"version": 2, "sources": sources, "role_source": role_source}, sort_keys=True))
    app._prompt_snapshot = None
    prompts.refresh(app)
    app._prompt_revision_at_launch = revision(app)
    app._prompt_baseline_texts = dict(app._prompt_snapshot.texts)
    app._seat_method_selected = ("method.md" in supplied or "method.md" in recorded
                                 or bool(authored_text(app._prompt_snapshot.texts["method.md"]).strip()))
    app._seat_prompt_policy = (any(value is not None for value in (role_file, cognitive_file, custom, directory))
                               or bool(authored_text(app._prompt_snapshot.texts["role.md"]).strip())
                               or bool(authored_text(app._prompt_snapshot.texts["method.md"]).strip()))


def _apply_launch_role(app, root: Path, text: str, input_text: str) -> None:
    """Use the on-fly publication/audit path, under the already acquired HOME lease."""
    import sys
    from uuid import uuid4

    from litetui import paths, prompt_commands

    authority = app._agent_session.authority
    parent = getattr(app.seat, "spawned_by", None)  # captured before launch-env scrubbing
    global_file = paths.prompt_file("role.md")
    global_text = prompts.read_text(global_file) if global_file.is_file() else ""
    command = {"type": prompt_commands.APPLY, "id": str(uuid4()), "action": "set",
               "seat": authority.agent_id, "convo": getattr(app, "_cli_convo_id", None),
               "name": "role.md", "reason": "Explicit launch body before first composition",
               "text": text, "global_sha": prompts.sha(global_text),
               "actor": parent or authority.agent_id, "notification_sent": None}
    # Validate the RAW launch body too: scaffold stripping cannot hide oversized
    # metadata/comments or NUL from the shared public mutation contract.
    prompt_commands.validate({**command, "text": input_text}, applying=True)
    prompt_commands.validate(command, applying=True)
    provenance = {"version": 1, "route": "owned-launch-role",
                  "actor_kind": "launch-envelope parent assertion" if parent else "unparented OS-user launch; actor is home identity",
                  "home_owner": authority.agent_id, "actual_role": app._seat_prompt_role,
                  "source": "explicit system-prompt body (literal or UTF-8 file)",
                  "input_text_sha256": prompts.sha(input_text), "body_sha256": prompts.sha(text),
                  "authentication": "acquired HOME lease; not private-RPC actor authentication",
                  "notification": "not sent; before first composition"}
    result, _ = prompt_commands.apply(root, command, actor_provenance=provenance)
    app._launch_prompt_update = result
    print(f"Launch role prompt published: request {command['id']}; before first composition; no notification sent", file=sys.stderr)


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
    # NEW explicit launch text need not equal the old transcript prefix. The
    # supplied default is also an exact known historical source, never a heading cut.
    preambles = [custom] if custom is not None else []
    if role_file:
        preambles.append(prompts.read_text(Path(role_file).absolute()))
    if not preambles:
        preambles = [""]
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
                               for preamble in preambles for method in methods if preamble or method))


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
    prompt_commands.validate(command, applying=True)
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
