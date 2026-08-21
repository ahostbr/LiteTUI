# Spec — a tool cancel button

**From Ryan, 2026-08-21.** Queued for FirmFuse (`8113984f`) after ETA and the
thinking-timer work. Written to a file rather than sent as mail on purpose:
mail arrives as a user turn and `@work(exclusive=True, group="chat")` at
`app.py:2906` would cancel the in-flight turn — the very bug the message-queue
item exists to fix. Read this when you are free.

> "top left we have the pallet button ... i want a tool cancel button ... thats
> bash is running away i think"

Observed: `bash {"command":"cd C:\\Projects\\LiteTUI && set \"PYTHONUTF8=1\"&&
python tests\\run_all.py"}` at **2m 32.5s**, then **3m 45.7s**, still in flight
with no way to stop it short of killing the app.

---

## 🔴 THE TRAP — A CANCEL THAT LOOKS LIKE IT WORKS AND DOES NOTHING

```python
app.py:3218    result = await asyncio.to_thread(fn, args)
```

**`asyncio.to_thread` CANNOT BE CANCELLED.** Cancelling that task abandons the
*await*; the worker thread keeps running and the subprocess keeps running.

So the obvious implementation — a button that cancels the asyncio task — produces
exactly the failure mode this codebase keeps generating: the bubble stops
updating, the UI says "cancelled", and `run_all.py` is still burning a core and
holding file locks. **It would pass every test written against the UI.**

Do not build that. The cancel must reach the process.

## ✅ THE PATH THAT ACTUALLY WORKS — IT ALREADY EXISTS

```
ttyguard.py:62   def run(...)    -> subprocess.run(...)   blocking, no handle
ttyguard.py:91   def popen(...)  -> subprocess.Popen      RETURNS THE HANDLE
```

`tool_bash` (`app.py:~330`) calls `ttyguard.run`, so the handle is created and
discarded inside a blocking call. `popen()` is already there and already carries
the envelope (stdin=DEVNULL, `errors="replace"`, CREATE_NO_WINDOW, terminal
repair). Switch bash to `popen`, keep the handle in a registry keyed by the
tool-call id, and cancel by killing it.

Keep `ttyguard` as the only place a process is spawned — its module docstring
says it source-scans the runtime to assert no bare `subprocess.run/Popen`
exists. Do not add one.

## 🪤 SECOND TRAP — KILLING THE CHILD ORPHANS THE GRANDCHILD

`shell=True` on Windows means the direct child is **cmd.exe**; `python
tests\run_all.py` is its grandchild. `proc.kill()` kills cmd.exe and leaves
python running, detached and invisible.

Kill the TREE: `taskkill /PID <pid> /T /F`, or spawn with
`CREATE_NEW_PROCESS_GROUP` and signal the group. Same class as the first trap —
the naive version reports success and leaves the real work running.

## VERIFY WITH A DISCRIMINATING PAIR, NOT WITH THE UI

The UI going quiet proves nothing. Assert against the **process table**:

```
positive   start `python -c "import time; time.sleep(300)"`, press cancel,
           then assert that PID is GONE (tasklist / psutil), not that the
           bubble stopped
negative   same command, do NOT cancel; assert the PID is still there
tree       assert the GRANDCHILD pid is gone too, not just cmd.exe
```

Without the negative arm you have measured that a process eventually exits.

## SCOPE

- Button in the header beside the command-palette control, top-left.
- Enabled only while a tool is in flight; it already knows — `_inflight_tools`
  exists (you added it for the elapsed repaint).
- Cancelling ONE tool must not kill the turn. The loop should get a result like
  `[cancelled by user after 3m45s]` and carry on, so the model can react rather
  than the whole turn dying. That is the difference between this and Esc.
- Esc (`action_stop_turn`) stays what it is: stop the whole turn.
- Non-bash tools (`read`, `web_fetch`, MCP) have no process to kill — cancel
  should be disabled or a no-op with an honest message rather than a lie.

## ALSO SEEN, SEPARATE BUG — COMPACT RETRIED IN A LOOP

Same screenshots:

```
Compacting 126 messages…
Compact failed — conversation unchanged.
BadRequestError: 400 — 'Context size has been exceeded' (server_error 500)
Compacting 126 messages…          <- again, no backoff, no give-up
```

`_autocompact_running` (`app.py:2400-2410`) is supposed to prevent re-entry and
is cleared in a `finally`. Something re-entered anyway. Find out what: a failed
compact that immediately retries against a context that is still too large will
loop until the window changes, and each attempt is a full request.

Ryan cleared the 400 by raising LM Studio's context to 1,200,000 for compaction,
so the symptom is gone — **the retry loop is not**, and it will return the next
time a compact fails for any reason.

`BASH_DEFAULT_TIMEOUT_S = 120` (`app.py:264`) is the fallback, so a 3m45s bash
means the model passed a larger timeout. Worth showing the effective timeout in
the tool bubble — a tool with no visible deadline is indistinguishable from one
that has none.
