# C2 follow-up diagnostics prepared offline

The probe now captures fixed enumerations for hook tool categories (spawn, wait,
list, search, other), allow/deny decisions, hook completion states, native item
types, lifecycle phase, spawn-gate consumption and child-identity observation.
Unknown protocol values collapse to other. No prompt, argument, result, free-form
tool name or hook error text is added to the diagnostics. The original failed-run
JSON is preserved; these new fields were not retroactively claimed for it.

The generated gate is factored into a callable source builder so its exact helper
can be tested without a model. Two offline tests pass. Fresh Python helper calls
with synthetic inputs prove first spawn allowed, second spawn denied, discovery
denied, wait allowed and unknown tool denied. Sentinel strings in tool name,
prompt and path never reach helper stdout or diagnostic files. Unknown status
objects/strings collapse to other. Ruff passes script and tests.

This preserves the original probe permissions. Discovery denial is confirmed for
the synthetic gate fixture, not established as the cause of the earlier live
failure. No additional native delegation run was performed or is authorized yet.
Sentinel review is still required before repeating live delegation.
