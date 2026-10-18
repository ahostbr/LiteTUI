"""Owned restart migration uses exact known launch prefixes, never memory scans."""
from pathlib import Path

import pytest
from _owned_seat import owned_app

from litetui import appsvc, seat_prompts
from litetui import prompt_overrides as prompts

CATALOG = Path("C:/Projects/LiteSuite/resources/liteharness-plugin/prompts/cognitive-architectures")
RECAP = "\n\nEnd each FINAL answer with <recap>two short lines: what you did and result, about 40 tokens</recap>. Do not put this tag in interim tool calls."


@pytest.mark.parametrize("related", [True, False])
@pytest.mark.asyncio
async def test_owned_legacy_launch_rehydration_preserves_suffix_and_receipt(related):
    if not CATALOG.is_dir():
        pytest.skip("canonical catalog absent")
    role_file = CATALOG.parent / "preambles" / "leader-preamble.md"
    wrapper = CATALOG / "workers" / "gamma.md"
    pure = CATALOG / "pure" / "gamma.md"
    assert all(p.is_file() for p in (role_file, wrapper, pure))
    app = owned_app()
    app._materialise_convo()
    session = app._agent_session
    authority = session.authority
    root, path, cid = session.memory_root, app.store.convo_path, app.convo_id
    # Simulate the pre-feature home, which has no launch-source manifest.
    (root / ".prompt-launch.json").unlink()
    legacy = prompts.read_text(role_file) + "\n\n" + prompts.read_text(wrapper)
    suffix = appsvc.STORE_OPENING + "### memory.md (current contents)\n\nFROZEN STORE\n"
    host = app.plugins.compose_prompt() + RECAP
    app.conversation[:] = [{"role": "system", "content": (legacy if related else "UNRELATED CUSTOM") + suffix},
                           {"role": "system", "content": host},
                           {"role": "user", "content": "PRIOR USER"}]
    app._store_injected = True
    app._snapshot("synthetic pre-feature launch")
    app.store.release()
    session.release()
    (root / "memory.md").write_text("NEW DISK MUST NOT APPEAR", encoding="utf-8")
    resumed = owned_app(role_prompt_file=str(role_file), cognitive_file=str(pure),
                        spawn_identity=(authority.agent_id, authority.name, "leader"))
    resumed._render_resumed = lambda _: None
    assert resumed._resume(path, startup=True)
    before = [dict(m) for m in resumed.conversation]
    if not related:
        assert not seat_prompts.rehydrate(resumed)
        assert resumed.conversation == before
        return
    resumed._cli_convo_id = cid
    resumed._connect_settled = True
    resumed._cli_initial_model = None
    resumed._cli_thinking_level = None
    resumed._system = lambda _: None

    async def registered():
        return True

    resumed._register_owned_startup = registered
    from litetui.app import LiteTUI
    await LiteTUI._apply_cli_args.__wrapped__(resumed)
    assert not resumed._cli_launch_error
    messages = resumed._request_messages()
    assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]
    text = messages[0]["content"]
    assert text.count("# OWNER ROLE") == 1
    assert text.count("# POLYMATHIC GAMMA") == 1
    assert "Worker Mode" not in text and text.endswith(suffix)
    assert "NEW DISK MUST NOT APPEAR" not in text
    assert resumed._store_injected is True
    assert resumed._prompt_store_identity == app._prompt_store_identity


def test_live_text_survives_relaunch_with_same_defaults(tmp_path):
    role = tmp_path / "default.md"
    role.write_text("DEFAULT ROLE BODY", encoding="utf-8")
    app = owned_app(role_prompt_file=str(role))
    root = app._agent_session.memory_root
    (root / "prompts" / "role.md").write_text("LIVE PERSISTENT BODY", encoding="utf-8")
    app.store.release()
    app._agent_session.release()
    resumed = owned_app(role_prompt_file=str(role))
    text = resumed._request_messages()[0]["content"]
    assert "LIVE PERSISTENT BODY" in text and "DEFAULT ROLE BODY" not in text
