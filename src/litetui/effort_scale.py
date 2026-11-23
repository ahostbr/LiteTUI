"""A served model's reasoning-effort scale, read from its own chat template (T1398).

Ryan, 2026-11-23: *"allow both manual number and presets low med high and xhigh with associated
to diffrent numbers ... DO THIS CORRECTLY BRO EVERY OTHER OPTION U LISTED WAS A PATCH OR HACK"*.

THE TEMPLATE IS THE AUTHORITY. Three facts decide what a turn sends, and no backend or model name
appears in any rule below:

  names        the efforts the served template accepts (probed through the engine, or its map's keys)
  numbers      the template's own name -> number map, when it has one. Inkling:
               ``{"none": 0.0, "minimal": 0.1, "low": 0.2, "medium": 0.7, "high": 0.9, "xhigh": 0.99,
               "max": 0.99}``; DeepSeek-V4.1: ``{"low": 50, "high": 75, "max": 100}``; Qwen3.8: none.
  number_wire  whether this server carries a NUMBER to the template (probed: `discover_scale`).

A preset is sent as its name, verbatim. A manual number is sent as a number when the server carries
one, else as the name the template maps to exactly that number (Inkling 0.1 -> "minimal": the template
prints the same line, so it is an equivalence, not a fold), else it is refused.

A name the model does not take (saved under another model) goes through ONE nearest-accepted rule,
NInfer's own (ninfer-all src/models/qwen3_5/frontend/chat_template.cpp:83-103: ascending order
minimal..max, nearest by distance, a tie rounds UP), computed from this model's accepted names, and the
UI says so. It is never a fixed table.

Pure module: no Textual, no app. Network discovery takes a transport so tests can fake it.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

#: NInfer's ascending effort order (chat_template.cpp:84-85). Index distance is "nearness".
EFFORT_ORDER: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")
#: The wire spelling of "thinking off"; the UI calls it "off".
NONE = "none"
#: Every name a template can be asked about.
KNOWN_NAMES: tuple[str, ...] = (NONE, *EFFORT_ORDER)

#: Where a resolved effort goes on the wire.
TOP = "top"          # body["reasoning_effort"] (every engine reads it)
KWARGS = "kwargs"    # body["chat_template_kwargs"]["reasoning_effort"] (raw JSON into the template)


def is_number(value) -> bool:
    """A JSON number LiteTUI accepts as a manual effort: finite and >= 0. A bool is not a number."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def as_level(value):
    """A stored level as the resolver reads it: numeric TEXT is the number. A manual number travels as
    its text through the string-typed layers (the owned agent authority, the conversation's settings
    file, --thinking-level); no level name is numeric, so the reading is unambiguous."""
    if isinstance(value, str):
        text = value.strip()
        try:
            number = int(text) if text.isdigit() else float(text)
        except ValueError:
            return value
        return number if is_number(number) else value
    return value


def nearest_accepted(requested: str, accepted) -> str:
    """NInfer's rule, exactly (chat_template.cpp:91-103): accepted -> itself; else the nearest accepted
    effort by position in EFFORT_ORDER, checking UP before down at each distance; a name outside the
    order, or no accepted effort, keeps the request."""
    graded = [name for name in accepted if name in EFFORT_ORDER]
    if requested not in EFFORT_ORDER or not graded:
        return requested
    if requested in graded:
        return requested
    index = EFFORT_ORDER.index(requested)
    for distance in range(1, len(EFFORT_ORDER)):
        up, down = index + distance, index - distance
        if up < len(EFFORT_ORDER) and EFFORT_ORDER[up] in graded:
            return EFFORT_ORDER[up]
        if down >= 0 and EFFORT_ORDER[down] in graded:
            return EFFORT_ORDER[down]
    return requested


# ── the template's own map, without jinja2 (not in the app's venv; adding it is a download) ──────

