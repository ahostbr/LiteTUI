"""Read-only conversation snapshots; explicit refresh launches a detached job.

The standalone CLI owns root validation, metadata and refresh supervision. No
lookup builds an index or enumerates the fleet. Refresh is a write/process action,
not read-only authority disguised by the tool's usual search behavior.
"""
import subprocess
import sys

from litetui import paths, tool_schemas, ttyguard
from litetui.plugins import PluginManifest
from litetui.tool_policy import EXTERNAL_WRITE, PROCESS_EXECUTION, READ_ONLY, ToolPolicy

CLI = paths.ROOT / 'tools' / 'convo_search.py'
TIMEOUT = 120  # lookup only; detached refresh child has its own 3600s budget
MAX_LINES = 2000
MAX_CHARS = 50_000
UNKNOWN = 'Last successful refresh UTC: UNKNOWN'


def _truncate(out: str) -> str:
    lines = out.splitlines()
    cut = ''
    if len(lines) > MAX_LINES:
        out = '\n'.join(lines[:MAX_LINES])
        cut = f'\n…(truncated at {MAX_LINES} lines)'
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS]
        cut = f'\n…(truncated at {MAX_CHARS // 1000}KB)'
    return out + cut


def _error(message: str) -> str:
    return UNKNOWN + '\n[error] convo_search: ' + message


def _run(args: dict) -> str:
    if not CLI.exists():
        return _error(f'CLI missing at {CLI}')
    cmd = [sys.executable, str(CLI), '--root', str(paths.data_root())]
    if args.get('refresh'):
        if any(args.get(key) for key in ('query', 'stats', 'show', 'raw', 'agent', 'since', 'limit', 'messages')):
            return _error('refresh=true is a separate action; do not combine it with lookup arguments')
        cmd.append('--refresh')
    elif args.get('stats'):
        cmd.append('--stats')
    elif args.get('show'):
        cmd += ['--show', str(args['show'])]
    elif args.get('raw'):
        pattern = str(args.get('query') or '')
        if not pattern:
            return _error('raw=<convo> requires query=<regex>')
        cmd += ['--raw=' + str(args['raw']), '--', pattern]
    else:
        query = str(args.get('query') or '').strip()
        if not query:
            return _error('a query is required (or show=/raw=/stats=true/refresh=true)')
        for key in ('agent', 'since', 'limit'):
            if args.get(key):
                cmd.append('--' + key + '=' + str(args[key]))
        if args.get('messages'):
            cmd.append('--messages')
        # User text must never become an administrative CLI action.
        cmd += ['--', query]
    try:
        # The CLI's --refresh returns after spawning its hidden supervisor,
        # never after catalog/index work. The TUI console remains isolated.
        proc = ttyguard.run(cmd, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return _error(f'timed out after {TIMEOUT}s; refresh launch outcome may be unknown if requested')
    except OSError as exc:
        return _error(str(exc))
    out = proc.stdout or ''
    if not out.startswith('Last successful refresh UTC:'):
        out = UNKNOWN + '\n' + out
    if proc.returncode != 0:
        out += f'\n[error] convo_search exit {proc.returncode}: ' + (proc.stderr or '')
    return _truncate(out.strip())


def _capabilities(args, workspace):
    return (EXTERNAL_WRITE, PROCESS_EXECUTION) if args.get('refresh') else ()


POLICY = ToolPolicy(frozenset({READ_ONLY}), 'Read index snapshots; explicit refresh writes the data-root index and launches a process.', classify_args=_capabilities)


def _register(ctx) -> None:
    ctx.tool(tool_schemas.load('convo_search'), _run, policy=POLICY)


PLUGIN = PluginManifest(id='convo_search', register=_register)
