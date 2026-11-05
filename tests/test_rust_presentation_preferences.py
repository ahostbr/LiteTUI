import json

import pytest
from test_rust_context_reader import app as owned_app
from test_rust_context_reader import armed as owned_context
from test_rust_context_reader import switch_app as owned_switch_app

from litetui import background_input as work
from litetui import gui_rpc, settings
from litetui import native_presentation_preferences as native
from litetui.settings_scope import SETTING_SPECS

app, switch_app, armed = owned_app, owned_switch_app, owned_context
pytestmark = pytest.mark.asyncio


def call(owner, operation, **kw):
    return gui_rpc.dispatch(
        owner,
        {
            "type": native.PREFIX + operation,
            "id": "presentation-" + operation,
            "version": 1,
            **work.identity(owner),
            "work_instance": work.bridge(owner).instance,
            **kw,
        },
    )


async def test_registry_scope_and_timing_come_from_the_canonical_settings_specs():
    for name in native.FIELDS:
        spec = SETTING_SPECS[name]
        assert (spec.scope.value, spec.apply_timing) == ("device", "immediate"), name
    assert set(native.FIELDS) == {
        "show_thinking", "show_stop_line", "show_stop_time", "autoscroll",
        "error_message_style", "dialog_style", "dialog_side",
        "footer_show_seat", "footer_show_thinking", "footer_show_bg",
        "footer_show_subagents", "footer_show_convo", "footer_show_context",
        "footer_show_context_pct", "footer_show_tps", "footer_show_cache",
        "footer_task_manager", "footer_show_key_hints", "footer_order",
    }
    assert "image_viewer_enabled" not in native.FIELDS
    assert "sidecar_enabled" not in native.FIELDS
    assert tuple(native.ORDER) == settings.FOOTER_ORDER_DEFAULT


def reordered():
    order = list(settings.FOOTER_ORDER_DEFAULT)
    order.insert(0, order.pop(order.index("tps")))
    return order


CASES = [
    ("show_thinking", False),
    ("show_stop_line", False),
    ("show_stop_time", True),
    ("autoscroll", False),
    ("error_message_style", "detail"),
    ("dialog_style", "sidebar"),
    ("dialog_side", "left"),
    ("footer_show_seat", False),
    ("footer_show_thinking", False),
    ("footer_show_bg", False),
    ("footer_show_subagents", False),
    ("footer_show_convo", False),
    ("footer_show_context", False),
    ("footer_show_context_pct", False),
    ("footer_show_tps", False),
    ("footer_show_cache", False),
    ("footer_show_key_hints", True),
    ("footer_order", reordered()),
]


@pytest.mark.parametrize("field,value", CASES)
async def test_every_presentation_preference_saves_only_its_reviewed_device_key(
    armed, field, value, monkeypatch
):
    owner = armed
    calls = []
    # The unit app has no mounted screen; autoscroll's runtime hook repaints the lock button
    # and re-follows the log, which the real runtime owns.
    monkeypatch.setattr(owner, "_refresh_prompt_controls", lambda: calls.append("refresh"), raising=False)
    monkeypatch.setattr(owner, "_scroll_down", lambda *a, **k: calls.append("scroll"), raising=False)
    path = owner._agent_session.store.data_root / "settings.json"
    original = json.loads(path.read_bytes())
    current = call(owner, "preferences")
    row = current["fields"][field]
    assert row["editable"] and row["saved"] == row["effective"] != value
    review = call(
        owner, "review", revision=current["revision"], field=field, value=value
    )
    assert json.loads(path.read_bytes()) == original
    applied = call(owner, "apply", token=review["token"])
    with pytest.raises(ValueError):
        call(owner, "apply", token=review["token"])
    observed = call(
        owner, "readback", token=review["token"], apply_id=applied["apply_id"]
    )
    fields = observed["preferences"]["fields"]
    assert fields[field]["saved"] == fields[field]["effective"] == value
    assert getattr(owner.settings, field) == value
    assert json.loads(path.read_bytes()) == {**original, field: value}
    assert observed["after"] != current["revision"]
    assert calls == (["refresh", "scroll"] if field == "autoscroll" else [])
    for other in native.FIELDS:
        if other != field:
            assert fields[other]["effective"] == current["fields"][other]["effective"]


