"""Producer envelope identity only; no execution provenance or live transport."""
from __future__ import annotations

import io
import json
import uuid

import pytest
from test_rust_owned_construction import ConstructionGate

from litetui import rpc

pytestmark = [pytest.mark.asyncio, pytest.mark.real_mcp_load]


async def test_equal_events_have_distinct_envelope_ids_and_unchanged_legacy_payload(tmp_path, monkeypatch):
    async with ConstructionGate(tmp_path, monkeypatch).open() as (app, _session, _calls):
        stream = io.StringIO()
        monkeypatch.setattr(rpc, "_real_stdout", stream)
        app._gui_rpc_enabled = True
        # Payload IDs belong to their original producer, never the envelope.
        event = {"type": "fixture_event", "event_id": "payload-id", "value": "same"}
        before = dict(event)
        app._rpc_emit(event)
        app._rpc_emit(event)
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert records[::2] == [before, before]
        assert [record["event"] for record in records[1::2]] == [before, before]
        ids = [record["event_id"] for record in records[1::2]]
        assert len(set(ids)) == 2
        assert all(uuid.UUID(value).version == 4 for value in ids)
        assert event == before
        assert all("execution_id" not in record for record in records)


async def test_envelope_identity_is_captured_before_first_sink_write(tmp_path, monkeypatch):
    async with ConstructionGate(tmp_path, monkeypatch).open() as (app, _session, _calls):
        stream = io.StringIO()
        monkeypatch.setattr(rpc, "_real_stdout", stream)
        app._gui_rpc_enabled = True
        app._gui_operation_id = "operation-A"
        original_emit = rpc.rpc_emit
        original_session = app.convo_id
        generated = []
        original_uuid = uuid.uuid4

        def new_id():
            value = original_uuid()
            generated.append(str(value))
            return value

        def sink(record):
            # A producer identity must already exist before the first sink.
            assert len(generated) == 1
            original_emit(record)
            app._gui_operation_id = "operation-B"
            app.convo_id = "33333333-3333-4333-8333-333333333333"

        monkeypatch.setattr(uuid, "uuid4", new_id)
        monkeypatch.setattr(rpc, "rpc_emit", sink)
        try:
            app._rpc_emit({"type": "fixture_event"})
        finally:
            app.convo_id = original_session
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert len(records) == 2
        assert records[1]["event_id"] == generated[0]
        assert records[1]["session_id"] == original_session
        assert records[1]["operation_id"] == "operation-A"
        assert records[1]["event"] == records[0]
