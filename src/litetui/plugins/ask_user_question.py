"""Structured questions to the human — registration for ask_user_question.

This migration is also where the satellite's set_app() module-global died:
the one genuinely impure hook in the old tool set. The app now rides in as
an argument closed over at registration, so no module holds a hidden App
reference and two apps in one process (the test suite's normal state)
cannot cross wires.
"""
from litetui import ask_user_question as auq_mod
from litetui.plugins import PluginManifest
from litetui.tool_policy import USER_QUESTION_POLICY


def _register(ctx) -> None:
    app = ctx.app
    # Always available: it renders inside this very app and has no external
    # precondition (unlike the browser tools' SCRIPT check).
    ctx.tool(
        auq_mod.ASK_USER_QUESTION_TOOL_SPEC,
        lambda args: auq_mod.run(args, app),
        policy=USER_QUESTION_POLICY,
    )


PLUGIN = PluginManifest(id="ask-user-question", register=_register)
