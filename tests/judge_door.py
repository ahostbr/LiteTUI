"""The real approval door, for the tests of T0408-L. Not a test module.

`seat_app` builds a REAL LiteTUI on an owned agent session (the constructor refuses to
run without one) and shapes it into a seat that an agent spawned: not the owner, a
recorded spawner, a registered presence. `Wire` records what the door does to the world:
what it asked the spawner, what modal it opened, what it logged, and how often the
deterministic check was consulted. `through` sends ONE call through
`LiteTUI._authorize_action` the way `_execute_tool` does (a name, the arguments, the
registry's policy, the turn's profile, and NO workspace) and returns everything that
happened as plain data, so two runs can be compared with `==`.

NOTHING HERE RUNS A COMMAND. The door only decides; the tool behind it is never called.
"""
from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path

from litetui import agent_launch_context, approval_relay, harness, plain_read, runtime_log
from litetui import app as app_mod
from litetui import permission_judge as pj
from litetui import tool_policy as tp

SPAWNER = "leader-4f1e2d3c-0000-0000-0000-000000000001"
SEAT_ID = "11111111-1111-4111-8111-111111111111"
SEAT = "Seat"


@contextlib.contextmanager
def seat_app(tmp_path: Path, workspace, *, profile: str = tp.INTERACTIVE,
             judge: object = pj.COUNT, registered: bool = True, spawner: str | None = SPAWNER):
    """A spawned seat named Seat whose launch directory was `workspace`."""
    home = tmp_path / ".agents" / SEAT
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({
        "schema_version": 1, "name": SEAT, "agent_id": SEAT_ID,
        "execution": {"backend": "codex", "model": "fixture", "thinking_level": "high"},
    }), encoding="utf-8")
    with agent_launch_context.acquire(tmp_path, SEAT) as session:
        app = app_mod.LiteTUI(agent_session=session)
        app.settings.tool_policy_profile = profile
        app._active_tool_profile = profile
        app.settings.permission_judge = judge
        app._spawned_seat, app._owner_seat = True, False
        app._spawner_id = spawner
        app._agent_launched, app._rpc, app._approval_host = False, False, False
        app._hook_source = "typed"
        app._system = lambda *_a, **_k: None
        app._hook_workspace = None if workspace is None else Path(workspace).resolve()
        present(app, spawner if registered else None, registered=registered)
        yield app


def present(app, spawner: str | None, *, registered: bool = True) -> None:
    """Write this seat's presence row: who the registry says spawned it NOW."""
    harness.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    row = {"agent_id": app.seat.agent_id}
    if spawner:
        row["spawned_by"] = spawner
    (harness.AGENTS_DIR / f"{app.seat.agent_id}.json").write_text(json.dumps(row), encoding="utf-8")
    app.seat.registered = registered


class Wire:
    """What the door did. `relay` is the spawner's scripted answer; pass relay=None to
    leave the real `ask_spawner` in place."""

    def __init__(self, monkeypatch, relay: str | None = "denied"):
        self.rows: list[tuple[str, dict]] = []     # every runtime_log.record call
        self.asked: list[dict] = []                # every request that reached the relay
        self.modals: list[str] = []                # every human door that opened
        self.assessed = 0                          # calls to plain_read.assess
        self.relay = relay
        monkeypatch.setattr(runtime_log, "record", self._record)
        if relay is not None:
            monkeypatch.setattr(approval_relay, "ask_spawner", self._ask)
        monkeypatch.setattr(app_mod, "show_dialog", self._human("modal"))
        monkeypatch.setattr(app_mod.tool_approval, "approve_over_rpc", self._human("rpc"))
        real = plain_read.assess

        def counted(*args, **kwargs):
            self.assessed += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(plain_read, "assess", counted)

    def _record(self, event, **fields) -> bool:
        self.rows.append((event, fields))
        return True

    async def _ask(self, app, name, args, decision, source) -> str:
        self.asked.append({"name": name, "args": json.dumps(args, sort_keys=True, default=repr),
                           "action": decision.action, "profile": decision.profile,
                           "reason": decision.reason, "danger": decision.danger, "source": source})
        return self.relay

    def _human(self, kind: str):
        async def opened(*_args, **_kwargs):
            self.modals.append(kind)
            return None                             # nobody answers: the same as a cancel

        return opened

    def observed(self) -> list[dict]:
        return [fields for event, fields in self.rows if event == pj.EVENT]


_IDENT = re.compile(r"appr-[0-9a-f]{12}")


def _no_ident(value):
    """The real relay names each request with a fresh random id; two runs differ in it
    and in nothing else."""
    return _IDENT.sub("appr-#", value) if isinstance(value, str) else value


async def through(app, wire: Wire, tool: str, args, *, policy=None, **door) -> dict:
    """One call through the real door. With no `policy` and no `workspace` it is the
    call `_execute_tool` makes; a native door's call passes both."""
    app._stop_requested, app._stop_reason, app._stop_cause = False, None, None
    rows, asked, modals, assessed = len(wire.rows), len(wire.asked), len(wire.modals), wire.assessed
    door.setdefault("profile", app._active_tool_profile)
    try:
        result = await app._authorize_action(
            tool, args, policy if policy is not None else app.plugins.policy_for(tool), **door)
        raised = ""
    except Exception as error:  # noqa: BLE001 - an error is an outcome to compare, too
        result, raised = ("raised", False), type(error).__name__
    new = wire.rows[rows:]
    return {
        "authorized": result is None,
        "refusal": None if result is None else _no_ident(result[0]),
        "raised": raised,
        "stop": (app._stop_requested, _no_ident(app._stop_reason), app._stop_cause),
        "asked": wire.asked[asked:],
        "modals": wire.modals[modals:],
        "log": [(event, {key: _no_ident(value) for key, value in fields.items()})
                for event, fields in new if event != pj.EVENT],
        "observed": [fields for event, fields in new if event == pj.EVENT],
        "assessed": wire.assessed - assessed,
    }


def same_but_for_rows(off: dict, on: dict) -> bool:
    """Everything the door did is equal; only observation rows may differ."""
    keep = ("authorized", "refusal", "raised", "stop", "asked", "modals", "log")
    return all(off[key] == on[key] for key in keep)


async def both(app, wire: Wire, tool: str, args, **door) -> tuple[dict, dict]:
    """The same call with the setting OFF and then ON, on the same seat."""
    wanted = app.settings.permission_judge
    app.settings.permission_judge = None
    off = await through(app, wire, tool, args, **door)
    app.settings.permission_judge = wanted
    on = await through(app, wire, tool, args, **door)
    return off, on
