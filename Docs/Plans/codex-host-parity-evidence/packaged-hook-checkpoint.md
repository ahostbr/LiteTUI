# C1 packaged hook client checkpoint

The current LiteTUI package is a setuptools Python wheel with a Python console
entry point, not a frozen executable. Built the candidate using:

```powershell
python -m build --wheel --no-isolation --outdir artifacts/codex-wheel-check
```

The local wheel includes both codex_hook_helper.py and codex_hook_bridge.py. It was
not installed globally, published, or used to restart Suite. Build artifacts remain
under the ignored local artifacts directory. Setuptools emitted an existing wheel
bdist_wheel deprecation advisory; build completed successfully.

## Reproduced failure and fix

The original helper emitted stderr and exit 2 on unavailable policy connection.
The packaged probe showed CMD preserving exit 2 but PowerShell returning exit 1.
Thus shell execution did not preserve the intended special blocking exit code.
The first replacement used universal continue:false, which survived both shells but
FAILED live native validation: Codex 0.154.0 reported `PreToolUse hook returned
unsupported continue:false`. The hook status was failed, so absence of a command
item was not counted as successful blocking. Shared schema fields were insufficient.

The corrected helper emits PreToolUse permissionDecision:deny with a fixed reason
and exits zero. PostToolUse failure emits its event-specific block feedback; it
cannot undo the action already completed. No exception text, arguments or credentials
are included. Invalid input conservatively yields the pre-tool denial shape.
An earlier probe-only CMD launcher incorrectly used CRT argv quoting for a shell
command and was corrected to send a command string; that was not a product defect.

## Packaged subprocess validation

With PYTHONPATH=src in the parent:

```powershell
python scripts/codex_packaged_hook_probe.py --wheel artifacts/codex-wheel-check/litetui-0.22.2-py3-none-any.whl --output Docs/Plans/codex-host-parity-evidence/packaged-hook-probe.json
```

The script extracts the exact standalone helper from the wheel to a temporary path
with spaces, removes PYTHONPATH for each child, uses the interpreter directory at
the front of PATH like the product bridge, and exercises the authenticated local
bridge. Six subprocess cases passed: allow, deny, unavailable under cmd.exe and
powershell.exe. Allowed/denied policy responses remain intact; unavailable returns
the exact explicit permission-denial document. All child processes and listeners are closed and
the temporary extraction is removed. No Codex inference or other provider call.

Wheel SHA-256: 551139209fc2695fd2b6163046db53c719d899a8ef554c29481a656def6a9e28.
Helper SHA-256: 7099f16d6bc23d51213bd3f2472bc99f7031b77acaf610a72455f704e55ad2f7.
Numeric/boolean evidence is in packaged-hook-probe.json.

PYTHONPATH=src `python -m pytest tests/test_codex_hook_helper.py tests/test_codex_hook_bridge.py tests/test_codex_native_policy.py -q`: 14 passed in 0.59s. Changed helper, both probes and helper tests pass Ruff.

This proves wheel inclusion, helper launch and response preservation across these
shells. An intentional native enforcement probe now passes:

```powershell
python scripts/codex_native_hook_probe.py --live --unavailable-policy
```

The probe invokes the actual helper with its authentication environment deliberately
missing. Native hook status is blocked, the wrapper proves helper invocation, and
no commandExecution item is created. Evidence: native-hook-unavailable-probe.json.
The isolated app-server closed with exit zero; its temporary home and copied login
state were removed. Two earlier continue:false probes failed the acceptance assertion
and are superseded, not counted as passing evidence. The probe now supplies its
private home to server.environment rather than modifying the parent's environment.
Only enumerated failure classifications are retained by the probe.

Other packaged
TUI/RPC/client journeys, child inheritance and full C1-C10 acceptance remain open.
No release clearance.
