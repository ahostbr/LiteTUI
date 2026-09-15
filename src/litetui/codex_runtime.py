"""Native process activity used to guard inventory-driven engine replacement."""


class RuntimeActivity:
    def __init__(self):
        self.revision = 0
        self.commands = {}
        self.finished = set()
        self.requests = set()
        self.uncertain = False
        self.connected = True

    def observe(self, message):
        method = message.get("method")
        if not method:
            return
        if "id" in message:
            self.requests.add(message["id"])
            self.revision += 1
            return
        params = message.get("params") or {}
        if method in ("turn/started", "turn/completed", "thread/status/changed"):
            self.revision += 1
        if method not in ("item/started", "item/completed"):
            return
        item = params.get("item") or {}
        if item.get("type") != "commandExecution":
            return
        self.revision += 1
        identity = tuple(params.get(key) for key in ("threadId", "turnId")) + (item.get("id"),)
        if not all(isinstance(value, str) and value for value in identity):
            self.uncertain = True
            return
        if identity in self.finished:
            return
        if method == "item/started":
            self.commands[identity] = item.get("processId") or self.commands.get(identity)
        elif item.get("exitCode") is not None or item.get("status") == "declined":
            self.finished.add(identity)
            self.commands.pop(identity, None)
            # A later native wait may report exit on another item for the same PTY.
            process = item.get("processId")
            if process is not None:
                matched = {key for key, value in self.commands.items() if key[0] == identity[0] and value == process}
                self.finished.update(matched)
                self.commands = {key: value for key, value in self.commands.items() if key not in matched}
        else:
            # Item completion/turn completion without process exit is not proof
            # that a native background PTY has terminated.
            self.commands[identity] = item.get("processId") or self.commands.get(identity)

    def replied(self, ident):
        if ident in self.requests:
            self.requests.remove(ident)
            self.revision += 1

    def disconnected(self):
        self.connected = False
        self.revision += 1


async def restart_readiness(server):
    """Read-only guard; never restart, interrupt, infer PIDs, or stop native work."""
    activity = server.runtime_activity
    if not activity.connected:
        return {"ready": False, "reason": "connection_unknown"}
    if activity.uncertain:
        return {"ready": False, "reason": "activity_unknown"}
    if activity.commands:
        return {"ready": False, "reason": "native_process_pending"}
    if activity.requests:
        return {"ready": False, "reason": "native_request_pending"}
    revision = activity.revision
    cursor, cursors, threads = None, set(), set()
    while True:
        params = {"limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        page = await server.request("thread/loaded/list", params)
        data = page.get("data")
        if not isinstance(data, list) or not all(isinstance(value, str) and value for value in data):
            return {"ready": False, "reason": "inventory_unknown"}
        threads.update(data)
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            return {"ready": False, "reason": "inventory_unknown"}
        cursors.add(cursor)
    for thread in sorted(threads):
        result = await server.request("thread/read", {"threadId": thread, "includeTurns": False})
        current = result.get("thread") or {}
        if current.get("id") != thread:
            return {"ready": False, "reason": "thread_unknown"}
        if (current.get("status") or {}).get("type") != "idle":
            return {"ready": False, "reason": "native_thread_not_idle"}
    if activity.revision != revision:
        return {"ready": False, "reason": "activity_changed"}
    return {"ready": True, "reason": "observed_idle", "revision": revision}
