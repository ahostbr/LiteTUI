"""Reconcile display metadata from native history without executing or emitting tools."""

from copy import deepcopy
from types import SimpleNamespace

from litetui.codex_tool_ui import CodexToolUI
from litetui.codex_trace import records


async def reconcile(app, thread):
    if app is None or not isinstance(thread, dict) or not thread.get("id"):
        return 0
    turns = {turn["id"]: turn for turn in thread.get("turns", [])
             if isinstance(turn, dict) and turn.get("id")}
    changed = 0
    for index, message in enumerate(app.conversation):
        metadata = message.get("provider_metadata") or {}
        if (metadata.get("provider") != "codex"
                or metadata.get("app_server_thread_id") != thread["id"]):
            continue
        trace = metadata.get("display_trace")
        if trace is not None and (not isinstance(trace, dict) or trace.get("version") != 1):
            continue
        old = records(metadata)
        ids = {entry.get("turnId") for entry in old if entry.get("turnId")}
        if metadata.get("app_server_turn_id"):
            ids.add(metadata["app_server_turn_id"])
        merged = {(entry.get("turnId"), entry.get("id")): deepcopy(entry) for entry in old}
        for turn_id, turn in turns.items():
            if turn_id not in ids:
                continue
            # The existing presentation mapper has no execution capability.
            # A sink suppresses RPC and widget mounting during reconciliation.
            sink = SimpleNamespace(_rpc=True, _rpc_emit=lambda event: None)
            ui = CodexToolUI(sink, thread_id=thread["id"], turn_id=turn_id)
            native_order = []
            for item in turn.get("items", []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                if item.get("type") in ("userMessage", "reasoning", "plan"):
                    continue
                terminal = item.get("status") in (
                    "completed", "failed", "declined", "cancelled", "interrupted")
                if not terminal and (item.get("status") is not None
                                     or turn.get("status") != "completed"):
                    # Never turn an incomplete recovered item into success or
                    # start a fresh elapsed timer for historical work.
                    continue
                await ui.item(item, completed=True)
                record = ui.records.get(item["id"])
                if record is None:
                    continue
                key = (turn_id, item["id"])
                previous = merged.get(key, {})
                record = {**record, "turnId": turn_id}
                if (record.get("durationMs") is None and previous.get("durationMs") is not None
                        and previous.get("state") == record.get("state")):
                    record["durationMs"] = previous.get("durationMs")
                merged[key] = record
                native_order.append(key)
            # Recover missing items in native order rather than appending them
            # after an already-saved final answer. Retain local-only records.
            anchor = None
            for key in reversed(native_order):
                if anchor is not None:
                    value = merged.pop(key)
                    ordered = {}
                    for existing, record in merged.items():
                        if existing == anchor:
                            ordered[key] = value
                        ordered[existing] = record
                    merged = ordered
                anchor = key
        result = list(merged.values())
        if result != old:
            previous_trace = metadata.get("display_trace")
            metadata["display_trace"] = {"version": 1, "items": result}
            try:
                app._edit(index, "Codex native history reconciled")
            except Exception:
                if previous_trace is None:
                    metadata.pop("display_trace", None)
                else:
                    metadata["display_trace"] = previous_trace
                raise
            changed += 1
    return changed
