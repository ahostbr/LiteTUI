"""Use the connected provider's measured model catalogue, never invented levels."""
from litetui.agent_launcher import LaunchBlocked


def hosted_levels(app, request):
    backend = getattr(app, 'backend', None)
    if request.get('backend') != 'codex' or getattr(backend, 'name', None) != 'codex':
        raise LaunchBlocked('Hosted launch currently requires a connected Codex parent catalogue')
    model = request.get('model')
    if not isinstance(model, str) or model not in getattr(backend, 'models', {}):
        raise LaunchBlocked('Requested model is absent from the connected provider catalogue; refresh models')
    reader = getattr(backend, 'reasoning_levels', None)
    if reader is None:
        raise LaunchBlocked('Provider reasoning capabilities unavailable')
    levels = reader(model)
    if not isinstance(levels, (list, tuple)) or any(not isinstance(level, str) for level in levels):
        raise LaunchBlocked('Provider reasoning capabilities malformed')
    return list(levels)
