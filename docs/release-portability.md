# Release portability and evidence

## Output scratch roots

Worktree ownership remains the first check. Card-parent output expansion is
opt-in; a directory merely named `.scratch` grants nothing. Configure absolute
scratch bases in the machine-local file `~/.litetui/output-roots.json`:

```json
{"scratch_roots": ["/absolute/path/to/card-scratch"]}
```

The Windows equivalent uses absolute drive paths. Each owned linked worktree
must be directly below a card directory named `T` plus digits (optional hyphenated
suffixes). Only that card parent is added, never neighboring cards or the entire
base. Existing host `TEMP`/`TMP` output handling is unchanged.

`LITETUI_OUTPUT_SCRATCH_ROOTS`, separated with the platform path separator
(`;` on Windows, `:` on POSIX), overrides the file. An explicitly empty variable
disables card-parent expansion. Missing or invalid configuration grants none;
invalid configuration emits a warning. The home file is read in each process,
so panes that filter environment variables still share the machine-local setting.
These are output-path bounds, not permission to execute or destroy anything.

## Conversation search

`tools/convo_search.py` follows `LITETUI_DATA_ROOT` when set, matching the app.
Otherwise it uses its own checkout root. Conversations remain under `.convos/`;
the local search database remains under `tools/convo_search.db` at that data root.

## Candidate readiness

Run the structural evidence gate with an explicit expected observer:

```text
python scripts/readiness_gate.py --manifest <manifest.json> --observer "Release Owner"
```

`validate(manifest, root, expected_observer)` likewise requires an exact,
non-blank observer name. There is no machine-owner identity default. The manifest
must bind approval and all required evidence to the candidate commit, version,
and wheel digest. This validates records, not observer authenticity; a PASS is
not a signature. The release operator must review human approval provenance.
Source builds, artifact inspections, running-host checks, and human acceptance
are different evidence and must not be substituted for one another.

## Goal source compatibility

New owner-initiated goal loops emit `goal-owner`. Existing Codex steering and
Claude delivery ledgers normalize the old persisted source spelling on read;
new submissions do not gain an extra attended-source alias. Typed/GUI origin
and owner-seat checks retain their authority semantics.
