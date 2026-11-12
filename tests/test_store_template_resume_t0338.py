"""Custom per-home store templates keep proved v2 prefixes and frozen suffixes."""
import json

import pytest
from test_owner_resume import build as _build
from test_owner_resume import folder

from litetui import appsvc
from litetui import prompt_overrides as prompts
from litetui.conversation import ConversationRepository

build = _build


@pytest.mark.parametrize("owner", [False, True], ids=["ordinary", "owner-memory"])
@pytest.mark.parametrize("explicit_provenance", [True, False], ids=["proved", "unrecorded"])
def test_custom_template_save_release_resume_keeps_frozen_suffix(tmp_path, build, owner, explicit_provenance):
    memory = folder(tmp_path, "external-memory") if owner else None
    first = build(memory)
    first._system = lambda _: None
    first._materialise_convo()
    home = first._agent_session.memory_root
    template_name = "owner-store.md" if owner else "conversation-store.md"
    template = "CUSTOM ADDRESS {convo_id} AT {store_path}\n(NO LEGACY HEADER)"
    (home / "prompts" / template_name).write_text(template, encoding="utf-8")
    root = memory if owner else home
    body_name = "SOUL.md" if owner else "soul.md"
    (root / body_name).write_text("SAVED FROZEN BODY", encoding="utf-8")
    prompts.refresh(first)
    first.conversation.clear()
    first._load_system_prompt()
    first._request_messages()
    first._append({"role": "user", "content": "PRIOR USER"})
    first._snapshot("synthetic custom-template v2 snapshot")
    path = first.convo_path
    meta, saved = ConversationRepository.read(path)
    assert meta["store_injected"] is True
    assert saved[0]["litetui_prompt_version"] == 2
    base = saved[0]["litetui_prompt_base"]
    suffix = saved[0]["content"][len(base):]
    assert "CUSTOM ADDRESS" in base and appsvc.STORE_HEADER in suffix
    assert "SAVED FROZEN BODY" in suffix
    first.store.release()
    first._agent_session.release()
    (root / body_name).write_text("NEW DISK MUST NOT REPLACE FROZEN BODY", encoding="utf-8")
    if not explicit_provenance:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            row.pop("store_identity", None)
        path.write_bytes(("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8"))
    raw = path.read_bytes()
    resumed = build(memory)
    resumed._system = lambda _: None
    resumed._render_resumed = lambda _: None
    if not explicit_provenance:
        assert not resumed._resume(path), "v2 marker alone must never establish missing store provenance"
        assert path.read_bytes() == raw
        return
    assert resumed._resume(path, startup=True)
    assert resumed._store_injected is True
    assert ConversationRepository.read(path) == (meta, saved)
    outgoing = resumed._request_messages()
    assert outgoing == [{"role": "system", "content": saved[0]["content"]},
                        {"role": "user", "content": "PRIOR USER"}]
    assert outgoing[0]["content"].endswith(suffix)
    assert "NEW DISK MUST NOT REPLACE" not in outgoing[0]["content"]
    assert ConversationRepository.read(path) == (meta, saved)
    assert path.read_bytes() == raw
