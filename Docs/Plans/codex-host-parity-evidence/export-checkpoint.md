# C3 saved transcript export checkpoint

No conversation-export route existed in the current LiteTUI source. Added a
read-only CLI route, --export-conversation plus --export-output, before application
construction. It replays the production conversation store and projects visible
messages and native display records to Markdown. Native identity deduplication
uses the latest saved snapshot at the first visible position; mirrored assistant
aggregate text is suppressed. Phases/states and known zero duration remain explicit.
Embedded image bytes are replaced with a placeholder. Literal fences are longer
than any backtick run in their contents. Output uses exclusive creation.

Validation: 29 export/history/CLI model-flag tests pass. Export tests verify mirrored
metadata, newer recovered results, independent thread identities, image omission,
literal Markdown, unknown/zero timing, saved-edit replay, source byte preservation,
existing-output rejection and the actual CLI branch without constructing LiteTUI.
Ruff passes the new module/tests. cli.py retains its pre-existing unused `remaining`
diagnostic (confirmed in HEAD). No live inference or app launch was used.

Remaining export gates: structured question cards, attachment copy/reference policy,
and full GUI export behavior. Other C3 lifecycle and full-plan release gates remain
open; this is not acceptance of a narrower export contract.
