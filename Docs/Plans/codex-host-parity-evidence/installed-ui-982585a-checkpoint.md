# Installed TUI interaction checkpoint

Task: REL-20260914-CODEX-PARITY

Ran scripts/codex_installed_ui_probe.py with clean-982585a Python and -I.
Installed module provenance is asserted. The data directory is isolated, harness
registration disabled, connection startup replaced with no-ops and AppServer.start
forbidden. Events and displayed text are synthetic, not native provider execution.

The running installed Textual app receives command start/progress/completion,
retains the reported 1.25-second duration and collapses on completion. A pilot
click on the header expands the result. Expansion survives resize to 48 and 110
columns. The mounted Codex settings panel disables sampling and local context
loading while allowing thinking selection. Cancel closes it.

Evidence: installed-ui-982585a.json. SVG captures remain in ignored
artifacts/installed-ui-982585a/installed-codex-{48,110}.svg. They have not received
visual review and do not replace live native acceptance.

The first run reached cleanup with its runtime recorder holding the log open.
The probe now closes its recorder before temporary cleanup, including on failure.
The corrected run passed and removed its temporary data. Scoped Ruff passes.
Automatic approval review rejected cleanup of the first run's residue with
"blocked by policy"; it remains at
C:/Users/Ryan/AppData/Local/Temp/litetui installed ui 71choolv.

No global install, provider request, GUI/Suite launch, release, merge or push
occurred. Native background/child lifecycle, inventory replacement and complete
multi-client acceptance remain open.
