"""Approved T065 producers carry bounded metadata and no bodies."""
from __future__ import annotations

import ast
from pathlib import Path

from litetui import runtime_log
from litetui.conversation import ConversationRepository
from litetui.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parent.parent

# 🔴 SCAN THE PACKAGE, DO NOT HAND-LIST THE FILES.
#
# This was a four-entry tuple written against the pre-T070 layout. The
# decomposition then moved the scheduler monitor out of `app.py` into `cron.py`,
# and a hand-listed tuple CANNOT SEE a producer that moved to a file it does not
# name: the count silently reads 15, and the cheapest way to make that green is
# to edit the 16 down to 15 — which converts a RELOCATED producer into a DELETED
# one, with a green suite either way.
#
# `app.py` is actively being decomposed, so producers moving between modules is
# the expected case, not an exception to it. Scanning the package means a
# producer that moves stays counted, and only one that is genuinely removed
# changes the number.
PRODUCER_FILES = tuple(sorted((ROOT / "src" / "litetui").rglob("*.py")))
EXPECTED_EVENTS = {
    "monitor_sweep_failed",
    "sidecar_frame_rejected",
    "persistence_failure",
    "harness_registration_failed",
    # harness_rebind_failed was here and is GONE ON PURPOSE (f64442b, T5: "one
    # seat id per process, no ghost identities"; T585 deleted `Seat.rebind` and
    # tests/test_seat_rebind.py with it -- see app.py:2290). A producer that
    # disappears is the one change this gate must NOT wave through on sight, so
    # it was checked before being removed: there is no rebind path left to fail,
    # which is why nothing records it. Not a silenced error.
    "mcp_reader_failed",
    "mcp_config_failed",
    "mcp_server_start_failed",
    # T218: a per-server disconnect can fail on its own now that stopping ONE
    # server is a user-reachable act rather than something that only happened
    # while tearing the app down. Silent would be wrong for the same reason
    # start is not silent: the tool simply stops appearing.
    "mcp_server_stop_failed",
    "mcp_tool_failed",
    "plugin_observer_failed",
    "plugin_finalizer_failed",
    "plugin_import_failed",
    "plugin_register_failed",
    "plugin_activate_failed",
    "scheduler_tick_failed",
    "backend_connect_failed",
    "turn_stream_failed",
    # NOT MINE — landed in 7de4655 (SilverBolt, T217). Recorded here because
    # this gate is one scalar plus one set: there is no edit that accounts for
    # T218's producer and leaves this one unaccounted, so a green that excludes
    # it is unreachable. Sentinel ruled (A) on exactly that question.
    # WARNING: it is also the only dotted name among the eighteen; every other
    # event is underscore-only, so a grep of this log now needs two patterns.
    "llama.router_record_kept_foreign",
    # T499 (b541181), background tool calls. The FIRST non-failure producer in
    # this set, and it is recorded rather than exempted: the gate's subject is
    # every `runtime_log.record` in the tree, not every failure, and an
    # exemption for "lifecycle" would be a hole the next producer walks through.
    "task.started",
    # T838 (f2571bf, 2026-09-16, card-summary/card-collapse). Two producers
    # from ONE commit, and the second is the more interesting of the pair:
    # a summary that fails to RENDER and a summary that fails to PERSIST are
    # different failures with different remedies, and folding them into one
    # event would have made the log unable to tell them apart.
    # Third dotted family here, after llama.* and task.* — the warning above
    # about needing more than one grep pattern now understates it.
    "card_summary.failed",
    "card_summary.persist_failed",
}
PROHIBITED = {
    "prompt",
    "body",
    "content",
    "text",
    "message",
    "arguments",
    "args",
    "result",
    "output",
    "conversation",
    "label",
    "command",
}


def _producer_calls() -> list[ast.Call]:
    calls: list[ast.Call] = []
    for path in PRODUCER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "runtime_log"
            and node.func.attr == "record"
        ]
        for node in found:
            # The file, carried on the node, so a failure can NAME the
            # producer instead of only counting it.
            node._file = path.name
        calls += found
    return calls


def _located(calls):
    """(file, line, event) per producer, for a failure message that NAMES
    them. The gate could always count; it could never say which.
    """
    rows = []
    for call in calls:
        event = (call.args[0].value
                 if call.args and isinstance(call.args[0], ast.Constant)
                 else "<computed>")
        rows.append((getattr(call, "_file", "?"), call.lineno, event))
    return sorted(rows)