_SET_DICT = re.compile(r"\{%-?\s*set\s+\w+\s*=\s*(\{[^{}]*\})\s*-?%\}")


def parse_effort_map(template: str | None) -> dict[str, int | float]:
    """The template's name -> number effort map, or {} when it has none.

    Reads every ``{% set x = {...} %}`` dict literal and keeps the first whose keys are all effort
    names (at least one graded) and whose values are all numbers. Inkling's and DeepSeek's maps are
    JSON-shaped literals (double-quoted keys); a literal that is not JSON is skipped, never guessed.
    """
    for match in _SET_DICT.finditer(template or ""):
        try:
            literal = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(literal, dict) or not literal:
            continue
        keys = set(literal)
        if not keys <= set(KNOWN_NAMES) or not keys & set(EFFORT_ORDER):
            continue
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in literal.values()):
            return dict(literal)
    return {}


def _show(number) -> str:
    """0.0 -> "0", 0.9 -> "0.9", 75 -> "75": the number as the template prints it."""
    if isinstance(number, float) and number.is_integer():
        return str(int(number))
    return str(number)


@dataclass(frozen=True)
class Resolved:
    """What one turn sends for a level, and the sentence that explains it."""
    placement: str | None        # TOP, KWARGS, or None (nothing sent)
    value: str | int | float | None
    note: str = ""               # empty when the level is sent exactly as chosen

    def apply(self, extra: dict) -> None:
        """Put the value where it goes. KWARGS merges into an existing chat_template_kwargs."""
        if self.placement == TOP:
            extra["reasoning_effort"] = self.value
        elif self.placement == KWARGS:
            extra.pop("reasoning_effort", None)
            extra["chat_template_kwargs"] = {**(extra.get("chat_template_kwargs") or {}),
                                             "reasoning_effort": self.value}


