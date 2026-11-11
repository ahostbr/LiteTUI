"""One request's exact standing approval scope and independent saved-rule proof."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from litetui import settings_runtime, tool_policy
from litetui.settings_service import SettingChange

LIMIT = 512 * 1024


def _encoded(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > LIMIT:
        raise ValueError("Standing approval state exceeds its bound")
    return data


def _identity(app):
    session = app._agent_session
    from litetui.owned_storage import require_conversation
    require_conversation(app.convo_dir, session)
    return (id(session), str(app.convo_dir), _encoded(asdict(session.authority)), id(app.settings), getattr(app, "_active_tool_profile", None))


def _bindings(app, name):
    return tuple((id(item), id(item.run), id(item.policy)) for item in app.plugins.tools if item.name == name)


@dataclass
class Scope:
    wire: dict
    identity: tuple
    bindings: tuple
    revisions: dict
    rules: list[str]
    service: Any
    selected: bool = False


def capture(app, ident, name, capabilities):
    """Inspection only. Legacy/non-owned hosts keep their existing two choices."""
    identity = _identity(app)
    if not isinstance(name, str) or not name or len(name.encode()) > 256:
        raise ValueError("Invalid standing approval tool")
    caps = sorted(capabilities)
    if not caps or len(caps) > 32 or any(not isinstance(c, str) or not c or len(c.encode()) > 128 for c in caps):
        raise ValueError("Invalid standing approval capabilities")
    service = settings_runtime.service_for(app)
    snapshot = service.snapshot(app.convo_dir.name, read_only=True, max_bytes=LIMIT)
    rules = list(app.settings.tool_always_allow)
    if rules != snapshot.saved.tool_always_allow or len(rules) > 1024:
        raise ValueError("Standing approval settings differ from saved state")
    key = tool_policy.rule_key(name, caps)
    wire = {"version": 1, "tool": name, "capabilities": caps, "rule": key,
            "conversation_id": app.convo_dir.name, "scope": "conversation"}
    wire["snapshot"] = hashlib.sha256(_encoded([ident, wire, snapshot.revisions, rules])).hexdigest()
    scope = Scope(wire, identity, _bindings(app, name), dict(snapshot.revisions), rules, service)
    registry = getattr(app, "_native_approval_remember", None)
    if registry is None:
        registry = app._native_approval_remember = {}
    if len(registry) >= 100 or ident in registry:
        raise ValueError("Standing approval lifecycle bound exceeded")
    registry[ident] = scope
    return deepcopy(wire)


def validate(app, ident, wire):
    scope = getattr(app, "_native_approval_remember", {}).get(ident)
    if scope is None or wire != scope.wire or scope.selected:
        raise ValueError("Standing approval does not match the reviewed request")
    if _identity(app) != scope.identity or _bindings(app, wire["tool"]) != scope.bindings:
        raise ValueError("Standing approval ownership or tool changed")
    now = scope.service.snapshot(app.convo_dir.name, read_only=True, max_bytes=LIMIT)
    if now.revisions != scope.revisions or app.settings.tool_always_allow != scope.rules:
        raise ValueError("Standing approval settings changed; request again")
    return scope


def select(app, ident, wire):
    scope = validate(app, ident, wire)
    scope.selected = True


def finish(app, ident):
    """Called after the existing authority settled, before a tool may proceed."""
    scope = getattr(app, "_native_approval_remember", {}).get(ident)
    if scope is None or not scope.selected:
        return None
    # Recheck everything after the asynchronous authority waiter resumes.
    scope.selected = False
    validate(app, ident, scope.wire)
    scope.selected = True
    rules = list(scope.rules)
    if scope.wire["rule"] not in rules:
        rules.append(scope.wire["rule"])
    result = scope.service.save_patch(app.convo_dir.name,
        [SettingChange("tool_always_allow", rules, "conversation")], scope.revisions,
        max_bytes=LIMIT)
    # Source semantics: failed persistence still grants this exact rule in this session.
    app.settings.tool_always_allow = list(rules)
    saved = False
    try:
        readback = scope.service.snapshot(app.convo_dir.name, read_only=True, max_bytes=LIMIT)
        saved = (result.fully_saved and readback.saved.tool_always_allow == rules
                 and readback.effective.tool_always_allow == rules)
    except (OSError, ValueError):
        pass
    if not saved:
        app._system("Always-allow rule is session-only; saved-rule verification failed.")
    return {"scope": deepcopy(scope.wire), "saved": saved, "session_only": not saved}


def close(app, ident):
    getattr(app, "_native_approval_remember", {}).pop(ident, None)
