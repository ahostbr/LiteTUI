"""Reviewed agent execution, subagent-route and identity preferences, one field per review.

Source-owned: every scope, timing, bound and choice below is checked against the
canonical settings registry and validators, never taken from the native client.
Nothing here loads a model, contacts a provider, refreshes an index or starts a
process. The three standing tool-authority lists change only through a review
whose consequence states whether the single entry WIDENS or NARROWS authority,
and they hold admission until their exact saved readback (see mutation_hold).
"""

from __future__ import annotations

import copy
import hashlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from litetui import (
    backend_catalog,
    backend_transition,
    codex_settings,
    native_context,
    native_settings,
    settings,
    settings_runtime,
    tool_context,
    tool_policy,
)
from litetui.settings_scope import SETTING_SPECS

PREFIX = "gui.agent.native_"
MAXIMUM = 2147483647
MAX_TEXT = 256
MAX_NAME = 128
MAX_ENTRY = 512
MAX_ENTRIES = 64
MAX_WIRE = 512 * 1024
AUTHORITY = ("tool_always_allow", "tool_deny", "tool_trusted_interpreters")

# key -> (kind, group, minimum, maximum, choices, scope, apply_timing)
_ROWS = (
    ("tool_iterations", "integer", "execution", 1, MAXIMUM, (), "conversation", "next_turn"),
    ("tool_auto_background_s", "integer", "execution", 0, MAXIMUM, (), "conversation", "next_turn"),
    ("enter_interrupts", "boolean", "execution", None, None, (), "device", "immediate"),
    ("relay_approval_timeout_s", "integer", "execution", 1, MAXIMUM, (), "conversation", "next_turn"),
    ("permission_judge", "choice", "execution", None, None, (None, "count"), "device", "immediate"),
    ("tool_context_mode", "choice", "execution", None, None, tuple(tool_context.MODES), "conversation", "next_turn"),
    ("tool_context_threshold_chars", "integer", "execution", 0, MAXIMUM, (), "conversation", "next_turn"),
    ("tool_always_allow", "list", "authority", None, None, (), "conversation", "next_turn"),
    ("tool_deny", "list", "authority", None, None, (), "conversation", "next_turn"),
    ("tool_trusted_interpreters", "list", "authority", None, None, (), "conversation", "next_turn"),
    ("subagent_route", "route", "routes", None, None, (), "device", "immediate"),
    ("small_task_route", "task_route", "routes", None, None, (), "device", "immediate"),
    ("allow_local_subagents", "boolean", "routes", None, None, (), "device", "immediate"),
    ("subagent_route_override", "override", "routes", None, None, (), "conversation", "next_turn"),
    ("subagent_model", "optional_text", "routes", None, None, (), "conversation", "next_turn"),
    ("tool_summary_model", "optional_text", "routes", None, None, (), "conversation", "next_turn"),
    ("user_name", "text", "identity", None, None, (), "defaults", "next_turn"),
    ("seat_name", "text", "identity", None, None, (), "defaults", "next_turn"),
    ("owner_domain_cap", "integer", "identity", 1, MAXIMUM, (), "conversation", "next_turn"),
    ("skills_enabled", "boolean", "identity", None, None, (), "conversation", "reconnect"),
    ("skill_roots", "list", "identity", None, None, (), "conversation", "reconnect"),
)
SPECS = {
    row[0]: dict(zip(("kind", "group", "minimum", "maximum", "choices", "scope", "timing"), row[1:], strict=True))
    for row in _ROWS
}
FIELDS = tuple(SPECS)


@dataclass
class Review:
    token: str
    instance: str
    revision: str
    field: str
    op: str
    value: object
    proposed: object
    effect: str
    consequence: str
    destination: str
    setting_scope: str
    timing: str
    effective_before: object
    fields: dict = field(default_factory=dict)
    apply_id: str | None = None
    after: str | None = None


def _encoded(value, limit=MAX_WIRE):
    return backend_catalog._encoded(value, limit)


