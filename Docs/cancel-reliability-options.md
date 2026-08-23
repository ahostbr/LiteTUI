# Cancelling a tool: three options, and what each one costs

**For Ryan's decision. Requested by Sentinel; no code written for (b) or (c).**
Author: SilverBolt, 2026-08-23. Branch `fix/kill-tree-honesty`.

## The problem, in one paragraph

`tool_bash` spawns with `shell=True`, so the direct child is `cmd.exe` and the real work is
its grandchild. Cancelling means killing a **tree**, and today that is `taskkill /PID n /T /F`
under a 15s budget. Measured against this app's own tree (Anvil, n=10): **min 3.42s, median
6.38s, max 43.06s, 1 in 10 over budget.** The walk is slow because it is a walk, and it gets
slower exactly when the box is busy — which is when a user is most likely to hit cancel.

`23cbf78` fixed the *honesty*: an unfinished kill is no longer reported as a successful
cancel. It did not fix the *kill*. **The product still cannot reliably stop its own child on
a loaded box**, and a test that asserts the tree is dead still fails, correctly.

---

## (a) Accept and report honestly — what we have now

**Contract:** `kill_tree -> bool`. `False` means *we could not confirm*, and no caller may
call that a successful cancel. Three report sites plumbed (toast, cancelled result, timeout
result).

| | |
|---|---|
| **Cost** | Nothing further to build. |
| **What the user gets** | A cancel that works ~90% of the time, and an honest warning the other ~10%. |
| **What could go wrong** | The honest warning is the *only* remedy. A user who cancels a runaway and is told "could not be confirmed" has no next step inside the app — they are sent to Task Manager. Repeated often enough, an accurate warning that offers no action becomes noise people click past. |
| **Residual** | Orphaned trees keep holding their stdout pipe, so `communicate()` stays blocked and the tool call cannot return until the tool's own timeout. Leaked probes then inflate the process table, which slows the next `taskkill`, which raises the failure rate. **The failure is self-amplifying** — measured on this box today: 12 orphan probes at one point, and load rising monotonically across a test run. |

---

## (b) Retry in the background worker until confirmed, or a hard deadline

**Contract:** unchanged in shape — `kill_tree -> bool` — but the worker keeps trying, so
`False` becomes rarer and *later*. The user waits longer before hearing anything definite.

| | |
|---|---|
| **Cost** | Small. The cancel already runs on a thread worker (`@work(thread=True)`), so a loop there costs no UI responsiveness. |
| **What could go wrong** | **We have no data that a retry helps.** We have one n=10 sample of the *timing* distribution and nothing at all on whether a second `taskkill` against the same tree succeeds where the first timed out. If the overrun is caused by system-wide contention, a retry samples the same contention and fails the same way — and each attempt costs another 15s of a busy box's time, making the contention worse. That is the self-amplification in (a) with a pump attached. |
| **Before choosing it** | Measure one thing: **given a first `taskkill` that exceeded 15s, what fraction of second attempts succeed?** That is a cheap experiment and it decides (b) on its own. Choosing (b) without it is tuning a constant on a hunch. |
| **Honest note** | Sentinel ruled no retry for `23cbf78` on exactly this reasoning, and I agree with it. (b) is listed because it is the obvious next thought, not because I recommend it. |

---

## (c) Replace the walk: a Windows **Job Object** with `KILL_ON_JOB_CLOSE`

Instead of walking the tree at kill time, put the child **in a job** at spawn time. Every
descendant it creates inherits the job. Killing is then closing one handle — the kernel
terminates every member. There is no walk, so there is nothing to time out.

### Is it reachable from how `tool_bash` spawns today? **YES — probed, not reasoned.**

Against the exact shape `ttyguard.popen` uses (`shell=True`, `CREATE_NO_WINDOW`,
`subprocess.Popen`), 4 trials:

```
AssignProcessToJobObject   ->  True, all trials
grandchild killed          ->  yes, 0 survivors
latency                    ->  0.1 - 0.2 ms
```

Compare `taskkill` on the same tree shape: **3,420 – 43,060 ms.** That is four to five orders
of magnitude, and it removes the timeout rather than widening it.

🔴 **A correction I have to make about my own probe.** My first run reported *"this process is
already inside a job: False"*. **That was wrong** — my `IsProcessInJob` call had no `argtypes`
declared, and undeclared `ctypes` truncates a `HANDLE` to 32-bit on Win64. Declared properly,
the answer is **True: this process is already inside a job.** Which means my very first probe
was *already* the nested case and I did not know it. The conclusion gets **stronger** — nested
assignment works in the real environment, on this box, today — but two facts I stated were
false, and the instrument that produced them was mine.

### What changes in `tool_bash`'s contract

- **Spawn** gains one step: create the job, assign the child. `ttyguard.popen` is the single
  place that spawns, so it is one edit, not a scatter.
- **Cancel** becomes: close the handle. `kill_tree`'s `bool` stays but is almost always
  `True`, and the honest-warning paths from `23cbf78` become the rare fallback rather than
  the routine one. **Nothing built in `23cbf78` is wasted or contradicted.**
- **Lifetime** gains an owner: the job handle must live as long as the child and be closed
  exactly once. `CANCELLABLE` already carries per-child state and is the natural home.
- **Dependency:** none. `ctypes` is stdlib. No `pywin32`.

### What could go wrong

1. **A race at spawn.** Between `Popen` returning and `AssignProcessToJobObject`, `cmd.exe`
   could already have spawned the grandchild — which would then be outside the job and would
   survive. **I did not measure this window.** It is microseconds against a `cmd.exe` startup
   of milliseconds, so it is unlikely, but "unlikely" is what the 1-in-10 timeout was too.
   Mitigations, in order of preference: keep `taskkill` as a fallback when the job reports no
   members; or verify membership with `IsProcessInJob` on the grandchild before relying on the
   job. The fallback costs nothing and makes the race non-fatal.
2. **Killing yourself.** A job created with `KILL_ON_JOB_CLOSE` that contains *your own*
   process kills the app when the handle closes. Never assign the parent; only ever the child.
   I hit this hazard while probing and had to deliberately leak a handle to avoid it.
3. **Windows-only.** `taskkill` already is, so this narrows nothing — but if this app ever
   targets another platform the kill path needs a second implementation. Today it does not.
4. **Nested jobs need Windows 8+.** This box is build 26200 and it is verified working. Any
   supported Windows is fine; it is worth stating rather than assuming.

---

## What I would tell Ryan

**(c), with `taskkill` kept as the fallback.** It removes the failure instead of reporting it
faster, it is stdlib-only, it is proven reachable against the real spawn shape including the
nested case, and it makes the honest-warning machinery from `23cbf78` a rare path rather than
a routine one. The one unmeasured risk — the spawn race — is neutralised by the fallback we
already have.

**(b) should not be chosen without the retry-success measurement**, and if (c) lands, (b)
stops being interesting.

**(a) is not a resting place.** It is honest about a product that cannot reliably stop what it
started, and the failure it reports amplifies itself.

---

## Re-run any of this

The probes are scratchpad-only and deliberately not committed as product code; they are
throwaway instruments, and one of them was wrong in a way worth remembering. Ask me for them
and I will hand them over with the `argtypes` bug fixed.
