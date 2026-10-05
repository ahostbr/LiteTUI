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
from pathlib import Path
import os
import sys


def main() -> None:
    # Fast path for probes: --version / -V print and exit before app.py (and its
    # Textual import) is ever loaded.
    if any(a in ("--version", "-V") for a in sys.argv[1:]):
        from litetui.version import __version__

        print(f"litetui {__version__}")
        return

    if sys.argv[1:2] == ["--capabilities"]:
        from litetui.model_capabilities import main as capabilities_main
        capabilities_main(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="litetui",
        description="LiteTUI — a terminal interface for local LLMs",
        add_help=True,
    )
    parser.add_argument("--version", "-V", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--rpc", action="store_true", help="headless JSONL-over-stdio mode")
    from litetui.llm_backend import BACKEND_NAMES
    from litetui.tool_policy import PROFILE_NAMES
    parser.add_argument("--backend", choices=BACKEND_NAMES, default=None, help='invocation-only backend selection')
    from litetui.launch_options import add_arguments, from_args
    add_arguments(parser)
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument('--reasoning-effort', default=None)
    thinking.add_argument('--thinking-level', default=None)
    parser.add_argument("--model", type=str, default=None, help="model slug to select on start")
    parser.add_argument("--prompt", type=str, default=None, help="first turn to submit once ready")
    parser.add_argument("--system-prompt", type=str, default=None, help="prepend a system message")
    parser.add_argument(
        "--system-prompt-file",
        type=str,
        default=None,
        help="read the system message from a UTF-8 file",
    )
    parser.add_argument("--cognitive-file", type=str, default=None,
                        help="canonical cognitive architecture file selected by the LiteSuite bridge")
    parser.add_argument("--cwd", type=str, default=None, help="change working directory before start")
    parser.add_argument("--mcp-servers", default="all",
                        help="seat-only project MCP selection: all, none, or comma-separated names")
    parser.add_argument("--mcp-start", choices=("lazy", "eager"), default="lazy",
                        help="host stdio MCP startup (native Codex owns its own startup)")
    parser.add_argument(
        "--tool-profile",
        type=str,
        default=None,
        choices=list(PROFILE_NAMES),
        help="tool policy profile (default: autonomous when --rpc, else settings)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default=None,
        choices=["normal", "plan"],
        help="plan: load ls-plan-w-quizmaster and ask through ask_user_question (T558)",
    )
    agent_selection = parser.add_mutually_exclusive_group()
    agent_selection.add_argument("--agent", type=str, default=None, help="select authoritative named agent folder")
    agent_selection.add_argument("--create-agent", type=str, help="create a new owned named agent home")
    parser.add_argument("--agent-id", type=str, help="fresh identity; valid only with --create-agent")
    parser.add_argument("--convo", type=str, default=None, help="resume a conversation within the selected agent")
    parser.add_argument("--export-conversation", type=str, help="export a saved convo.jsonl without starting the app")
    parser.add_argument("--export-output", type=str, help="new Markdown file for --export-conversation")

    args = parser.parse_args()
    if args.create_agent:
        if args.convo or not all((args.agent_id, args.backend, args.model,
                                 args.reasoning_effort or args.thinking_level)):
            parser.error("--create-agent requires --agent-id, --backend, --model and thinking level; no --convo")
    elif args.agent_id:
        parser.error("--agent-id is valid only with --create-agent")
    try:
        launch_options = from_args(args)
        from litetui.mcp_seat import server_selection
        mcp_servers = server_selection(args.mcp_servers)
    except ValueError as exc:
        parser.error(str(exc))

    if args.cwd:
        os.chdir(args.cwd)
    from litetui.settings import load
    try:
        settings = load()
        launch_options.overrides(settings, args.backend or settings.backend, args.model)
    except ValueError as exc:
        parser.error(str(exc))

    if args.export_conversation or args.export_output:
        if not args.export_conversation or not args.export_output:
            parser.error("--export-conversation and --export-output must be supplied together")
        from litetui.conversation_export import export

        try:
            export(Path(args.export_conversation), Path(args.export_output))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        return

    from litetui.paths import data_root
    agent_session = None
    app = None
    try:
        if args.convo and not args.agent:
            from litetui.agent_store import AgentStore, StoreError
            try:
                store = AgentStore(data_root())
                directory = store.locate_conversation(args.convo)
                args.agent = store.find_agent(name=directory.parent.parent.name).name
            except (StoreError, OSError) as exc:
                parser.error(
                    f'Owned conversation lookup refused: {exc}; pass --agent <Name> only for an existing owned child. '
                    'Legacy archives are read-only. Export without starting the app: '
                    'litetui --export-conversation <archive/convo.jsonl> --export-output <new.md>. '
                    'Owned continuation requires operator-approved copy/migration with a hash-pinned manifest/merge receipt '
                    'and quiet-seat verification before activation; --agent does not migrate an archive. No automatic copy.')
        spawned_identity = None
        from litetui import harness as harness_mod
        if os.environ.get(harness_mod.SPAWN_IDENTITY_MARKER) == '1':
            spawned_identity = harness_mod.spawned_seat_identity()
        if not args.agent and not args.create_agent:
            from litetui.agent_launch_context import ordinary
            try:
                from litetui.shared_state import check_data_version
                check_data_version(data_root())
                agent_session = ordinary(data_root(), settings, spawned_identity=spawned_identity,
                                         backend=args.backend, model=args.model,
                                         thinking_level=args.reasoning_effort or args.thinking_level,
                                         notice=lambda text: print(text, file=sys.stderr))
            except (ValueError, OSError) as exc:
                parser.error(str(exc))
        if args.agent or args.create_agent:
            from litetui.agent_launch_context import acquire, create
            try:
                if args.create_agent:
                    # Version refusal precedes reservation; all writes below belong
                    # to the child's newly reserved home, never the caller's seat.
                    from litetui.shared_state import check_data_version
                    check_data_version(data_root())
                    agent_session = create(data_root(), args.create_agent, agent_id=args.agent_id,
                                           backend=args.backend, model=args.model,
                                           thinking_level=args.reasoning_effort or args.thinking_level)
                else:
                    agent_session = acquire(data_root(), args.agent, conversation_id=args.convo,
                                            backend=args.backend, model=args.model,
                                            thinking_level=args.reasoning_effort or args.thinking_level)
                authority = agent_session.authority
                args.backend, args.model = authority.backend, authority.model
                args.thinking_level, args.reasoning_effort = authority.thinking_level, None
            except (ValueError, OSError) as exc:
                parser.error(str(exc))
        from litetui.shared_state import check_data_version
        try:
            check_data_version(data_root())
        except (ValueError, OSError) as exc:
            parser.error(str(exc))

        from litetui.app import LiteTUI, wants_ansi_fallback

        app_kwargs: dict = {}
        if not args.rpc:
            app_kwargs["ansi_color"] = wants_ansi_fallback()

        if args.system_prompt and args.system_prompt_file:
            parser.error("--system-prompt and --system-prompt-file are mutually exclusive")
        system_prompt = args.system_prompt
        if args.system_prompt_file:
            try:
                system_prompt = Path(args.system_prompt_file).read_text(encoding="utf-8")
            except OSError as exc:
                parser.error(f"cannot read --system-prompt-file: {exc}")
        if args.cognitive_file:
            try:
                architecture = Path(args.cognitive_file).read_text(encoding="utf-8")
            except OSError as exc:
                parser.error(f"cannot read --cognitive-file: {exc}")
            system_prompt = (system_prompt + "\n\n" if system_prompt else "") + architecture

        app = LiteTUI(
            rpc=args.rpc,
            first_prompt=args.prompt,
            system_prompt=system_prompt,
            initial_model=args.model,
            initial_backend=args.backend,
            initial_thinking=args.reasoning_effort or args.thinking_level,
            launch_options=launch_options,
            mcp_servers=mcp_servers,
            mcp_lazy=args.mcp_start == "lazy",
            tool_profile=args.tool_profile or ("autonomous" if args.rpc else None),
            plan_mode=args.mode == "plan",
            convo_id=args.convo,
            agent_session=agent_session,
            spawn_identity=spawned_identity,
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
    finally:
        if agent_session is not None:
            try:
                store = getattr(app, 'store', None)
                if store is not None:
                    store.release()
            finally:
                agent_session.release()


if __name__ == "__main__":
    main()
