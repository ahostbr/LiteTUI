# Cancel, then JIT — what a refused load does not prevent

T703. A **reading**, not a fix. Ryan's residual from 2026-09-12: after pressing
**Cancel** on the second-instance VRAM modal (T690), a later chat turn can still
make LM Studio load the refused model server-side.

Everything below is read from source at `39a47c4`. **No request was sent to any
model server to produce it** — the standing rule is that nothing probes a model
into VRAM, and a trace that needed a load to confirm itself would be the bug it
is describing.

---

## 1. The mechanism, in four steps

1. `switch_model` (`plugins/model_switch.py:52`) assigns **`app.model_id = target`
   first**, then drives the effects. The assignment is not conditional on the
   load succeeding.
2. One of those effects is `app.apply_context_length()` (`app.py:3680`), which
   is where a load actually happens.
3. The T690 gate lives inside the backend: `_VramGate.vram_guard`
   (`llm_backend.py:640`) asks, and on Cancel raises
   `VramRefused` (`llm_backend.py:650`).
4. **`VramRefused` is raised in exactly one place and caught in none.**
   `grep -rn "VramRefused" --include=*.py src/` returns two hits: the class
   and the raise. It is a `BackendError` subclass, so it is swallowed by
   whatever catches `BackendError` — and `apply_context_length` is a
   `@work(exclusive=True, group="ctxload")` worker, so the exception ends its
   worker rather than the switch.

**The result: the load is refused and the SELECTION is not.** `app.model_id`
is left naming the model the human just declined, and the next turn sends it —
and LM Studio JIT-loads whatever a chat request names. Nothing in the app is
lying; no component failed. The gate guards `backend.load()`, and this path
never calls it.

> **A REFUSAL THAT DOES NOT MOVE THE STATE THAT CAUSED IT IS A REFUSAL THE NEXT
> ACTION UNDOES.**

There is no record anywhere that this instance refused this model: no field, no
set, no timestamp. That absence is the whole of the residual.

---

## 2. Every path that names a model, and what guards it today

Derived from `grep -rn '"model"' --include=*.py src/` plus the endpoint scan
(`v1/chat/completions`, `/models/load`, `/embeddings` — there are no embeddings
calls in the tree).

| # | Path | Where | Guard today | JIT risk |
|---|------|-------|-------------|----------|
| 1 | **Main chat turn** | `turn_engine.py:168` and `:251` | `_ensure_chat_ready` (`app.py:4992`) runs pre-flight | 🔴 **YES for a human** — see §3 |
| 2 | **Thinking probe** | `thinking_probe.py:92`, `:176` | its own `_headless_model_decision` call at `app.py:4215` | 🔴 **YES for a human** |
| 3 | **Explicit load** | `llm_backend.py:1304` `POST /models/load` | ✅ T690 `vram_guard` | no |
| 4 | **Subagent tool** | `plugins/subagent_plugin.py:131` | `_resolve_model` requires the model be in the loaded set (`:80`) | ✅ no |
| 5 | **Tool-summary fold** | `app.py:1754` | `model_residency.resolve_side_call_model` degrades a cold pick to the main model and says so once | ✅ no |
| 6 | **Remote providers** | `model_transport.py:170/266/420/468` | codex/claude — not local VRAM | n/a |
| 7 | **Audio listen** | `listen_tool.py:311` | separate `llama-server` on `AUDIO_PORT`, fixed name | n/a |

**So it is not one chokepoint — it is two, and they are already separate.**
Rows 4 and 5 are already residency-aware; rows 1 and 2 are the exposure, and
they deliberately do not share a door: `_probe_thinking`'s own docstring
(`app.py:4199`) records that it POSTs **through urllib, not through
model_transport, so nothing `_ensure_chat_ready` does reaches it**. Any fix that
guards only the chat path leaves the probe, and the probe fires on `connect()`
without a user turn at all.

---

## 3. Where a "refuse the turn" guard would sit

**The seam already exists and already refuses.** `_ensure_chat_ready`
(`app.py:4992`) is a pre-flight query that runs before `create()`, and it
already has a refuse branch:

```python
if getattr(self, "_rpc", False):           # app.py:5022
    action, model, why = self._headless_model_decision()
    if action == "refuse":
        ...
        raise llm_backend.BackendError(f"model not loaded — {why}")
```

It is gated on `_rpc` **on purpose**. `llm_backend.py:1581` states the rule
plainly: a cold model is not an error for LM Studio, because *"a person typing
a prompt wants the load"*, and refusing would break chats that work today. T594
carved out headless children precisely because nobody is watching them.

