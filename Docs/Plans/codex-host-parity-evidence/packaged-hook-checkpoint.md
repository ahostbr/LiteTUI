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
The helper now emits the universal hook output continue:false plus a fixed
stopReason and exits zero. The matching native hook schema defines those universal
fields. It never includes exception text, arguments, or credentials in failure output.
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
the exact explicit stop document. All child processes and listeners are closed and
the temporary extraction is removed. No Codex inference or other provider call.

Wheel SHA-256: 6806da44d0bf8a67e380028471e898468819c540b05c318d5a0da667b5bca044.
Helper SHA-256: 2c4d782da467f38c019d6b7396ccfce3b8cfd50dd33ac903ee3ee6537c95b5bf.
Numeric/boolean evidence is in packaged-hook-probe.json.

PYTHONPATH=src `python -m pytest tests/test_codex_hook_bridge.py tests/test_codex_native_policy.py -q`: 10 passed in 0.53s. Helper, bridge and packaged probe pass Ruff.

This proves wheel inclusion, helper launch and response preservation across these
shells. The changed unavailable-policy verdict still needs an intentional native
engine enforcement probe; schema support alone is not that proof. Other packaged
TUI/RPC/client journeys, child inheritance and full C1-C10 acceptance remain open.
No release clearance.
