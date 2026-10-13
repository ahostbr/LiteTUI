"""Every production writer preserves or replaces the system's recorded facts."""
import pytest
from test_owner_resume import build as _build
from test_owner_resume import folder

from litetui import appsvc, gui_rpc
from litetui.conversation import ConversationRepository

build = _build


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True], ids=["home", "owner"])
@pytest.mark.parametrize("loaded", [False, True], ids=["pending-store", "loaded-store"])
async def test_gui_retry_preserves_custom_system_facts_on_reopen(tmp_path, build, owner, loaded):
    app = build(folder(tmp_path, "widgets") if owner else None)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._materialise_convo()
        app._handle_command("/system Custom system body")
        if loaded:
            app._request_messages()
        app._append({"role": "user", "content": "old question"})
        app._append({"role": "assistant", "content": "old answer"})
        before, _ = ConversationRepository.read(app.convo_path)
        # Mock only the next model-submit boundary, not the production RPC writer.
        app._submit_text = lambda text, **kwargs: app._append({"role": "user", "content": text})
        reply = gui_rpc._conversations(app, "retry", {"session_id": app.convo_id,
                                                       "index": 1, "message": "retry question"})
        assert reply["accepted"]
        meta, messages = ConversationRepository.read(app.convo_path)
        assert meta.get("store_identity") == before["store_identity"], "GUI retry dropped store provenance"
        assert meta.get("store_injected") is loaded, "GUI retry dropped injection receipt"
        assert messages == app.conversation
        assert app._resume(app.convo_path, startup=True)
        assert app._store_injected is loaded and app._store_notice_text is None
        prompt = app._request_messages()[0]["content"]
        assert prompt.startswith("Custom system body")
        assert prompt.count(appsvc.STORE_HEADER) == 1
        assert "OWNER-BODY" in prompt if owner else "HOME-BODY" in prompt