So the fix is not new machinery. It is **one more condition on an existing
refusal**: refuse when this instance has an outstanding Cancel *for the model
the request is about to name*. That needs the one thing §1 says is missing — a
record of the refusal — which `vram_guard` is the natural place to write, since
it is already the only code that knows a human said no.

Two sites, not one, because of row 2:

- `_ensure_chat_ready` — covers the chat turn, compact, and the goal loop
  (`app.py:3327`, `:5179`, `:5846`, `goal_loop.py:233`).
- `_probe_thinking` (`app.py:4215`) — already calls
  `_headless_model_decision()` for the headless case and would need the same
  extra condition.

⚠️ **A third site would be needed if the refusal is meant to survive a restart.**
Nothing here persists it. Per-conversation settings (T691) would carry it, but
a VRAM refusal is a property of *this box right now*, not of the conversation —
recording it in `.convos/<id>/settings.json` would make a decision about a
machine follow a transcript onto a different one.

---

## 4. What the user would see

Today, after Cancel:

- the picker shows the refused model as selected (`model_id` moved);
- the footer shows it;
- the next message loads it anyway, and the only clue is the pause;
- **nothing says a load happened** — this is the same shape as the measurement
  in `_headless_model_decision`'s docstring, where six probe children put a
  second 27B beside Ryan's and the `ready` payload looked identical either way.

Under "refuse the turn" the honest version is a refusal in the chat, naming
both the model and the reason — and it has to answer an obvious question:
**what is the user now on?** Three shapes, and they are not equivalent:

1. **Refuse and stay** — the turn is declined, `model_id` still names the
   refused model, and every subsequent turn is refused until they switch. Safe,
   and reads as the app being stuck.
2. **Refuse and roll back** — `model_id` returns to the previously loaded
   model, with a line saying so. The switch is undone, which is what Cancel
   *looked* like it did. This is the only option where the picker stops lying.
3. **Refuse once, then allow** — the first turn is declined with a warning and
   the second proceeds. This is what a "warning" usually means, and it is
   exactly what Ryan's T690 ruling rejected for loads: *"they go through a
   warning modal EVERY TIME."*

⬜ **Option 2 is the one that matches what Cancel appears to promise**, but it
is a real behaviour change — a cancelled switch becomes a cancelled switch
rather than a cancelled load — and that is Ryan's call, not mine.

---

## 5. What llama.cpp does in the same case

**Nothing — the residual is LM Studio-only.** `llm_backend.py:1588`, verbatim:

> *"our router will not JIT-load (it is started `--no-models-autoload`, by law),
> and LM Studio will."*

So on llama.cpp a refused model stays unloaded, and the next turn gets a clean
`400 model is not loaded` from the router, which `ensure_chat_ready` already
turns into words for a human (D2/D11). The asymmetry is documented as *"the
truth about the two engines, not an oversight"*.

⚠️ **One caveat that cuts the other way.** llama.cpp will not JIT, but its
`--models-max` ceiling is shared, so a load that DOES happen there can evict a
model the other window is mid-turn on. That is `eviction_notice`, it is a
different defect, and it is already documented in README's "Two instances at
once". Do not fold the two together.

---

## 6. The question for Ryan

The T690 ruling was about **loads**: every load, every time, a modal. This is
about what the refusal *means afterwards*, which that ruling did not reach.

- **Accept it** — Cancel refuses the explicit load; a later turn may still JIT,
  because a person at a keyboard asked for a turn and LM Studio's convenience
  is deliberate. Cost: the modal can be dismissed and the VRAM taken anyway,
  one message later, with nothing said.
- **Refuse the turn** — a cancelled model is refused until the human picks
  another, at one of the three shapes in §4. Cost: a new way for the app to
  decline to answer, and a second guard site for the thinking probe.

**Recommendation withheld deliberately.** The last time I turned a question of
this shape into a recommendation (owner-pid on `router.json`), Ryan ruled the
opposite, and the mechanism analysis was sound while the inference about the
GOAL was invented. What is measurable is above; which behaviour is wanted is
his.

---

## Re-runnable

```bash
git -C C:/Projects/LiteTUI grep -n "VramRefused" -- 'src/**/*.py'     # 2 hits: class, raise
git -C C:/Projects/LiteTUI grep -n '"model"' -- 'src/**/*.py'          # every named-model request
git -C C:/Projects/LiteTUI grep -n "_ensure_chat_ready\|_headless_model_decision" -- 'src/**/*.py'
```

🔴 **UNVERIFIED BY OBSERVATION.** This is a source trace. Nobody has cancelled
the modal and then watched `lms ps` across a turn, and doing so would itself
load a model — so the reading stops where measurement would cost what it is
trying to prevent.