def _same(left, right):
    return _encoded(left) == _encoded(right)


def _text_ok(value, limit):
    return isinstance(value, str) and len(value.encode("utf-8")) <= limit and all(ch.isprintable() for ch in value)


def _route_ok(key, value):
    if value is None:
        return True
    from litetui.small_task_dispatch import Route
    from litetui.subagent_routing import validate_route

    try:
        if key == "small_task_route":
            Route.parse(value)
        else:
            validate_route(value, override=key == "subagent_route_override")
            if value and (len(value["backend"]) > 64 or not _text_ok(value["model"], MAX_NAME * 4)
                          or value["model"] != value["model"].strip()):
                return False
    except (ValueError, TypeError, KeyError):
        return False
    return True


def _value(key, value):
    """A stored value must already be exactly what the native client can render."""
    spec = SPECS[key]
    kind = spec["kind"]
    if kind == "integer":
        valid = type(value) is int and spec["minimum"] <= value <= spec["maximum"]
    elif kind == "boolean":
        valid = type(value) is bool
    elif kind == "choice":
        valid = any(type(value) is type(item) and value == item for item in spec["choices"])
    elif kind == "text":
        valid = _text_ok(value, MAX_NAME)
    elif kind == "optional_text":
        valid = value is None or _text_ok(value, MAX_TEXT)
    elif kind == "list":
        valid = (isinstance(value, list) and len(value) <= MAX_ENTRIES
                 and all(_text_ok(item, MAX_ENTRY) for item in value))
    else:
        valid = _route_ok(key, value)
    if not valid:
        raise ValueError(f"The configured {key} value is outside its supported display format")
    return copy.deepcopy(value)


def _rule(entry, *, allow):
    tool, separator, rest = entry.rpartition(":")
    capabilities = rest.split(",")
    if (not separator or not tool or len(tool.encode("utf-8")) > MAX_NAME
            or any(ch.isspace() or not ch.isprintable() for ch in tool)
            or not all(name in tool_policy.CAPABILITIES for name in capabilities)
            or capabilities != sorted(set(capabilities))):
        raise ValueError("A standing rule is tool:capability[,capability] with known sorted capabilities")
    if allow and tool_policy.DESTRUCTIVE_IRREVERSIBLE in capabilities:
        raise ValueError("A standing approval for irreversible actions can only come from the approval prompt that shows the call")
    return entry


def _interpreter(entry):
    from litetui.agent_store import _unlinked

    path = Path(entry)
    if (not path.is_absolute() or ".." in path.parts or entry.startswith(("\\\\", "//"))
            or os.name == "nt" and not PureWindowsPath(entry).drive):
        raise ValueError("A trusted interpreter is one exact absolute local path")
    try:
        _unlinked(path)
        regular = path.is_file()
    except Exception:  # noqa: BLE001 - any link, access or path fault means no trust
        regular = False
    if not regular:
        raise ValueError("A trusted interpreter must be an existing regular unlinked file")
    return entry


def _root(entry):
    path = Path(os.path.expanduser(entry))
    if not (entry.startswith("~") or path.is_absolute()) or ".." in path.parts:
        raise ValueError("A skills library is an absolute or ~ path without parent steps")
    return entry


def _entry(key, op, entry):
    if (not isinstance(entry, str) or not entry or entry != entry.strip() or not _text_ok(entry, MAX_ENTRY)):
        raise ValueError("A list entry is one bounded, printable, trimmed line")
    if op == "remove":
        return entry  # An existing malformed entry must stay removable.
    if key == "tool_always_allow":
        return _rule(entry, allow=True)
    if key == "tool_deny":
        return _rule(entry, allow=False)
    if key == "tool_trusted_interpreters":
        return _interpreter(entry)
    return _root(entry)


