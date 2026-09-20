"""Thread-safe tool dispatch onto the App's owned asyncio loop."""
import json
from litetui.agent_launcher import LaunchBlocked


def make_runner(app, *, launch):
    """launch(args) is an async trusted service configured by the App."""
    def runner(args):
        if not isinstance(args, dict):
            raise LaunchBlocked('Agent operation must be an object')
        action = args.get('action', 'spawn')
        if action not in ('spawn', 'status', 'cancel'):
            raise LaunchBlocked('Unknown agent operation')
        if action != 'spawn' and set(args) - {'action', 'operation_id'}:
            raise LaunchBlocked('Unexpected status/cancel fields')
        request = {key: value for key, value in args.items() if key != 'action'}
        # Textual awaits async callbacks on its own loop. Only cancellation
        # waits for cleanup; spawn returns an operation id immediately.
        async def dispatch():
            from litetui.agent_operations import AgentOperations
            manager = getattr(app, '_agent_operations', None)
            if manager is None:
                if action != 'spawn':
                    raise LaunchBlocked('No child operations in this parent process')
                manager = app._agent_operations = AgentOperations()
            if action == 'spawn':
                captured = dict(request)
                ident = manager.start(lambda: launch(captured))
                return {'operation_id': ident, 'status': 'accepted'}
            ident = request.get('operation_id')
            if not isinstance(ident, str) or not ident:
                raise LaunchBlocked('Explicit operation_id required')
            if action == 'cancel':
                return await manager.cancel(ident)
            return manager.status(ident)
        return json.dumps(app.call_from_thread(dispatch), ensure_ascii=False)
    return runner
