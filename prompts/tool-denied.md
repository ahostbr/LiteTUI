<!--
  Every refusal LiteTUI hands back to the model in place of a tool result.

  EDIT THE PROSE FREELY. Two things are not free:

    * Keep the `## key` headings exactly as they are. The loader finds sections
      by heading; renaming one is the same as deleting it.
    * Keep the {placeholders} listed beside each heading. A section that loses
      one is treated as BROKEN and the built-in fallback in textfmt.py is used
      instead — so the refusal still happens and still names the tool. It never
      renders a literal {name} at the model, and it never goes silent.

  `tests/test_tool_denied.py` fails loudly if a heading or placeholder goes
  missing here, which is where you find out — not at the moment of a refusal.

  Substitution is plain replacement, not str.format(), so stray braces in this
  prose cannot crash anything.
-->

## unknown-tool
<!-- placeholders: {name} -->

[error] unknown tool: {name}

## no-metadata
<!-- placeholders: {name} -->

[policy denied] {name}: no capability metadata. LiteTUI will not run a tool
whose authority it cannot describe, so nothing ran and nothing changed.

## profile
<!-- placeholders: {name}, {reason} -->

[policy denied] {name}: {reason}. Nothing ran and nothing changed. This is the
active authority profile refusing, not the user — do not ask them to approve
it and do not retry.

## by-user
<!-- placeholders: {name} -->

[policy denied by user] {name} — the user was asked and refused, so nothing ran
and nothing changed. THE TURN ENDED THERE, by their choice. Do not retry this
call, do not reach for a different tool to accomplish the same thing, and do
not treat the refusal as an obstacle to work around. Wait for their next
message.

## tool-disabled
<!-- placeholders: {name} -->

[disabled] The user has switched `{name}` OFF in the tool list, so nothing ran
and nothing changed. Its schema is not offered to you any more; you are seeing
this because the name reached the host anyway. Only the user can switch it back
on (/tools). Do not retry it, and do not reach for a different tool to
accomplish the same thing — they turned this one off on purpose. Say plainly
that it is off, then carry on with what you can still do.

## tools-off
<!-- placeholders: none -->

[disabled] Tools are turned OFF in LiteTUI, so nothing ran and nothing changed.
Only the user can turn them on: Ctrl+T, or Settings -> Agent loop -> Tools
enabled. Tell them that in plain language, then answer as best you can without
tools. Do not retry and do not try another tool.
