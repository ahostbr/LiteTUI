import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from test_rust_context_reader import app as owned_app
from test_rust_context_reader import armed as owned_context
from test_rust_context_reader import switch_app as owned_switch_app

from litetui import background_input as work
from litetui import gui_rpc, native_voice_preferences

app, switch_app, armed = owned_app, owned_switch_app, owned_context
pytestmark = pytest.mark.asyncio


def call(owner, operation, **kw):
    return gui_rpc.dispatch(
        owner,
        {
            "type": native_voice_preferences.PREFIX + operation,
            "id": "voice-" + operation,
            "version": 1,
            **work.identity(owner),
            "work_instance": work.bridge(owner).instance,
            **kw,
        },
    )


def block_audio(stack):
    from litetui import listen_tool, litesuite_tts, stt_backend, voice_backend

    def forbidden(*_args, **_kwargs):
        pytest.fail("Voice preferences invoked an audio or device probe")

    for source, names in (
        (voice_backend, ("stop", "speak", "list_sapi_voices", "available_engines")),
        (
            stt_backend,
            (
                "available",
                "missing",
                "list_mics",
                "record_start",
                "record_stop",
                "transcribe",
            ),
        ),
        (litesuite_tts, ("_available", "speak")),
        (listen_tool, ("run", "_status", "_start_server")),
    ):
        for name in names:
            stack.enter_context(patch.object(source, name, forbidden))


@pytest.mark.parametrize(
    "field,value",
    [("stt_model", "small.en"), ("tts_engine", "edge"), ("tts_litesuite_first", False)],
)
async def test_registered_voice_preference_saves_only_reviewed_device_key(
    armed, field, value
):
    owner = armed
    path = owner._agent_session.store.data_root / "settings.json"
    original = json.loads(path.read_bytes())
    with ExitStack() as guard:
        block_audio(guard)
        current = call(owner, "preferences")
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
        assert (
            observed["preferences"]["fields"][field]["saved"]
            == observed["preferences"]["fields"][field]["effective"]
            == value
        )
        assert json.loads(path.read_bytes()) == {**original, field: value}
        assert observed["after"] != current["revision"]


async def test_environment_capture_and_unrelated_revision_refuse_stale_edits(
    armed, monkeypatch
):
    owner = armed
    monkeypatch.setenv("LITETUI_STT_MODEL", "base.en")
    current = call(owner, "preferences")
    assert (
        current["fields"]["stt_model"]["overridden"]
        and not current["fields"]["stt_model"]["editable"]
    )
    with pytest.raises(ValueError):
        call(
            owner,
            "review",
            revision=current["revision"],
            field="stt_model",
            value="tiny.en",
        )
    review = call(
        owner, "review", revision=current["revision"], field="tts_engine", value="edge"
    )
    path = owner._agent_session.store.data_root / "settings.json"
    data = json.loads(path.read_bytes())
    data["retained_other_tool"] = {"value": 42}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        call(owner, "apply", token=review["token"])
    assert json.loads(path.read_bytes()) == data


async def test_refuse_recording_wrong_scalar_and_changed_post_save_readback(armed):
    current = call(armed, "preferences")
    armed._mic_proc = object()
    with pytest.raises(ValueError, match="dictation"):
        call(
            armed,
            "review",
            revision=current["revision"],
            field="tts_engine",
            value="edge",
        )
    armed._mic_proc = None
    with pytest.raises(ValueError, match="supported"):
        call(
            armed,
            "review",
            revision=current["revision"],
            field="tts_litesuite_first",
            value=0,
        )
    review = call(
        armed, "review", revision=current["revision"], field="tts_engine", value="edge"
    )
    applied = call(armed, "apply", token=review["token"])
    path = armed._agent_session.store.data_root / "settings.json"
    saved = path.read_bytes()
    path.write_text(
        json.dumps({**json.loads(saved), "stt_mic": "another configured input"}),
        encoding="utf8",
    )
    with pytest.raises(ValueError, match="changed after saving"):
        call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
    path.write_bytes(saved)
    with pytest.raises(ValueError, match="again"):
        call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
