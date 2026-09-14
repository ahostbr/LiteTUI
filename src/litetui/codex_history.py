"""Reconcile display metadata from native history without executing or emitting tools."""

from copy import deepcopy
from types import SimpleNamespace

from litetui.codex_tool_ui import CodexToolUI
from litetui.codex_trace import records
from litetui.model_transport import ProviderError


async def hydrate(server, thread):
    """Honor the native thread's persisted legacy/paginated history contract."""
    if thread.get("historyMode", "legacy") != "paginated":
        return thread
    turns, cursors = {}, set()
    cursor = None
    while True:
        params = {"threadId": thread["id"], "itemsView": "full", "sortDirection": "asc", "limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        page = await server.request("thread/turns/list", params)
        data = page.get("data")
        if not isinstance(data, list):
            raise ProviderError("Codex returned an invalid history page.")
        for turn in data:
            if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str)
                    or not isinstance(turn.get("items"), list)
                    or turn.get("itemsView", "full") != "full"):
                raise ProviderError("Codex returned incomplete turn history.")
            turns[turn["id"]] = turn
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ProviderError("Codex history pagination did not advance.")
        cursors.add(cursor)
    return {**thread, "turns": list(turns.values())}


async def read(server, reference):
    response = await server.request("thread/read", {"threadId": reference, "includeTurns": False})
    thread = response.get("thread", {})
    if thread.get("id") != reference:
        raise ProviderError("Codex returned history for a different thread.")
    if thread.get("historyMode", "legacy") == "paginated":
        return await hydrate(server, thread)
    response = await server.request("thread/read", {"threadId": reference, "includeTurns": True})
    thread = response.get("thread", {})
    if thread.get("id") != reference:
        raise ProviderError("Codex returned history for a different thread.")
    return thread


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
            sink = SimpleNamespace(_rpc=True, _rpc_emit=lambda event: None, _native_activity_disabled=True)
            ui = CodexToolUI(sink, thread_id=thread["id"], turn_id=turn_id)
            native_order = []
            for item in turn.get("items", []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                if item.get("type") in ("userMessage", "reasoning", "plan"):
                    continue
                terminal = item.get("status") in (
                    "completed", "failed", "declined", "cancelled", "interrupted")
                partial_message = (item.get("type") == "agentMessage"
                                   and turn.get("status") in ("interrupted", "failed", "inProgress"))
                if not terminal and not partial_message and (item.get("status") is not None
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
                if partial_message:
                    record["state"] = ("running" if turn["status"] == "inProgress"
                                       else turn["status"])
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
