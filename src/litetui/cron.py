"""The cron scheduler — T070 step O3.

`CronService` OWNS the job list. That is the difference between this module and
`appsvc`: appsvc holds helpers that take `app` and READ it, and its own docstring
says a function that needs to write `app.<something>` "belongs back on the class
or behind a real service object". This is that service object, and `jobs` is the
state it owns outright -- nothing in `app.py` writes it any more.

`app.jobs` still works: the property delegates here, so `scheduler_ui.py`'s
twelve reads and its in-place edits are untouched.

⚠️ `monitor` IS A MODULE FUNCTION, NOT A METHOD, AND THE FRAMEWORK DECIDES THAT.
`textual.work` asserts `isinstance(args[0], DOMNode)` before scheduling, so a
`CronService` cannot carry a `@work` method -- but a module-level function whose
first argument is the app satisfies it exactly. Proven by probe, negative control
included (PLAN §3b): the worker ran to SUCCESS with its group intact, and the
same decorator with a non-DOMNode first argument raised AssertionError.

🔴 `_fire_job` IS NOT HERE, DELIBERATELY. It writes `_pending_input` and
`_active_tool_profile` and calls `_chat_running`, `_handle_command`,
`_user_bubble`, `_append` and `_stream`: it is the seam between the scheduler and
the TURN ENGINE, and it sits on the turn-engine side of it. A service that owns
the job list cannot own the input queue. Holding it is what makes this a clean
extraction -- with it, every method here writes ONLY `self.jobs`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual import work

from litetui import paths
from litetui import runtime_log
from litetui import textfmt
from litetui import scheduler as sched_mod


class CronService:
    """Owns the scheduled-job list and the /cron verbs.

    Takes `app` because the verbs still speak to the user and fire prompts
    through it. That coupling is visible in `self._app` rather than hidden.
    """

    def __init__(self, app) -> None:
        self._app = app
        self.jobs: list = sched_mod.load(paths.data_root())

    def command(self, arg: str) -> None:
        """/cron — add, list, remove, enable, disable, or fire a job now."""
        arg = arg.strip()
        if not arg or arg.lower() in ("list", "ls"):
            self.list_jobs()
            return

        verb, _, rest = arg.partition(" ")
        verb = verb.lower()
        rest = rest.strip()

        if verb == "add":
            self.add(rest)
            return

        if verb in ("rm", "del", "remove"):
            job = self.find(rest)
            if not job:
                return
            self.jobs.remove(job)
            sched_mod.save(self.jobs, paths.data_root())
            self._app._system(f"/cron: removed {job.id} ({job.label or job.prompt[:40]})")
            return

        if verb in ("on", "off", "enable", "disable"):
            job = self.find(rest)
            if not job:
                return
            job.enabled = verb in ("on", "enable")
            sched_mod.save(self.jobs, paths.data_root())
            self._app._system(f"/cron: {job.id} is now {'ON' if job.enabled else 'OFF'}")
            return

        if verb == "run":
            job = self.find(rest)
            if not job:
                return
            # Fires REGARDLESS of schedule and of enabled — "run" is the human
            # asking for it now, which is a different act from the schedule
            # coming round. It still stamps the slot, so an actual due-time
            # inside this same minute will not double up.
            self._app._fire_job(job)
            return

        self._app._system(
            "/cron add <schedule> <prompt>   e.g. /cron add @daily summarise my inbox\n"
            "/cron list | rm <id> | on <id> | off <id> | run <id>\n"
            "schedule: 5-field cron (min hour day month weekday) or "
            "@hourly @daily @weekly @monthly"
        )

    def find(self, token: str):
        """Resolve an id prefix or a label to exactly one job, or say why not.

        Ambiguity is reported rather than resolved to the first match: picking
        one silently is how the wrong job gets deleted.
        """
        token = token.strip()
        if not token:
            self._app._system("/cron: which job? Use /cron list to see ids.")
            return None
        hits = [j for j in self.jobs
                if j.id.startswith(token) or (j.label and j.label == token)]
        if not hits:
            self._app._system(f"/cron: no job matches {token!r}. /cron list shows them.")
            return None
        if len(hits) > 1:
            ids = ", ".join(j.id for j in hits)
            self._app._system(f"/cron: {token!r} matches {len(hits)} jobs ({ids}) — be more specific.")
            return None
        return hits[0]

    def add(self, rest: str) -> None:
        if not rest:
            self._app._system("/cron add <schedule> <prompt>")
            return

        tokens = rest.split()
        if tokens[0].startswith("@"):
            schedule, prompt = tokens[0], " ".join(tokens[1:])
        elif len(tokens) > 5:
            schedule, prompt = " ".join(tokens[:5]), " ".join(tokens[5:])
        else:
            self._app._system(
                "/cron add: a schedule is 5 fields (min hour day month weekday) "
                "or an @alias, followed by the prompt.\n"
                "  /cron add 0 9 * * 1-5 what is on for today?"
            )
            return

        if not prompt:
            self._app._system("/cron add: that schedule parsed, but there is no prompt after it.")
            return

        try:
            cron = sched_mod.Cron.parse(schedule)
        except sched_mod.CronError as e:
            # The error names the FIELD. "invalid cron expression" would leave
            # the person guessing which of five to fix.
            self._app._system(f"/cron add: {e}")
            return

        job = sched_mod.Job(prompt=prompt, schedule=schedule)
        self.jobs.append(job)
        sched_mod.save(self.jobs, paths.data_root())

        nxt = cron.next_after(datetime.now())
        when = nxt.strftime("%a %d %b %H:%M") if nxt else "never (no matching date)"
        self._app._system(
            f"/cron: added {job.id} — next fire {when}\n  {prompt}\n"
            f"  {textfmt.SCHEDULED_AUTO_NOTE}"
        )

    def list_jobs(self) -> None:
        if not self.jobs:
            self._app._system(
                "/cron: no jobs.\n"
                "  /cron add @daily summarise what I did yesterday\n"
                "  /cron add */30 * * * * check the build"
            )
            return

        now = datetime.now()
        lines = [f"{len(self.jobs)} job(s) — jobs fire only while LiteTUI is open"]
        lines.append("")
        for job in self.jobs:
            try:
                nxt = job.next_after(now)
                when = nxt.strftime("%a %d %b %H:%M") if nxt else "never"
            except sched_mod.CronError as e:
                # A job that can never fire must SAY so here. Silently listing
                # it beside working jobs is how it sits dead for weeks.
                when = f"BROKEN — {e}"
            state = "on " if job.enabled else "off"
            head = f"  {job.id}  {state}  {job.schedule:<16} next {when}"
            lines.append(head)
            lines.append(f"        x{job.run_count}  {job.prompt}")
        self._app._system("\n".join(lines))


@work(exclusive=True, group="cron")
async def monitor(app) -> None:
    """Fire scheduled prompts while the app is up.

    Its own worker group, deliberately. Sharing "chat" with _stream would
    make every tick cancel the turn in flight -- the exact trap autocompact
    fell into, where a checker started work from inside the work it was
    checking.
    """
    while True:
        await asyncio.sleep(sched_mod.TICK_SECONDS)
        try:
            ready = sched_mod.due(app.cron.jobs, datetime.now())
        except Exception as e:
            # T065 producer #13. The approved list named this at app.py:1914;
            # the T070 decomposition moved the monitor here, so the producer
            # follows the CODE, not the line number. `site` says where it now
            # lives — a site label that still said "app.cron_monitor" would
            # send the next reader to a function that no longer exists.
            #
            # `except Exception:` was ALREADY the clause. Binding `as e` is
            # inert, which is the only reason this is allowed: the T065 ruling
            # forbids widening an except clause to make room for a log.
            runtime_log.record(
                "scheduler_tick_failed",
                site="cron.monitor",
                component="scheduler",
                operation="due",
                error_type=type(e).__name__,
            )
            continue  # a scheduling bug must never take the chat down
        for job in ready:
            app._fire_job(job)
