"""System-first interpreters for isolated optional-feature workers.

Never inject another interpreter's site-packages into this process.
"""
import functools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def candidates():
    """Base Python, PATH Python, then this application's interpreter."""
    found = []
    current = os.path.normcase(os.path.abspath(sys.executable))
    for exe in (getattr(sys, '_base_executable', None), shutil.which('python3'),
                shutil.which('python'), sys.executable):
        if not exe or not Path(exe).is_file():
            continue
        exe = os.path.abspath(exe)
        if os.path.normcase(exe) not in [os.path.normcase(p) for p in found]:
            found.append(exe)
    return [p for p in found if os.path.normcase(p) != current] + [sys.executable]


@functools.lru_cache(maxsize=64)
def supports(executable, modules):
    # Import rather than find_spec: broken transitive/binary dependencies count
    # as unavailable too. Probe is bounded and cannot write bytecode.
    code = 'import importlib,json,sys; [importlib.import_module(m) for m in json.loads(sys.argv[1])]'
    try:
        from litetui import ttyguard
        result = ttyguard.run([executable, '-E', '-B', '-c', code, json.dumps(modules)], timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve(*modules):
    return next((exe for exe in candidates() if supports(exe, tuple(modules))), None)


def install_target():
    return candidates()[0]


def invalidate():
    supports.cache_clear()