def _edited(key, value):
    spec = SPECS[key]
    kind = spec["kind"]
    if kind == "text" and key == "seat_name" and value:
        from litetui.agent_store import valid_name

        valid_name(value)
    if kind == "text" and (not isinstance(value, str) or value != value.strip()):
        raise ValueError("Names are bounded, printable and trimmed")
    if kind == "optional_text" and value is not None and (not value or value != value.strip()):
        raise ValueError("Use an exact model name, or clear it with an empty entry")
    return _value(key, value)


def _candidate(key, op, value, current):
    if SPECS[key]["kind"] == "list":
        if op not in ("add", "remove"):
            raise ValueError("Lists change by adding or removing one entry")
        entry = _entry(key, op, value)
        if op == "add":
            if entry in current or len(current) >= MAX_ENTRIES:
                raise ValueError("That entry is already present or the list is full")
            return [*current, entry]
        if entry not in current:
            raise ValueError("That entry is not present")
        remaining = list(current)
        remaining.remove(entry)
        return remaining
    if op != "set":
        raise ValueError("This preference is replaced as a whole value")
    return _edited(key, value)


def _effect(key, op, before, after):
    if key in ("tool_always_allow", "tool_trusted_interpreters"):
        return "widens" if op == "add" else "narrows"
    if key == "tool_deny":
        return "narrows" if op == "add" else "widens"
    if key == "allow_local_subagents":
        return "widens" if after else "narrows"
    if key == "small_task_route" and (before is None) != (after is None):
        return "widens" if before is None else "narrows"
    return "none"


def _consequence(key, op, value, before, after, saved):
    if key == "tool_always_allow":
        text = (f"Standing approval {value!r} will let that tool run at exactly that authority without asking. "
                "Deny rules and the deny floor still win. This WIDENS tool authority."
                if op == "add" else
                f"Removes the standing approval {value!r}; matching calls ask again. This NARROWS tool authority.")
    elif key == "tool_deny":
        text = (f"Standing refusal {value!r} is checked before any allow rule or profile. This NARROWS tool authority."
                if op == "add" else
                f"Removes the standing refusal {value!r}; matching calls fall back to the profile and any allow rule. "
                "This WIDENS tool authority.")
    elif key == "tool_trusted_interpreters":
        text = (f"Trusts the interpreter at {value!r}: foreign-program confirmation is skipped for that exact file only. "
                "Danger and deny rules still apply. This WIDENS tool authority."
                if op == "add" else
                f"Stops trusting {value!r}; commands through it confirm again. This NARROWS tool authority.")
    elif key == "allow_local_subagents":
        text = ("Allows expert local subagent dispatch. It still never loads a model for a child. This WIDENS dispatch."
                if after else "Disallows local subagent dispatch. This NARROWS dispatch.")
    elif key == "small_task_route":
        text = ("Enables ordinary named-worker dispatch through the seat launcher on this route. Saving launches nothing. "
                "This WIDENS what a turn may start."
                if before is None else
                "Disables named-worker dispatch." if after is None else
                "Changes the route used for named-worker dispatch. Saving launches nothing.")
    elif key in ("subagent_route", "subagent_route_override"):
        text = "Selects where child calls are routed. Saving loads no model and contacts no provider."
        if key == "subagent_route_override" and after is None and saved.subagent_model:
            text += f" The conversation model {saved.subagent_model!r} still applies when the override inherits."
    elif key == "seat_name":
        text = "The registry is asked for this name the next time this seat registers; the running name is unchanged."
    elif key in ("skills_enabled", "skill_roots"):
        text = ("Saved only. The running skills index is not refreshed. After reconnect, rebuild it with the "
                "reviewed Skills refresh in Work.")
    else:
        text = "Changes this preference only."
    return text[:1000]


def _overridden(app, key):
    return bool(settings.source_of(key) or key in (getattr(app, "_launch_overrides", None) or {})
                or key in (getattr(app, "_invocation_saved_values", None) or {}))


