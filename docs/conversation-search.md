# Conversation lookup and explicit refresh

Conversation lookup is a **read-only index snapshot**, not a refresh request.
`convo_search(query=...)`, `show=`, `messages=true`, and `stats=true` open the
existing SQLite index with `mode=ro`. They do not build, migrate, create an index,
read transcript sources, or enumerate the fleet. Search ranking is unchanged.
A no-hit means no match in this snapshot, not proof of absence in live transcripts.
`show` displays indexed messages, not a current source dump.

## Freshness and compatibility

The first result line is `Last successful refresh UTC: <UTC timestamp>` or
`Last successful refresh UTC: UNKNOWN`. Only successful refresh metadata supplies
that timestamp; file mtime is never used. Missing or malformed timestamp metadata
means UNKNOWN. A snapshot older than 24 hours prints a plain warning with its age,
**not a refusal**.

An index must have compatible schema/version and provenance binding it to the
configured durable data root. Missing, incompatible, legacy-without-provenance,
wrong-root, corrupt, or currently unavailable indexes refuse rather than repair.
The refusal includes an **exact copy/paste refresh command** with the running
Python executable, engine path and explicit data root. On Windows it is a quoted
PowerShell command; on POSIX it is shell-quoted. Follow that actual receipt rather
than guessing a root from the current directory. A legacy index without root
provenance refuses with UNKNOWN; a compatible index missing only freshness can
still be searched. Refresh failures remain failures: no automatic DB deletion or
repair fallback is provided.

## Explicit refresh: initiator and watcher

Call `convo_search(refresh=true)` **as a separate action**, or use the exact
`--root ... --refresh` command printed by a refusal. The tool classifies refresh
as external-write and process-execution authority; ordinary lookup is read-only.
A refresh launches a hidden, detached supervisor and immediately returns a
**launch receipt, not a completion claim**: supervisor PID, 3600-second work
budget, and absolute log path under:

```
<data root>/tools/convo-search-refresh/<UTC time>-<unique id>.log
```

The requesting agent/operator initiates and owns watching that log. No automatic
watcher is installed. Use the normal file read tool on the returned log path;
wait for `Refresh finished UTC: ...; exit 0`, a nonzero exit, a launch failure,
or `Refresh timed out ...`. Logs retain child output and terminal status.
If there is no terminal record (for example, machine or supervisor failure),
completion is **unknown**; do not infer success from a launch receipt or PID.
A timeout kills and waits for the index child. A commit can race the deadline,
so timeout reports completion unconfirmed rather than claiming no commit happened.
Check the successful-refresh header on the next lookup.

Only this explicit work action enumerates/validates the authoritative ownership
catalog. Rows and successful-refresh metadata publish in one SQLite transaction;
a failed pre-commit refresh leaves the previous snapshot and timestamp intact.
No auto retry is performed. Avoid concurrent refresh requests; SQLite contention
can fail a refresh or briefly refuse lookup (read lock wait is bounded to 0.2s).
The transaction can use more temporary disk than incremental per-conversation
commits; production refresh duration/disk cost has not been remeasured here.

Legacy CLI `--index` alone is an alias for detached refresh; `--reindex` launches
a detached full pass. Combining any refresh/index action with query/show/raw/stats
refuses, never silently does both. Hidden `--refresh-worker` and `--build-index`
are the supervisor/index-child implementation, not user lookup modes. Plugin
query text is passed after `--`, so text such as `--refresh` cannot start a job.

**Proposed trigger only, NOT implemented:** at a future operator-controlled
startup checkpoint, offer an explicit refresh when the last successful refresh
is UNKNOWN or over 24h old. The initiating owner must accept and watch its log.
There is no automatic startup refresh, daemon, scheduled refresh, or age-triggered
write in this change.

## Raw access intentionally refuses

`raw=` / `--raw` currently refuses with an explicit limitation. **Refreshing does
not enable raw access.** Use `show` only if an indexed snapshot answers the question;
it is not a substitute for current raw-source verification.

The existing source resolver is not bounded: `storage_catalog.locate_owned` calls
`conversations`; an agent selector still calls `AgentStore.find_agent`, which
validates through `list_agents`, and `list_conversations` calls `find_agent` again.
`catalog_paths` also enforces owned-over-archive precedence and duplicate-owned
membership checks. Trusting only a stored source path would bypass those checks.
T0503 therefore refuses raw reads rather than silently weaken current ownership,
scan the fleet on lookup, or rewrite AgentStore. Future bounded authoritative
raw-source resolution is separate work.

## Validation boundary

Focused tests use temporary data roots/databases and mocked process boundaries.
They cover read-only/no-build/no-catalog lookup; stale/UNKNOWN metadata;
missing/incompatible/wrong-root refusal; failed-refresh rollback; empty snapshots;
raw refusal; detached launch receipts/logs/budget; legacy action handling; option
text isolation; and the plugin's read/write policy split. No live index refresh,
performance benchmark rerun, or running-product rollout is part of these tests.
