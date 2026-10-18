"""Authored prompt text — INSIDE the package on purpose.

These files used to live at the repo root in ``prompts/`` and were read
through ``paths.PROMPTS_DIR``, a path anchored on the repo root. Inside an
installed wheel that anchor resolves outside the install, so:

* ``compact.md`` / ``wake-after-compact.md`` — read at IMPORT time by app.py —
  crashed first launch from the PyPI wheel (T135);
* ``tools.md`` — read on every turn while tools are enabled — would have
  crashed the first message;
* ``systemprompt.md`` — guarded by an exists() check, so it degraded SILENTLY:
  the agent running with no base prompt at all.

T135 (2026-08-30): the files ship inside ``litetui`` and ``paths.PROMPTS_DIR``
resolves to this folder when the repo-root layout is absent. A repo-root
``prompts/`` still wins when present, so a dev checkout can override any file
without touching src/.

T582 (2026-09-10): "override any file" is TRUE ONLY NOW. The anchor was
all-or-nothing on ``systemprompt.md``'s existence, so a repo-root ``prompts/``
holding just that one file moved EVERY read to a directory missing the other
five -- and ``textfmt.load_prompt`` reads them unguarded. Taking this docstring
at its word crashed on ``compact.md``. Reads now go through
``paths.prompt_file()``, which resolves PER FILE.

T0338 (current): a leased agent HOME owns a complete ``prompts/`` catalog,
seeded after child lease acquisition and before first system composition. All
shipped files plus role.md/method.md are editable; ordinary conversations in
that home share the layer. External --owner/--memory folders are NOT prompt
homes. The actual launch tier supplies exactly one role header independently
of editable body text. Selected wrapped cognitive sources resolve to an
existing pure catalog file, never a guessed heading split.

--role-prompt-file supplies default role body; caller --system-prompt[-file]
replaces that body. --cognitive-file selects one method. --prompts-dir is an
exclusive direct-CLI full-folder seed. The launch-source manifest prevents
repeated defaults from erasing live edits; changed launch sources for an
initialized home explicitly require owner prompt commands/new home.

Snapshots refresh at turn admission, not mid-turn. Reset keeps revision/audit
history and uses packaged defaults; role/method have no global defaults, so
their reset is explicitly unsupported (set the desired body instead). Native
Claude edits use a fresh seeded segment with prior context and frozen store;
pending deliveries are held, never silently moved or replayed. Legacy native
segments lacking frozen metadata require an explicit /claude new after edits.
Executable ownership/approval/tool enforcement is not authored prompt prose.
"""