def _reason(app, key, saved, effective, overridden):
    control = codex_settings.control(app.backend, key)
    if control is not None and not control.editable:
        return control.help[:512]
    if env := settings.source_of(key):
        return f"Environment {env} owns this preference"
    if overridden:
        return "A launch option owns this preference for this run"
    if not _same(saved, effective):
        if SPECS[key]["timing"] == "reconnect":
            return "Saved; reconnect is required before this preference can change again"
        return "The saved and running values differ; reopen the conversation before editing"
    return None


def _snapshot(app, binding):
    context = backend_catalog._context(app, binding["session_id"])
    service = settings_runtime.service_for(app)
    before = service.snapshot(context.session_id, read_only=True, max_bytes=1024 * 1024)
    paths, _, revisions = native_settings.owned_documents(service, context.session_id, app._agent_session)
    if revisions != before.revisions or paths["global"] != app._agent_session.store.data_root / "settings.json":
        raise ValueError("Agent preferences changed while inspecting their destinations")
    values = {}
    for key, spec in SPECS.items():
        registry = SETTING_SPECS[key]
        if registry.scope.value != spec["scope"] or registry.apply_timing != spec["timing"]:
            raise ValueError("Agent preference ownership changed")
        saved = _value(key, getattr(before.saved, key))
        effective = _value(key, getattr(app.settings, key))
        overridden = _overridden(app, key)
        reason = _reason(app, key, saved, effective, overridden)
        values[key] = {
            "scope": spec["scope"], "apply_timing": spec["timing"],
            "minimum": spec["minimum"], "maximum": spec["maximum"], "choices": list(spec["choices"]),
            "saved": saved, "effective": effective, "overridden": overridden,
            "editable": reason is None, "reason": reason,
        }
    result = {"kind": "agent_preferences", "version": 1, "scope": "mixed", **binding, "fields": values}
    result["revision"] = hashlib.sha256(_encoded([result, revisions])).hexdigest()
    return result, context, service, before, paths


def _quiet(app):
    backend_transition._quiet(app, require_unowned_server=False)


def _fatal(app):
    app._backend_transition_error = backend_transition.CLOSE_REQUIRED
    app._owned_launch_error = backend_transition.CLOSE_REQUIRED
    app._cli_launch_error = backend_transition.CLOSE_REQUIRED


def _release(app):
    app._native_agent_preferences_hold = False


def hold(app):
    return bool(getattr(app, "_native_agent_preferences_hold", False))


def _receipt(binding, review, kind):
    return {
        "kind": kind, "version": 1, "scope": "mixed", **binding,
        "token": review.token, "field": review.field, "op": review.op, "value": review.value,
        "proposed": review.proposed, "effect": review.effect, "consequence": review.consequence,
        "destination": review.destination, "setting_scope": review.setting_scope,
        "apply_timing": review.timing, "before": review.revision, "after": review.after,
        "apply_id": review.apply_id,
    }


def readback_allowed(app, cmd):
    """The one operation admitted while a saved authority change awaits its readback."""
    review = getattr(app, "_native_agent_review", None)
    if (not hold(app) or not isinstance(review, Review) or review.apply_id is None
            or cmd.get("type") != PREFIX + "readback" or cmd.get("token") != review.token
            or cmd.get("apply_id") != review.apply_id):
        return False
    native_context.read_binding(app, cmd, PREFIX + "readback", "token", "apply_id")
    return True