async def test_task_manager_is_visible_but_not_offered_for_editing(armed):
    current = call(armed, "preferences")
    row = current["fields"]["footer_task_manager"]
    assert row["editable"] is False and row["effective"] is False
    with pytest.raises(ValueError, match="supported"):
        call(
            armed,
            "review",
            revision=current["revision"],
            field="footer_task_manager",
            value=True,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("show_thinking", 1),
        ("show_thinking", "false"),
        ("error_message_style", "verbose"),
        ("dialog_style", "popup"),
        ("dialog_side", "top"),
        ("footer_order", ["pct"]),
        ("footer_order", [*settings.FOOTER_ORDER_DEFAULT[:-1], "pct"]),
        ("footer_order", [*settings.FOOTER_ORDER_DEFAULT[:-1], "bogus"]),
        ("footer_order", list(settings.FOOTER_ORDER_DEFAULT) + ["plan"]),
        ("footer_order", "pct,ctx"),
        ("image_viewer_enabled", False),
        ("sidecar_enabled", True),
        ("theme_name", "matrix"),
    ],
)
async def test_unsupported_values_and_foreign_fields_are_refused_without_a_write(
    armed, field, value
):
    path = armed._agent_session.store.data_root / "settings.json"
    before = path.read_bytes()
    current = call(armed, "preferences")
    with pytest.raises(ValueError):
        call(armed, "review", revision=current["revision"], field=field, value=value)
    assert path.read_bytes() == before


async def test_launch_override_makes_the_preference_read_only(armed, monkeypatch):
    # No presentation field has an environment variable, but a launch override still wins.
    monkeypatch.setattr(armed, "_launch_overrides", {"show_thinking": False}, raising=False)
    current = call(armed, "preferences")
    row = current["fields"]["show_thinking"]
    assert row["overridden"] and not row["editable"]
    with pytest.raises(ValueError, match="editable"):
        call(armed, "review", revision=current["revision"], field="show_thinking", value=False)
    assert current["fields"]["show_stop_line"]["editable"]


async def test_unrelated_disk_revision_after_review_refuses_apply(armed):
    owner = armed
    current = call(armed, "preferences")
    review = call(
        owner,
        "review",
        revision=current["revision"],
        field="show_stop_time",
        value=True,
    )
    path = owner._agent_session.store.data_root / "settings.json"
    data = json.loads(path.read_bytes())
    data["retained_other_tool"] = {"value": 42}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        call(owner, "apply", token=review["token"])
    assert json.loads(path.read_bytes()) == data
    with pytest.raises(ValueError, match="select an editable change"):
        call(
            owner,
            "review",
            revision=current["revision"],
            field="show_stop_time",
            value=True,
        )


async def test_readback_rejects_changed_post_save_state_and_replay(armed):
    current = call(armed, "preferences")
    review = call(
        armed,
        "review",
        revision=current["revision"],
        field="dialog_side",
        value="left",
    )
    applied = call(armed, "apply", token=review["token"])
    path = armed._agent_session.store.data_root / "settings.json"
    saved = path.read_bytes()
    path.write_text(
        json.dumps({**json.loads(saved), "autoscroll": False}), encoding="utf8"
    )
    with pytest.raises(ValueError, match="changed after saving"):
        call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
    path.write_bytes(saved)
    with pytest.raises(ValueError, match="again"):
        call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])


async def test_revision_changes_with_every_field_so_a_stale_review_cannot_apply(armed):
    first = call(armed, "preferences")
    armed.settings.show_thinking = not armed.settings.show_thinking
    second = call(armed, "preferences")
    assert first["revision"] != second["revision"]
    with pytest.raises(ValueError):
        call(
            armed,
            "review",
            revision=first["revision"],
            field="show_stop_time",
            value=True,
        )


async def test_runtime_state_reports_every_presentation_value_the_frontend_renders(armed):
    effective = gui_rpc._settings(armed)["effective"]
    for name in native.FIELDS:
        assert effective[name] == getattr(armed.settings, name), name
    assert effective["footer_order"] == list(settings.FOOTER_ORDER_DEFAULT)
