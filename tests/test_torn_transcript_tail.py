"""T615: a torn append must not consume the next complete record."""
import json

import pytest

from litetui.conversation import ConversationRepository


@pytest.mark.parametrize("kind", ["msg", "edit", "truncate"])
@pytest.mark.parametrize("tail", [b'', b'{"type":"msg","message":', b'{"broken":"\xf0\x9f'])
def test_resume_append_replay_preserves_boundary(tmp_path, kind, tail):
    path = tmp_path / "transcript.jsonl"
    first = {"role": "user", "content": "original"}
    prefix = (json.dumps({"type": "msg", "message": first}) + "\n").encode() + tail
    path.write_bytes(prefix)
    repo = ConversationRepository()
    repo.adopt(path, "test")
    new = {"role": "user", "content": "survives"}
    if kind == "msg":
        repo.record_msg(new)
        expected = [first, new]
    elif kind == "edit":
        repo.record_edit(0, new)
        expected = [new]
    else:
        repo.record_truncate(1, [new], False)
        expected = [new]
    assert repo.persist_error is None
    assert path.read_bytes().startswith(prefix), "damaged evidence must not be discarded"
    _, messages = ConversationRepository.read(path)
    assert messages == expected
    # A subsequent resume and append must not regress the repaired boundary.
    repo.release()  # the prior writer exits before the next writer resumes
    again = ConversationRepository()
    again.adopt(path, "test")
    again.record_msg(first)
    assert ConversationRepository.read(path)[1] == expected + [first]