@dataclass(frozen=True)
class EffortScale:
    """One served model's scale. `names` uses wire spelling ("none" for off) in ascending order."""
    names: tuple[str, ...]
    numbers: dict = field(default_factory=dict)
    number_wire: bool = False
    #: Where the scale came from, said in the UI ("template in <path>", "template not read", ...).
    source: str = ""
    #: True when `names` came from the template or a probe of it, False for a backend's static list.
    template_read: bool = False

    # -- reading the scale ------------------------------------------------------------------------

    @property
    def graded(self) -> tuple[str, ...]:
        return tuple(name for name in self.names if name in EFFORT_ORDER)

    def number_for(self, name: str):
        return self.numbers.get(name)

    def name_for(self, number) -> str | None:
        """The lowest-ordered name the template maps to exactly `number` (0.99 -> "xhigh", not "max")."""
        if not is_number(number):
            return None
        for name in KNOWN_NAMES:
            if name in self.numbers and name in self.names and float(self.numbers[name]) == float(number):
                return name
        return None

    @property
    def integer_only(self) -> bool:
        return bool(self.numbers) and all(isinstance(v, int) for v in self.numbers.values())

    def accepts_number(self, number) -> bool:
        """Can this number be sent AS a number: the server carries one, and it lies inside the map
        (0 up to the map's top; whole numbers only when every map value is whole, as DeepSeek's)."""
        if not (self.number_wire and self.numbers and is_number(number)):
            return False
        if self.integer_only and not float(number).is_integer():
            return False
        return float(number) <= max(float(v) for v in self.numbers.values())

    def label(self, name: str) -> str:
        """ "high · 0.9" when the template maps it, else the bare name."""
        number = self.number_for(name)
        shown = "off" if name == NONE else name
        return f"{shown} · {_show(number)}" if number is not None else shown

    def sendable_numbers(self) -> list[str]:
        """The numbers that work here when the server cannot carry a number: the map's own values."""
        seen: list[str] = []
        for name in KNOWN_NAMES:
            if name in self.numbers and name in self.names and _show(self.numbers[name]) not in seen:
                seen.append(_show(self.numbers[name]))
        return seen

    def refuse_number(self, number) -> str | None:
        """Why `number` cannot be chosen for this model, or None when it can."""
        if not is_number(number):
            return f"{number!r} is not a reasoning number (a finite number >= 0)."
        if not self.numbers:
            return "This model's template has no effort numbers; pick one of its levels instead."
        if self.accepts_number(number) or self.name_for(number) is not None:
            return None
        if self.number_wire:
            top = _show(max(self.numbers.values(), key=float))
            whole = ", whole numbers only" if self.integer_only else ""
            return f"{_show(number)} is outside this model's scale (0 to {top}{whole})."
        return (f"This server cannot send a number to the template yet. Numbers that work here: "
                f"{', '.join(self.sendable_numbers())}.")

    # -- one level -> the wire --------------------------------------------------------------------

    def resolve(self, level) -> Resolved:
        """A chosen level (a UI name, "off", or a number) -> what this turn sends."""
        level = as_level(level)
        if level is None or level == "":
            return Resolved(None, None)
        if is_number(level):
            return self._resolve_number(level)
        name = NONE if level == "off" else str(level)
        if name == NONE:
            # A template that maps "none" (Inkling: level 0) has no thinking switch of its own, and
            # llama.cpp ERASES a top-level "none" (server-common.cpp:1498-1505), which left Inkling at
            # its default 0.9. Through the template kwargs it reaches the map on every engine.
            return Resolved(KWARGS if NONE in self.numbers else TOP, NONE)
        if not self.template_read or name in self.names or not self.graded:
            return Resolved(TOP, name, "" if self.template_read else "template not read")
        near = nearest_accepted(name, self.names)
        number = self.number_for(near)
        return Resolved(TOP, near,
                        f"{name} -> {near}{f' ({_show(number)})' if number is not None else ''}: "
                        f"this model's template has no '{name}'")

    def _resolve_number(self, number) -> Resolved:
        if self.accepts_number(number):
            return Resolved(KWARGS, number)
        name = self.name_for(number)
        if name is not None:
            return Resolved(TOP if name != NONE else KWARGS, name,
                            f"{_show(number)} is sent as '{name}' (this model's template maps "
                            f"'{name}' to {_show(number)})")
        # A number saved under another model or server (/think refuses it up front here). Same
        # spirit as the name rule: the nearest of THIS template's own numbers, a tie rounding up,
        # and the UI says so. A template with no numbers has nothing near, so nothing is sent.
        mapped = [(float(self.numbers[n]), n) for n in KNOWN_NAMES if n in self.numbers and n in self.names]
        if not mapped:
            return Resolved(None, None, f"{_show(number)} not sent: this model's template has no effort numbers")
        target = float(number)
        _value, near = min(mapped, key=lambda item: (abs(item[0] - target), -item[0]))
        return Resolved(KWARGS if near == NONE else TOP, near,
                        f"{_show(number)} -> {near} ({_show(self.numbers[near])}): "
                        f"{self.refuse_number(number) or ''}".rstrip(": "))


def parse_number(text):
    """A typed reasoning number: "0.1" -> 0.1, "60" -> 60 (an int: DeepSeek's template takes integers);
    anything else, including "nan", "inf" and negatives, -> None."""
    text = str(text).strip()
    try:
        number = int(text) if text.isdigit() else float(text)
    except ValueError:
        return None
    return number if is_number(number) else None


def number_refusal(number, scale: EffortScale | None, backend_label: str = "this backend") -> str | None:
    """Why `number` cannot be CHOSEN as the thinking level for this model, or None when it can.

    ONE rule for every place a number is typed: /think <n>, the /think picker's number row, the
    Settings "manual number" box and set_thinking (RPC/GUI). Choosing is stricter than sending: a number
    saved earlier under another model still resolves (nearest, said in the UI), but a new one is refused.
    """
    if not is_number(number):
        return f"{number!r} is not a reasoning number (a finite number >= 0)."
    if scale is None or not scale.template_read:
        return (f"{backend_label} takes level names only here: a number needs a model whose template "
                "was read and maps effort numbers.")
    return scale.refuse_number(number)


