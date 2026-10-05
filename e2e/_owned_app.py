"""Owned storage fixtures for explicit live probes; importing starts nothing."""
from contextlib import contextmanager
import os
from pathlib import Path

from litetui import paths, settings
from litetui.agent_launch_context import ordinary


@contextmanager
def owned_session(root, cfg, *, backend=None, model=None, thinking_level=None):
    """Keep the probe's execution intent and release even before run_test starts."""
    root = Path(root).resolve()
    previous_env = os.environ.get('LITETUI_DATA_ROOT')
    previous_convos = paths.CONVO_DIR
    previous_load, previous_path = settings.load, settings.settings_path
    session = None
    try:
        os.environ['LITETUI_DATA_ROOT'] = str(root)
        paths.CONVO_DIR = root / '.convos'
        settings.load = lambda *a, **k: cfg
        settings.settings_path = lambda root=None: (Path(root) if root is not None else paths.data_root()) / 'settings.json'
        session = ordinary(root, cfg, backend=backend, model=model, thinking_level=thinking_level)
        yield session
    finally:
        try:
            if session is not None:
                app = getattr(session, '_e2e_app', None)
                try:
                    if app is not None:
                        app.store.release()
                finally:
                    session.release()
        finally:
            settings.load, settings.settings_path = previous_load, previous_path
            paths.CONVO_DIR = previous_convos
            if previous_env is None:
                os.environ.pop('LITETUI_DATA_ROOT', None)
            else:
                os.environ['LITETUI_DATA_ROOT'] = previous_env


def configure_owned_app(app):
    """Track cleanup and replace fleet transport only, never local RPC/sidecar.

    These probes explicitly opt out of the live fleet with NO_HARNESS. A local
    positive receipt keeps actual startup/session validation enabled, without
    claiming a real registry registration. Provider and local bridge stay real.
    """
    session = app._agent_session
    session._e2e_app = app
    if os.environ.get('LITETUI_NO_HARNESS') != '1':
        raise ValueError('Owned e2e fixture requires explicit NO_HARNESS')
    def registered():
        app.seat.registered = True
        app.seat.error = None
        return True
    app.seat.register = registered
    app.seat.poll = lambda: []
    app.seat.refresh_name = lambda: None
    app.seat.heartbeat = lambda: None
