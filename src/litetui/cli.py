"""The console-script entry point — thin on purpose.

This used to be ``litetui.app:main``, but importing app.py builds a Textual
application (it imports textual at module level), so even ``litetui --version``
paid the full cold-start cost before printing one line. Every package manager,
wizard, and curious user runs exactly that probe on first install; this launcher
checks argv BEFORE touching app, so the version path costs only sys + version.

The heavy import is deferred to the last possible moment — a ``--version`` probe
never sees it, a real launch pays it once. See version.py for why reading the
number itself must stay cheap.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    # Fast path for probes: --version / -V print and exit before app.py (and its
    # Textual import) is ever loaded.
    if any(a in ("--version", "-V") for a in sys.argv[1:]):
        from litetui.version import __version__

        print(f"litetui {__version__}")
        return

    parser = argparse.ArgumentParser(
        prog="litetui",
        description="LiteTUI — a terminal interface for local LLMs",
        add_help=False,
    )
    parser.add_argument("--version", "-V", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--rpc", action="store_true", help="headless JSONL-over-stdio mode")
    parser.add_argument("--backend", choices=['codex', 'lmstudio', 'llamacpp', 'ninfer'], default=None, help='invocation-only backend selection')
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument('--reasoning-effort', default=None)
    thinking.add_argument('--thinking-level', default=None)
    parser.add_argument("--model", type=str, default=None, help="model slug to select on start")
    parser.add_argument("--prompt", type=str, default=None, help="first turn to submit once ready")
    parser.add_argument("--system-prompt", type=str, default=None, help="prepend a system message")
    parser.add_argument("--cwd", type=str, default=None, help="change working directory before start")
    parser.add_argument(
        "--tool-profile",
        type=str,
        default=None,
        choices=["autonomous", "interactive", "scheduled"],
        help="tool policy profile (default: autonomous when --rpc, else settings)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default=None,
        choices=["normal", "plan"],
        help="plan: load ls-plan-w-quizmaster and ask through ask_user_question (T558)",
    )
    parser.add_argument("--convo", type=str, default=None, help="resume a conversation by id")
    parser.add_argument("--export-conversation", type=str, help="export a saved convo.jsonl without starting the app")
    parser.add_argument("--export-output", type=str, help="new Markdown file for --export-conversation")

    args, remaining = parser.parse_known_args()

    if args.cwd:
        os.chdir(args.cwd)

    if args.export_conversation or args.export_output:
        if not args.export_conversation or not args.export_output:
            parser.error("--export-conversation and --export-output must be supplied together")
        from pathlib import Path

        from litetui.conversation_export import export

        try:
            export(Path(args.export_conversation), Path(args.export_output))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        return

    from litetui.paths import data_root
    from litetui.shared_state import check_data_version
    try:
        check_data_version(data_root())
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    from litetui.app import LiteTUI, wants_ansi_fallback

    app_kwargs: dict = {}
    if not args.rpc:
        app_kwargs["ansi_color"] = wants_ansi_fallback()

    app = LiteTUI(
        rpc=args.rpc,
        first_prompt=args.prompt,
        system_prompt=args.system_prompt,
        initial_model=args.model,
        initial_backend=args.backend,
        initial_thinking=args.reasoning_effort or args.thinking_level,
        tool_profile=args.tool_profile or ("autonomous" if args.rpc else None),
        plan_mode=args.mode == "plan",
        convo_id=args.convo,
        **app_kwargs,
    )

    from litetui.image_viewer import init_image_backend

    # Pre-run, before the fd-1 redirect below: the image backend bind +
    # cell-size seed must happen while we still own the tty (their terminal
    # replies would otherwise be read by Textual and leak into the Input).
    init_image_backend()

    if args.rpc:
        # Import rpc first so it captures the real fd 1 via os.dup(1), then
        # redirect the original fd 1 to stderr — Textual's escape codes go
        # there, and only rpc_emit's duped fd carries JSONL.
        import litetui.rpc  # noqa: F401 — side effect: captures fd 1
        os.dup2(sys.stderr.fileno(), 1)
        app.run(headless=True)
    else:
        app.run()


if __name__ == "__main__":
    main()
