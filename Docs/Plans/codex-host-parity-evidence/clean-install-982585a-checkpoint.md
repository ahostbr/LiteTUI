# Clean dependency installation at 982585a

Task: REL-20260914-CODEX-PARITY

Created artifacts/clean-982585a with Python 3.11.9 venv, without system
site-packages. Installed only the candidate wheel and its declared dependencies
using pip. No global package installation or live application restart occurred.

Wheel SHA256:
71426fda2f307b7820d9c1d0a65edde390b231b799a736463a976b4f656e64b8

Validation:

- pip check: no broken requirements.
- Installed Scripts/litetui.exe --version: litetui 0.22.2.
- Isolated Python (-I) imported app, codex_app_server, codex_runtime and
  codex_settings from this environment's installed package; each resolved module
  path was checked against sys.prefix. System site-packages were verified off.
- The packaged export probe passed with this interpreter and its dependencies.
  Export still imports the extracted wheel in a fresh process, checks provenance,
  forbids app construction and verifies content/assets/source preservation.
- The packaged hook probe passed all six allow/deny/unavailable cases under CMD
  and PowerShell using this interpreter and installed bridge implementation.

Evidence: clean-environment-982585a.json records resolved distribution versions;
clean-export-982585a.json and clean-hook-982585a.json record probe results.
Notable resolved dependencies include Textual 8.2.8 and OpenAI 3.14.0. This proves
these tested paths under this resolved set, not compatibility with every version
allowed by the dependency ranges.

The environment is retained as an ignored isolated artifact for follow-up
validation. No model requests were made. Full interactive installed-app journeys,
native background/child work, inventory replacement and GUI/Suite acceptance remain
open. This narrows the clean-install uncertainty without declaring release parity.