def test_exactly_the_approved_producers_exist_and_no_body_key_is_present() -> None:
    calls = _producer_calls()
    # 16 -> 18. Two producers, one per seat, neither of which updated this gate:
    #   llama.router_record_kept_foreign  llm_backend.py  7de4655  T217
    #   mcp_server_stop_failed            mcp_client.py   T218
    # Measured over HEAD's blobs, decoded as utf-8 explicitly:
    #   git ls-tree -r --name-only HEAD src/litetui, then ast.walk each
    #   `git show HEAD:<f>` for runtime_log.record -> 17 at 7de4655, 18 here.
    # WARNING: the first run of that probe LIED WITH A CONFIDENT NUMBER. Shelled
    # in TEXT mode, Windows decoded the blobs as cp1252, ~25 files raised
    # UnicodeDecodeError inside subprocess reader threads, and the script still
    # printed "count = 1" and a verdict. A crashing check reporting as a clean
    # measurement is why the bytes+utf-8 form is the one quoted above.
    #
    # 18 -> 19 (T579). Net of three changes, each traced to its commit rather
    # than absorbed into one number:
    #   - harness_rebind_failed  REMOVED  f64442b/T585   -1
    #   - task.started           ADDED    b541181/T499   +1
    #   - "task." + task.state   ADDED    b541181/T499   +1
    # Re-derive with: python -c on ast.walk over
    #   sorted((ROOT/"src"/"litetui").rglob("*.py")), same predicate as below.
    #
    # 19 -> 21 (T838). BOTH from one commit, traced with
    #   git log -1 --format='%h %ad %s' --date=short -L <line>,<line>:<file>
    # rather than absorbed into a new number:
    #   card_summary.failed          app.py:4901  f2571bf  2026-09-16
    #   card_summary.persist_failed  app.py:4928  f2571bf  2026-09-16
    #   (f2571bf: 'feat(cards): land card-summary/card-collapse on 0.23.1')
    #
    # 🔴 THE FOURTH TIME THIS GATE WENT RED FOR THIS REASON (16 -> 18 -> 19
    # -> 21), and the pattern is the gate's, not the authors': a scalar that
    # only a human can update is a gate that fails AFTER the change lands,
    # naming the count and never the producer. The failure message below is
    # the smallest fix for that — it does the tracing FOR the next reader
    # instead of making them re-derive it, which is what the comments above
    # record three people doing.
    # Integration 2026-09-24: baseline main has 22 (monitor_sweep_failed,
    # fab8d0b). Sidecar adds sidecar_frame_rejected (261712e), and WS7
    # adds the stop_all mcp_server_stop_failed site (62bf9c2): total 24.
    assert len(calls) == 24, (
        "the producer count moved. Name each delta and its commit before changing "
        "this number — the list is:\n  "
        + "\n  ".join(f"{f}:{n} {e}" for f, n, e in _located(calls))
    )
    events = {
        call.args[0].value
        for call in calls
        if call.args and isinstance(call.args[0], ast.Constant)
    }
    assert events == EXPECTED_EVENTS
    # \U0001F534 AND THE SET CANNOT SEE EVERY EVENT THIS TREE EMITS. app.py:1959
    # records `"task." + task.state`, so task.done / task.failed / task.killed
    # reach the log and NO constant-scanning gate can enumerate them. The count
    # above sees the CALL; `events` never sees its names. Asserted here as a
    # known shape rather than left as a silent hole in a gate whose whole claim
    # is "exactly the approved producers exist".
    dynamic = [
        call for call in calls
        if call.args and not isinstance(call.args[0], ast.Constant)
    ]
    assert len(dynamic) == 1, (
        "a second computed event name appeared; this gate can only count them, "
        "not name them, so each one needs a behavioural arm of its own"
    )
    for call in calls:
        keys = {kw.arg for kw in call.keywords}
        assert not keys & PROHIBITED


def _is_type_of_e_dunder_name(node: ast.AST) -> bool:
    """Is this expression literally `type(<something>).__name__`?"""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "type"
    )


def test_every_error_type_is_the_class_name_and_never_the_message() -> None:
    """🔴 STANDING CONSTRAINT 2, ENFORCED STRUCTURALLY INSTEAD OF BY REVIEW.

    The approved list calls this "the single highest-risk line in the whole
    task: it is one character of difference and it silently exfiltrates
    prompts, paths and payloads into an always-on log."

    The key-name check above CANNOT see it. `error_type=str(e)` uses an approved
    key with a forbidden VALUE, so it passes every other assertion in this file
    — measured, not assumed: mutating one producer to `str(e)` left this module
    fully green before this test existed.

    The two behavioural tests below do catch it, but only for the two producers
    they drive (conversation repository, plugins.emit). The other fourteen —
    including all five in app.py and the relocated one in cron.py — had nothing
    checking them at all. This closes that by reading the SOURCE, so it covers
    every producer without needing a driver for each one.
    """
    offenders = [
        (kw.value.lineno, ast.dump(kw.value)[:80])
        for call in _producer_calls()
        for kw in call.keywords
        if kw.arg == "error_type" and not _is_type_of_e_dunder_name(kw.value)
    ]
    assert not offenders, (
        "error_type must be `type(e).__name__`, never the exception message. "
        f"Offending expressions: {offenders}"
    )


def test_persistence_failure_records_type_not_error_body(monkeypatch) -> None:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime_log,
        "record",
        lambda event, **fields: seen.append((event, fields)) or True,
    )
    repo = ConversationRepository()
    repo._raise_to_app(OSError("private path and record body"))
    assert seen == [
        (
            "persistence_failure",
            {
                "site": "conversation.repository",
                "component": "conversation",
                "error_type": "OSError",
            },
        )
    ]
    assert "private path" not in repr(seen)


def test_swallowed_plugin_error_records_owner_and_type_only(monkeypatch) -> None:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime_log,
        "record",
        lambda event, **fields: seen.append((event, fields)) or True,
    )
    registry = PluginRegistry()

    def broken(_event):
        raise RuntimeError("private observer body")

    registry.add_observer("probe-plugin", broken)
    registry.emit({"channel": "output", "label": "private answer"})
    assert seen == [
        (
            "plugin_observer_failed",
            {
                "site": "plugins.emit",
                "component": "plugin",
                "plugin": "probe-plugin",
                "error_type": "RuntimeError",
            },
        )
    ]
    assert "private observer body" not in repr(seen)
