# Regression and wheel checkpoint at 982585a

Task: REL-20260914-CODEX-PARITY

All 23 test_codex*.py modules ran together: 184 tests passed in 16.15 seconds.
The run used PYTHONPATH=src and the existing Python environment. This covers the
accumulated source regressions, not every shared-host test or live acceptance gate.

Built with:

```powershell
python -m build --wheel --no-isolation --outdir artifacts/parity-982585a
```

Wheel: artifacts/parity-982585a/litetui-0.22.2-py3-none-any.whl
SHA256: 71426fda2f307b7820d9c1d0a65edde390b231b799a736463a976b4f656e64b8

The packaged export probe passed in fresh Python processes outside the checkout,
with imports checked against the extracted wheel. It checked synthetic native
items, questions, zero duration, image bytes, relative asset links, unchanged
source and refusal to overwrite the export. Application construction was forbidden.
Evidence: packaged-export-982585a.json.

The packaged hook probe passed allow, deny and unavailable cases under CMD and
PowerShell, using a helper path with spaces and removing helper PYTHONPATH.
Evidence: packaged-hook-982585a.json. Embedded helper SHA256 remains
7099f16d6bc23d51213bd3f2472bc99f7031b77acaf610a72455f704e55ad2f7.

The build reported the existing wheel/bdist_wheel deprecation warning but completed
successfully. Prior wheels are retained separately. No global installation,
provider inference, GUI/Suite launch, merge, push or release occurred.

This refreshes stale package evidence after production changes. It uses installed
dependencies, not a clean dependency installation. Packaged native client journeys,
child/background lifecycle, live inventory replacement and the remaining full-plan
acceptance gates remain open; this checkpoint is not release approval.