def number_hint(scale: EffortScale | None) -> str:
    """What may be typed for this model, said before it is typed."""
    if scale is None or not scale.template_read or not scale.numbers:
        return "This model takes level names only."
    if scale.number_wire:
        top = _show(max(scale.numbers.values(), key=float))
        whole = ", whole numbers only" if scale.integer_only else ""
        return f"Any number from 0 to {top}{whole}."
    return f"This server sends only the template's own numbers: {', '.join(scale.sendable_numbers())}."


def app_scale(app) -> EffortScale | None:
    """The app's cached scale for its current model, or None (never blocks, never raises)."""
    cached = getattr(app, "_cached_effort_scale", None)
    try:
        return cached() if callable(cached) else None
    except Exception:
        return None


def backend_label(app) -> str:
    backend = getattr(app, "backend", None)
    return getattr(backend, "label", None) or getattr(backend, "name", "") or "this backend"


def think_chip(app) -> str:
    """The header/footer chip: the chosen level and what is actually sent for it."""
    return describe(getattr(app, "thinking_level", None), app_scale(app), backend_label(app))[0]


def effort_body(scale: EffortScale, level) -> dict:
    """The extra_body fields `level` puts on the wire for this scale (a side call's whole effort)."""
    body: dict = {}
    scale.resolve(level).apply(body)
    return body


def describe(level, scale: EffortScale | None, backend_label: str = "this backend") -> tuple[str, str]:
    """(header chip, the /think sentence) for `level` on this model: what is ACTUALLY sent.

    Prefix (fcb8e996): a number that cannot be sent "must show in the header and the /think line ...
    not only in a note nobody sees", on a model with no numbers and on a backend with no scale alike.
    """
    level = as_level(level)
    if level is None or level == "":
        return "think:default", "Thinking level unset: the field is not sent and the server default applies."
    shown = _show(level) if is_number(level) else str(level)
    if scale is None:
        if is_number(level):
            return (f"think:{shown} (not sent)",
                    f"{shown} not sent: {backend_label} takes level names only, so the server default applies.")
        return f"think:{shown}", ""
    resolved = scale.resolve(level)
    if resolved.placement is None:
        return f"think:{shown} (not sent)", resolved.note + ", so the template default applies."
    if is_number(level):
        if resolved.placement == KWARGS and resolved.value == level:
            return f"think:{shown}", f"Thinking level: {shown} (sent as a number to the template)."
        return (f"think:{shown} (as {resolved.value})", resolved.note + ".")
    sent = "off" if resolved.value == NONE else resolved.value
    number = scale.number_for(resolved.value)
    tail = f" · {_show(number)}" if number is not None else ""
    if resolved.note == "template not read":
        return f"think:{shown} (template not read)", (
            f"Thinking level: {shown} (sends reasoning_effort={resolved.value!r}; this model's template was "
            f"not read, so its numbers are unknown and the engine maps the name).")
    if resolved.note:
        return f"think:{shown} -> {sent}{tail}", resolved.note + "."
    where = "chat_template_kwargs.reasoning_effort" if resolved.placement == KWARGS else "reasoning_effort"
    meaning = f" -> template level {_show(number)}" if number is not None else ""
    return f"think:{shown}{tail}", f"Thinking level: {shown} (sends {where}={resolved.value!r}{meaning})."


# ── discovery: what the SERVER says, through its own template renderer ──────────────────────────
#
# POST /apply-template renders the prompt a chat request would produce and returns {"prompt": ...}:
# no generation, no load of anything not already resident. NInfer parses the body through the same
# gates as a turn (ninfer src/serve/llamacpp_http.cpp:175-181); llama.cpp serves it too (tools/server/
# server.cpp:202). Discovery calls NOTHING else on the engine.

