# Packaged export and hook checkpoint at 131e7ff

Built with `python -m build --wheel --no-isolation --outdir artifacts/parity-131e7ff`.
Wheel: litetui-0.22.2-py3-none-any.whl.
SHA256: dd4faf489632e00bb0cf126ba07604b6c2bfb4dd19c49c393f74bc2c3c766f07.
The earlier hook wheel/artifact remains separately retained.

`scripts/codex_packaged_export_probe.py` extracts this wheel to a temporary path
with spaces and launches fresh Python processes outside the checkout. Imported
app/CLI/history/export modules are asserted to originate from the extracted wheel.
Application construction is forbidden. The packaged CLI exports a synthetic native
tool result, assistant answer, pending question and image, each once. Assertions
check zero duration, byte-for-byte copied image, relative asset link, no base64 in
Markdown, unchanged source, and refusal to overwrite the exported document.
The temporary directory is removed. Evidence: packaged-export-probe.json.

The same wheel passed the existing packaged hook probe's allow/deny/unavailable
cases under both CMD and PowerShell (six cases), with helper PYTHONPATH removed
and a path containing spaces. Embedded helper SHA256 remains
7099f16d6bc23d51213bd3f2472bc99f7031b77acaf610a72455f704e55ad2f7.
Evidence: packaged-hook-131e7ff.json. The first driver invocation lacked PYTHONPATH
and failed at import before launching any hook case; the corrected invocation
used PYTHONPATH=src for the driver only. All six actual helper cases passed.

Ruff passes the new probe. No provider calls, desktop deployment, global install,
Suite build/restart or release action occurred. This uses the available Python
dependency environment, not a clean dependency-install test. Native live packaged
client journeys and the remaining full-plan gates are still open.