def dispatch(app, cmd):
    op = cmd["type"].removeprefix(PREFIX)
    arguments = {
        "preferences": (),
        "review": ("revision", "field", "op", "value"),
        "apply": ("token",),
        "readback": ("token", "apply_id"),
    }
    if op not in arguments:
        raise ValueError("Unknown agent preference operation")
    bridge, binding = native_context.read_binding(app, cmd, PREFIX + op, *arguments[op])
    if hold(app) and op != "readback":
        raise ValueError("Await the exact readback of the saved authority preference")
    if op == "preferences":
        app._native_agent_review = None
        return _snapshot(app, binding)[0]
    if op == "review":
        app._native_agent_review = None
        _quiet(app)
        key = cmd["field"]
        if not isinstance(key, str) or key not in SPECS or not isinstance(cmd["op"], str):
            raise ValueError("Unknown agent preference")
        current, _, _, saved, paths = _snapshot(app, binding)
        row = current["fields"][key]
        if cmd["revision"] != current["revision"] or not row["editable"]:
            raise ValueError("Refresh agent preferences and select an editable change")
        proposed = _candidate(key, cmd["op"], cmd["value"], row["effective"])
        if _same(proposed, row["effective"]):
            raise ValueError("Choose a different agent preference")
        spec = SPECS[key]
        effect = _effect(key, cmd["op"], row["effective"], proposed)
        destination = paths["conversation" if spec["scope"] == "conversation" else "global"]
        review = Review(
            secrets.token_hex(32), bridge.instance, current["revision"], key, cmd["op"], cmd["value"], proposed,
            effect, _consequence(key, cmd["op"], cmd["value"], row["effective"], proposed, saved.saved),
            str(destination), spec["scope"], spec["timing"], copy.deepcopy(row["effective"]),
            copy.deepcopy(current["fields"]),
        )
        app._native_agent_review = review
        return _receipt(binding, review, "agent_review")

    review = getattr(app, "_native_agent_review", None)
    if (not isinstance(review, Review) or review.token != cmd["token"]
            or review.instance != bridge.instance):
        raise ValueError("Review the current agent preference again")
    held = review.field in AUTHORITY
    if op == "apply":
        if review.apply_id is not None:
            raise ValueError("Agent preference review was already consumed")
        app._native_agent_review = None
        if held:
            app._native_agent_preferences_hold = True

        def reader(owner, _cmd):
            bridge.check()
            _quiet(owner)
            current, context, service, before, _paths = _snapshot(owner, binding)
            row = current["fields"][review.field]
            if current["revision"] != review.revision or not row["editable"]:
                raise ValueError("Agent preferences changed after review")
            return (
                {
                    "field": review.field,
                    "value": review.proposed,
                    "scope": review.setting_scope,
                    "destination": review.destination,
                    "effective_before": row["effective"],
                },
                context,
                service,
                before,
            )

        def result(_owner, _cmd, status, **details):
            if status == "saved":
                return None
            runtime = details.get("runtime", ())
            if (review.timing == "reconnect" and status == "partial" and len(runtime) == 1
                    and runtime[0].status == "pending" and runtime[0].action == "reconnect"):
                return None
            if details.get("close_required") or details.get("mutated"):
                return "Agent preference publication is uncertain. Close and reopen to inspect it."
            return "Agent preference was refused. Refresh to inspect it."

        try:
            error = native_settings.publish_reviewed(app, cmd, reader, result, lambda k, v: _value(k, v), strict=True)
            if error:
                if not getattr(app, "_backend_transition_error", None):
                    _release(app)
                raise ValueError(error)
            after = _snapshot(app, binding)[0]
            row = after["fields"][review.field]
            expected_effective = review.effective_before if review.timing == "reconnect" else review.proposed
            if (after["revision"] == review.revision or not _same(row["saved"], review.proposed)
                    or not _same(row["effective"], expected_effective)
                    or any(not _same(after["fields"][k], v) for k, v in review.fields.items() if k != review.field)):
                raise ValueError("Agent preference did not match its durable and effective readback")
        except BaseException:
            if held and hold(app):
                _fatal(app)
            raise
        review.after, review.apply_id = after["revision"], cmd["id"]
        app._native_agent_review = review
        return _receipt(binding, review, "agent_applied")

    if review.apply_id is None or review.apply_id != cmd["apply_id"]:
        raise ValueError("Agent preference readback belongs to another application")
    app._native_agent_review = None
    current = _snapshot(app, binding)[0]
    if review.after != current["revision"]:
        if held and hold(app):
            _fatal(app)
        raise ValueError("Agent preferences changed after saving. Refresh to inspect them.")
    if held:
        _release(app)
    return {**_receipt(binding, review, "agent_saved"), "preferences": current}
