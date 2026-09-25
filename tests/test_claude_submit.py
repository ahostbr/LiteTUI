"""Claude submission uses attachment state owned by each individual message."""
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui.claude_persistence import ClaudeLedger


@pytest.mark.parametrize("maintenance", [False, True])
@pytest.mark.parametrize("attached", [False, True])
def test_fresh_claude_submit_and_followup(tmp_path, monkeypatch, attached, maintenance):
    submitted, bubbles, spills = [], [], []
    image_path = str(tmp_path / "image.png")

    def spill(data):
        spills.append(data)
        return image_path

    app = SimpleNamespace(
        pending_image="AAAA" if attached else None,
        backend=SimpleNamespace(name="claude", owns_native_turns=True, session=None),
        convo_dir=tmp_path, convo_id="fresh", chosen_tool_profile="autonomous",
        tools_enabled=True, _pending_input=[], _mcp_maintenance=maintenance,
        notify=lambda *args, **kwargs: None,
        _materialise_convo=lambda: None, _chat_running=lambda: False,
        _split_image_path=lambda text: (None, text),
        _looks_like_image_path=lambda text: False,
        _oversize_refusal=lambda content: None,
        _spill_image_for_reclick=spill,
        _system=lambda text: None,
        _user_bubble=lambda text, has_image, **kwargs: bubbles.append((text, has_image, kwargs)),
        _scroll_down=lambda **kwargs: None,
    )
    monkeypatch.setattr(app_mod.hook_host, "start_prompt", lambda app, item: submitted.append(item))

    app_mod.LiteTUI._submit_text(app, "hey buddy", alt_chord=False)
    app_mod.LiteTUI._submit_text(app, "followup", alt_chord=False)

    if maintenance:
        assert submitted == []
        submitted = app._pending_input
    assert len(submitted) == 2
    assert submitted[1]["content"] == "followup"
    assert bubbles[1][2]["image_path"] is None
    assert app.pending_image is None
    if attached:
        assert image_path in submitted[0]["content"]
        assert bubbles[0][2]["image_path"] == image_path
        assert spills == ["AAAA"]
    else:
        assert submitted[0]["content"] == "hey buddy"
        assert bubbles[0][2]["image_path"] is None
        assert spills == []
    ledger = ClaudeLedger(tmp_path)
    assert [entry["content"] for entry in ledger.pending(ledger.selected["id"])] == [
        item["content"] for item in submitted
    ]