PROBE_MESSAGES = ({"role": "user", "content": "probe"},)
_NO = object()


def http_post(host: str, timeout: float = 5.0):
    """`post(path, body) -> (status, json)` against `host`; an HTTP error is a status, not a raise."""
    import urllib.error
    import urllib.request

    def post(path: str, body: dict):
        req = urllib.request.Request(f"{host.rstrip('/')}{path}", data=json.dumps(body).encode("utf-8"),
                                     headers={"User-Agent": "LiteTUI", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
    return post


def _render(post, model, *, effort=None, kwargs_effort=_NO) -> str | None:
    body: dict = {"model": model, "messages": [dict(m) for m in PROBE_MESSAGES]}
    if effort is not None:
        body["reasoning_effort"] = effort
    if kwargs_effort is not _NO:
        body["chat_template_kwargs"] = {"reasoning_effort": kwargs_effort}
    try:
        status, payload = post("/apply-template", body)
    except Exception:
        return None
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    return prompt if status == 200 and isinstance(prompt, str) else None


def accepted_from_renders(renders: dict) -> tuple[str, ...]:
    """The graded names a template really accepts, from one render per name.

    Two names that render the same prompt are one level. On NInfer that is the engine mapping a
    rejected name onto its nearest accepted one (Qwen3.8: high and max render as xhigh); a template
    can also alias a name itself (Qwen's `high` -> `xhigh`). Each group holds exactly one accepted name,
    and the set chosen must reproduce the groups under NInfer's nearest rule; among sets that do, the
    highest name of each group wins (llama.cpp's own convention, common/jinja/caps.cpp:631-641).
    """
    import itertools

    groups: dict[str, list[str]] = {}
    for name in EFFORT_ORDER:
        if renders.get(name) is not None:
            groups.setdefault(renders[name], []).append(name)
    members = [sorted(g, key=EFFORT_ORDER.index, reverse=True) for g in groups.values()]
    for choice in itertools.product(*members):
        if all(nearest_accepted(name, choice) == choice[i] for i, group in enumerate(members) for name in group):
            return tuple(sorted(choice, key=EFFORT_ORDER.index))
    return tuple(sorted((g[0] for g in members), key=EFFORT_ORDER.index))


def discover_scale(post, *, model: str, template: str | None, fallback_names=(), source: str = "") -> EffortScale:
    """A model's scale from its template text (may be None) and its server's /apply-template.

    `post(path, body) -> (status, json)` is the only door to the engine. Names: the template map's keys
    when it has a map, else the probe's render groups, else `fallback_names` (the backend's static
    list) with template_read False. The number channel is on only when a number sent in
    chat_template_kwargs renders exactly like the name the map gives that number, and unlike no effort.
    """
    numbers = parse_effort_map(template)
    renders = {name: _render(post, model, effort=name) for name in KNOWN_NAMES}
    probed = any(prompt is not None for prompt in renders.values())
    if numbers:
        names = tuple(n for n in KNOWN_NAMES if n in numbers and (not probed or renders.get(n) is not None))
        if NONE not in names and renders.get(NONE) is not None:
            names = (NONE, *names)       # off through the thinking switch, for a map without "none"
        read = True
    elif probed:
        names = ((NONE,) if renders.get(NONE) is not None else ()) + accepted_from_renders(renders)
        read = True
    else:
        names, read = tuple(fallback_names), False
    number_wire = False
    if numbers and probed:
        default = _render(post, model)
        for name in names:
            if name in EFFORT_ORDER and renders.get(name) is not None and renders[name] != default:
                carried = _render(post, model, kwargs_effort=numbers[name])
                number_wire = carried is not None and carried == renders[name]
                break
    where = source or "the served template"
    return EffortScale(names=names, numbers=numbers if read else {}, number_wire=number_wire,
                       source=where if read else "template not read", template_read=read)
