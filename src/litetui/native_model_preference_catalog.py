"""Typed model-screen vocabulary, derived from the source-owned panel tables."""

from __future__ import annotations

import json
import math

from litetui import llm_backend
from litetui.plugins import model_switch
from litetui.turn_engine import TurnEngine

INFERENCE = tuple(row[0] for row in model_switch._INFER_FIELDS) + (
    "enable_thinking",
    "json_schema",
)
LOAD = tuple(row[0] for row in model_switch._LOAD_FIELDS)
# Preserve the original native keyboard order; the remaining source rows follow.
FIELDS = (
    ("temperature", "top_p", "max_tokens")
    + tuple(k for k in INFERENCE if k not in ("temperature", "top_p", "max_tokens"))
    + LOAD
)
BACKENDS = frozenset(("lmstudio", "llamacpp", "custom", "ninfer", "strata"))


def catalog(app):
    llama = app.backend.name == "llamacpp"
    flags = llm_backend.configured_flags(app.settings) if llama else frozenset()
    modes = (
        llm_backend.configured_spec_types(app.settings)
        if llama and "spec-type" in flags
        else frozenset()
    )
    selected = app.model_rows.get(app.model_id)
    result = {}
    rows = [("inference", row) for row in model_switch._INFER_FIELDS]
    rows += [
        ("inference", ("enable_thinking", "Enable Thinking", "tri", None)),
        ("inference", ("json_schema", "Structured Output (JSON schema)", "json", None)),
    ]
    rows += [("load", row) for row in model_switch._LOAD_FIELDS]
    for group, (key, label, kind, extra) in rows:
        choices = list(extra) if kind == "select" else []
        reason = None
        if group == "inference":
            if app.backend.name not in BACKENDS:
                reason = "This provider owns its inference configuration."
            elif key == "enable_thinking" and not (llama and "jinja" in flags):
                reason = "Requires llama.cpp with the jinja template flag."
        elif not llama and not (app.backend.name == "lmstudio" and key == "ctx"):
            reason = "Load settings belong to the backend's own model manager."
        elif llama and flags and llm_backend.FLAG_FOR.get(key, "") not in flags:
            reason = "The installed llama.cpp build does not advertise this flag."
        note = extra if isinstance(extra, str) else "server default"
        if key == "spec_type":
            kind, choices = "select", []
            if llama:
                if not flags or "spec-type" in flags and "none" in modes:
                    choices.append("none")
                if {
                    "spec-type",
                    "spec-draft-model",
                } <= flags and "draft-simple" in modes:
                    choices.append("draft-simple")
                mtp = {
                    "spec-type",
                    "spec-draft-n-max",
                    "spec-draft-n-min",
                    "spec-draft-p-min",
                }
                heads = getattr(selected, "nextn_predict_layers", None)
                if (
                    "draft-mtp" in modes
                    and mtp <= flags
                    and type(heads) is int
                    and heads > 0
                ):
                    choices.append("draft-mtp")
                reason = None  # Always retain the source screen's clear/recovery path.
            note = "Unset preserves legacy draft settings; none disables speculation. MTP requires binary support and GGUF heads."
        if key == "stop":
            kind, note = (
                "strings",
                "JSON array of exact stop strings; [] explicitly disables inherited stops.",
            )
        if key == "mmproj":
            note = 'Unset auto-pairs a unique sibling projector; "none" disables vision. Explicit paths must exist.'
        if key == "json_schema":
            note = "JSON text; inherit removes this model's structured-output override."
        low, high = (
            (model_switch.CTX_FLOOR, model_switch.CTX_TYPO_CEILING)
            if key == "ctx"
            else ((1, None) if key == "max_tokens" else (None, None))
        )
        if key == "ctx":
            note = f"512..1048576 is a typo guard, not model capacity. Configured default: {app.settings.default_context_length or 'model default'}."
        result[key] = {
            "label": label,
            "group": group,
            "kind": kind,
            "choices": choices,
            "minimum": low,
            "maximum": high,
            "help": note,
            "editable": reason is None,
            "reason": reason,
            "wire_choices": {},
        }
        if key == "reasoning_effort":
            result[key]["wire_choices"] = {
                level: TurnEngine.resolve_reasoning_effort(
                    level,
                    app.backend.name,
                    app.model_id,
                    app.settings.lmstudio_graded_thinking_models,
                )
                for level in choices
            }
    return result


def checked(field, value, spec=None):
    if field not in FIELDS:
        raise ValueError("Choose a supported model preference")
    if value is None:
        return None
    rows = (
        model_switch._INFER_FIELDS
        + model_switch._LOAD_FIELDS
        + [("enable_thinking", "", "tri", None), ("json_schema", "", "json", None)]
    )
    row = next(row for row in rows if row[0] == field)
    kind = row[2]
    if field == "stop":
        valid = (
            isinstance(value, list)
            and len(value) <= 256
            and all(isinstance(s, str) and len(s) <= 4096 for s in value)
        )
    elif kind == "int":
        valid = type(value) is int and -(2**63) <= value < 2**63
    elif kind == "float":
        valid = type(value) in (int, float) and math.isfinite(value)
    elif kind == "tri":
        valid = type(value) is bool
    else:
        valid = isinstance(value, str) and len(value.encode("utf8")) <= 65536
    if not valid:
        raise ValueError(f"{field}: invalid {kind} value")
    if field == "max_tokens" and value < 1:
        raise ValueError("Response limit must be a positive integer")
    if (
        field == "ctx"
        and not model_switch.CTX_FLOOR <= value <= model_switch.CTX_TYPO_CEILING
    ):
        raise ValueError("Context length must pass the source 512..1048576 typo guard")
    if field == "json_schema":
        json.loads(value)
    choices = (
        spec["choices"] if spec is not None else row[3] if kind == "select" else None
    )
    if (
        kind in ("select", "spec-select")
        and choices is not None
        and value not in choices
    ):
        raise ValueError(f"{field}: choose an advertised value or inherit")
    return value


def wire_value(field, value, spec):
    if field == "reasoning_effort" and value is not None:
        return spec["wire_choices"][value]
    if field == "json_schema" and value:
        return {
            "type": "json_schema",
            "json_schema": {"name": "litetui_schema", "schema": json.loads(value)},
        }
    return value
