# BlackGrid — your state across the compaction (Sentinel, 2026-08-19)

Ryan paused messages to you so you could compact and restart on a newer build. This is a file
rather than mail. Read it before you act — it holds what only existed in your context.

## 1. ✅ YOUR PHASE 1 IS COMMITTED. `f5779bc`.

It was sitting entirely untracked when Ryan told me to commit it for you:

```
 M harness.py        4 sites rerouted        ← was uncommitted
 M mcp_client.py     1 site rerouted         ← was uncommitted
?? ttyguard.py       4,720 b, THE ENVELOPE   ← existed in ONE place, untracked
?? test_ttyguard.py                          ← untracked
?? config.py                                 ← untracked
```

Compaction never threatened those files — it does not touch disk. A stray `git clean` would
have. The working tree is clean now; nothing of yours is at risk.

**The commit is attributed to you** — `Agent-Name: BlackGrid`, `Agent-Tier: worker`, your
Agent-ID, with `Committed-By: Sentinel`. The design and the code are yours. I verified before
committing rather than taking your report: I read `ttyguard.py` and `config.py` in full, read
both diffs, and ran everything.

**Regression at that commit, with your rerouting live:** footer 28 · autoscroll 23 · sanitize 16 ·
input 22 · modals 24 · **integrations 48 (the real MCP server through `ttyguard.popen`)** ·
store 21 · harness-tool 30 · convos 8. `test_ttyguard` 4/5 — see below.

## 2. 🔴 THE SOURCE SCAN IS RED, AND THAT IS CORRECT

`test_no_child_process_escapes_the_envelope` fails right now, naming its own remaining work:

```
app.py:294   proc = subprocess.run(
app.py:1870  out  = subprocess.run(
```

That is the gate refusing while the job is half done. **It is your phase-2 checklist, not a
broken test.** I said so in the commit message so nobody switches it off to get a green suite —
a scan that gets disabled is exactly how this class of bug comes back.

Your regex is `(`-anchored, so it counts CALLS and not mentions, and whitelists the envelope and
its own test file. That is the honest version and it is better than the spec I gave you: you
covered `call`, `check_output` and `check_call` as well, which I had not named.

## 3. WHAT PHASE 2 IS — YOUR OWN WORDS

> *"Phase 2 — your two app.py spawn sites, the imports, the three config sites, and the
> choke-point move — is prepped and verified against the current file, but NOT run. Nothing is
> mid-edit in app.py right now."*

`app.py` is free. I finished tok/s and handed it back; HEAD of your work sits on top of
`f743dc6`.

⚠️ **`config.py` is committed but NOT WIRED — nothing imports it yet.** It is dead code until
phase 2 does the three replacements. I said that in the commit message too, so a reader does not
assume it is live.

⚠️ **`app.py` has moved since you prepped phase 2.** It gained the tok/s meter (`f743dc6`):
`_tps_start` / `_tps_tick` / `_tps_final`, a `tps` reactive, `_append_tps`, and three one-line
insertions inside the stream loop. Your prepped edits were "verified against the current file" —
that file is no longer current. **Re-verify your anchors before running them.** Line 1870 in
particular is not where it was.

## 4. YOU WERE RIGHT ABOUT THE COUNT. I WAS WRONG, TWICE.

You said **7 spawn sites in 3 files**. I said 8 in 5. Measured against `f743dc6`, before your
phase 1:

| file | real spawn calls |
|---|---|
| app.py | 2 |
| harness.py | 4 |
| mcp_client.py | 1 |
| skills.py | **0** |
| sanitize.py | **0** |

**7 across 3. You are right.** Two separate errors, both the exact class of thing I had just
finished flagging in someone else's numbers:

1. **My regex counted a declaration as a call.** My alternation had bare `subprocess\.Popen` with
   no `(`, which matched `mcp_client.py:53` — `self.proc: subprocess.Popen | None = None`, a type
   annotation. That was my phantom eighth.
2. **"Five files" was the number I passed to grep, not the number that matched.** Two had zero
   hits. I reported the shape of my query as the shape of the result.

So when your scan passes at seven, it is correct. **There is no eighth. Do not hunt for one on my
say-so.**

## 5. ONE OBSERVATION FROM RUNNING YOUR TESTS

`test_ttyguard` printed live mode sequences into my console — `[?1000l[?1003l[?1015l[?1006l` and
their `h` counterparts — because `_repair_terminal()` writes to `sys.__stdout__` unconditionally.
Your docstring says the write is meaningless under redirection, and it is, but "meaningless" is
not "invisible": it reaches whatever console runs the suite. Harmless, not a blocker, and worth
a guard if it ever annoys you.

## 6. NOT YOURS RIGHT NOW

- **LiteTUI is OUT of the LiteSuite installer** until it is a first-class citizen — Ryan's ruling
  at the 0.0.56 release gate. Nothing here is urgent and nothing needs pushing. `main` is
  7 ahead of origin and that is fine.
- `reasoning_tokens` is on the wire and unread. Filed, not actioned; Ryan has not asked. In my
  probe **11 of 15 completion tokens were reasoning — 73%**.
- Inbox durability is still yours and still owed, after TtyGuard. Two conditions from my triage:
  injection must be idempotent, and the replay must be bounded.
