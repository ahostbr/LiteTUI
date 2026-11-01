"""Historical deferred receipts validate dispositions without granting replay."""
from types import SimpleNamespace
from uuid import UUID

import pytest

from litetui import background_input as work


def uid(value):
    return str(UUID(int=value))


@pytest.fixture
def deferred(tmp_path):
    app = SimpleNamespace(convo_dir=tmp_path, convo_id=uid(1),
        _agent_session=SimpleNamespace(authority=SimpleNamespace(agent_id=uid(2))),
        backend=SimpleNamespace(name="current"), model_id="current-model")
    job = work.encoded({"id": "job-a", "prompt": "Retained prompt"}).decode()
    row = {"invocation_id": uid(3), "job_id": "job-a", "kind": "cron",
        "generation": 1, "source": "scheduled", "requested_profile": "strict",
        "payload": {"content": "Retained prompt", "source": "scheduled", "tool_profile": "strict"},
        "job_snapshot": job, "job_sha256": work.digest(job.encode()),
        "epoch": {"instance": "a" * 64, "identity": {"session_id": uid(9),
            "agent_id": uid(2), "backend": "old", "model": "old-model"}, "provider": "codex"},
        "target": {"session_id": uid(1), "agent_id": uid(2)},
        "shutdown": {"version": 1, "instance": "a" * 64, "closing_sequence": 7,
            "final_sequence": 8, "admission_closed": True, "worker_settled": True,
            "callback_settled": True, "process_settled": True, "producer_ids": [uid(10)],
            "inputs": [{"input_id": uid(11), "disposition": "reconciled"}],
            "admission_ids": [], "ledger_sha256": "f" * 64, "effects_verified": False},
        "state": "held", "reason": "Prior invocation retained"}
    document = {"version": 1, "state": "pending", "input_id": None, "record": row}
    return app, document, tmp_path / work.DEFERRED_FILENAME


@pytest.mark.parametrize("disposition", ["settled", "held", "cancelled", "uncertain", "reconciled"])
def test_retired_input_disposition_is_historical_evidence_only(deferred, disposition):
    app, document, path = deferred
    document["record"]["shutdown"]["inputs"][0]["disposition"] = disposition
    raw = work.encoded(document)
    path.write_bytes(raw)
    rows = work.deferred_evidence(app)
    assert len(rows) == 1 and rows[0]["state"] == "held"
    assert rows[0]["shutdown"]["inputs"][0]["disposition"] == disposition
    assert rows[0]["epoch"] == document["record"]["epoch"]
    assert work.bridge(app) is None and path.read_bytes() == raw


@pytest.mark.parametrize("disposition", ["admitted", "queued", "Reconciled", "", None, True])
def test_unknown_or_nonterminal_retirement_disposition_is_rejected(deferred, disposition):
    app, document, path = deferred
    document["record"]["shutdown"]["inputs"][0]["disposition"] = disposition
    raw = work.encoded(document)
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="retirement input disposition"):
        work.deferred_evidence(app)
    assert path.read_bytes() == raw and work.bridge(app) is None
