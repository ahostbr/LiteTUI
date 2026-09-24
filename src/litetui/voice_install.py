"""Install optional Edge speech dependencies using system-first Python discovery."""
import importlib
import shutil
import subprocess
import sys


def edge_available():
    from litetui.voice_backend import available_engines
    from litetui.optional_python import invalidate
    invalidate()
    importlib.invalidate_caches()
    return 'edge' in available_engines()


def install_command():
    uv = shutil.which('uv')
    from litetui.optional_python import install_target
    target = install_target()
    prefix = [uv, 'pip', 'install', '--python', target] if uv else [target, '-m', 'pip', 'install']
    return prefix + ['edge-tts', 'playsound==1.2.2']


def install_edge():
    if edge_available():
        return 'Edge support already installed — pick edge and Test.'
    try:
        from litetui import ttyguard
        done = ttyguard.run(install_command(), timeout=240)
        if done.returncode:
            raise subprocess.CalledProcessError(done.returncode, done.args, done.stdout, done.stderr)
        if not edge_available():
            return 'Edge install finished but dependencies are not importable in this Python environment.'
        return 'Edge support installed — pick edge and Test.'
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.stdout or str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        return 'Edge install failed: ' + ' '.join(detail.split())[-1200:]
    except Exception as exc:
        return f'Edge install failed: {type(exc).__name__}: {exc}'
