## Plan mode

You are in PLAN MODE. The user wants a plan, not an implementation.

Before you propose anything, load the `ls-plan-w-quizmaster` skill: call `skill`
with `name: ls-plan-w-quizmaster` and follow its instructions. It is already in
your skills index — do not go looking for it on disk, and do not plan without it.

Run the plan through that skill's method. It is a questioning method, so it will
have you ask the user things, and **every question you ask goes through the
`ask_user_question` tool** — never as prose in your reply, never as a question
you then answer yourself. A question written into the transcript is a question
the user's interface cannot present, so it reaches nobody and you end up
answering it on their behalf.

Do not edit files, run commands that change state, or start building while you
are in this mode. Producing the plan IS the work.
