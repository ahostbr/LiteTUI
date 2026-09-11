"""Offline caching audit: no credentials, model calls, or app startup.

Astra's original audit (Codex Desktop seat 01a0914a, 2026-09-11 17:44) is what
produced T625. Its structural findings still hold; ONE assertion had gone stale
and, because it was an `assert`, it aborted the script four lines before the
`print` — so the file published nothing while still reading as a standing
finding to anyone who opened it.

🔴 WHAT WENT STALE, AND WHY IT MATTERED MORE THAN A WRONG NUMBER. The probe
asserted that `_usage` returns exactly three normalised fields and printed
`cached_and_write_token_details_discarded: true`. T624 (`741ddbd`,
"retain optional cache details through transport and app") widened `_usage` to
carry `cached_tokens`, `cache_write_tokens`, `input_tokens_details` and
`usage_details` as well, so the claim is now the opposite of true — and the
blocker Astra recorded, that the transport throws away exactly what a caching
audit needs, IS GONE.

    A RED ARM THAT DIES BEFORE ITS OWN REPORT PUBLISHES NOTHING AND STILL READS
    AS A STANDING FINDING, because the claim is in the source whether or not the
    run ever reaches it. That is the "a body is true only at its own timestamp"
    problem in an executable file, where it is worse: it looks like a live check.

🔴 AND IT LIVED WHERE NOBODY COULD SEE IT CHANGE. The original sat in
`artifacts/`, which .gitignore:63 holds untracked by adjudication (2026-08-21):
no diff, no review, no gate. Nothing could have reported that T624 had falsified
its printed claim.
    A FILE NOBODY CAN SEE CHANGE IS A FILE THAT CANNOT BE MAINTAINED — so fixing
    the assertion and leaving it there would have fixed today's wrong sentence
    and guaranteed tomorrow's. This copy lives in `scripts/` beside its siblings
    (t526_rpc_probe, t531_parallel_probe, t558a_ask_over_rpc_manual); Astra's
    original bytes stay on disk in artifacts/, untouched, as the adjudication
    requires.

⬜ SO THE STALE EQUALITY IS INVERTED INTO A GUARD rather than deleted. It now
asserts the three normalised fields are right AND that the cache details are
PRESENT, so narrowing `_usage` again turns this red for the correct reason. The
three structural assertions below are untouched: they are T625's precondition
and they were green on 2026-09-11 against main 64707a7.
"""
import copy
import json

from litetui.model_transport import _usage, codex_request
from litetui.plugins import PluginRegistry

messages = [{"role": "system", "content": "Stable instructions"},
            {"role": "user", "content": "First question"}]
kwargs = {"model": "gpt-6-astra", "messages": messages}
first = codex_request(kwargs)
later = codex_request({**kwargs, "messages": messages + [
    {"role": "assistant", "content": "First answer"},
    {"role": "user", "content": "Follow-up"}]})
assert first["instructions"] == later["instructions"]
assert first["input"] == later["input"][:len(first["input"])]

raw = {"input_tokens": 12000, "output_tokens": 100,
       "input_tokens_details": {"cached_tokens": 10000, "cache_write_tokens": 2000}}
usage = vars(_usage(raw, "codex"))
# The normalised three, unchanged by T624 — checked as a SUBSET, because an
# equality here is what went stale: it fails on any widening, including one that
# adds exactly what this audit needs.
assert usage["prompt_tokens"] == 12000
assert usage["completion_tokens"] == 100
assert usage["total_tokens"] == 12100
# The inverted claim. Present, and carrying the values, since T624 (741ddbd).
assert usage["cached_tokens"] == 10000, "T624's cache details were narrowed away again"
assert usage["cache_write_tokens"] == 2000, "T624's cache details were narrowed away again"
assert usage["input_tokens_details"] == raw["input_tokens_details"]

registry = PluginRegistry()
for name in ("discover", "read_file"):
    registry.add_tool("probe", {"type": "function", "function": {
        "name": name, "description": name, "parameters": {"type": "object", "properties": {}}}}, lambda: None)
registry.deferred_static = frozenset({"read_file"})
before = copy.deepcopy(codex_request({**kwargs, "tools": registry.tool_specs()}))
registry.search_tools("select:read_file")
after = codex_request({**kwargs, "tools": registry.tool_specs()})
# T625's precondition: ONE discovery permanently grows the top-level tools array,
# which sits at the front of the cacheable prompt.
assert before["tools"] != after["tools"]
assert [t["name"] for t in before["tools"]] == ["discover"]
assert [t["name"] for t in after["tools"]] == ["discover", "read_file"]
registry.tools_disabled = lambda: frozenset({"read_file"})
disabled = codex_request({**kwargs, "tools": registry.tool_specs()})
assert disabled["tools"] == before["tools"]

print(json.dumps({"ordinary_history_prefix_preserved": True,
    "normalized_usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
    "cache_details_retained_since_T624": {
        "cached_tokens": usage["cached_tokens"],
        "cache_write_tokens": usage["cache_write_tokens"]},
    "discovery_changes_top_level_tools": True,
    "tools_before_discovery": [t["name"] for t in before["tools"]],
    "tools_after_discovery": [t["name"] for t in after["tools"]],
    "disable_removes_top_level_schema": True,
    "network_requests": 0}, indent=2))
