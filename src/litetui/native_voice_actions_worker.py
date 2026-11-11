"""Prepare and execute exactly reviewed Voice actions inside an owned process Job."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from litetui import optional_python, voice_backend
from litetui.file_observations import stable_read
from litetui.ttyguard import NO_WINDOW

MAX_BYTES = 65536
PHRASE = "This is the LiteTUI voice test."
PACKAGES = {
    "fallback": [],
    "edge": ["edge-tts", "playsound==1.2.2"],
    "stt": ["faster-whisper"],
}
MODULES = {
    "edge": ["edge_tts", "playsound"],
    "pyttsx3": ["pyttsx3"],
    "stt": ["faster_whisper"],
}
PLAN_KEYS = {
    "version",
    "action",
    "interpreter",
    "launcher",
    "install",
    "argv",
    "packages",
    "prefix",
    "cache",
    "model",
    "engine",
    "voice",
    "timeout",
    "phrase",
    "fingerprints",
    "modules",
    "network",
}


def load(path):
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Voice helper result exceeds its limit")
    return json.loads(raw)


def write(path, value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf8")
    if len(raw) > MAX_BYTES:
        raise ValueError("Voice helper result exceeds its limit")
    path.write_bytes(raw)


def fingerprint(path):
    path = Path(path)
    before = path.stat()
    if not path.is_file() or before.st_size > 128 * 1024 * 1024:
        raise ValueError("Voice executable exceeds its review bound")
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        value = hashlib.file_digest(source, "sha256").hexdigest()
        finished = os.fstat(source.fileno())

    def stamp(info, *, birthtime=False):
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_birthtime_ns if birthtime else info.st_ctime_ns,
        )

    if not stable_read(before, opened, finished, path.stat(), stamp=stamp):
        raise ValueError("Voice executable changed during review")
    return value


def bounded_run(argv, *, timeout, environment=None):
    """Retain and join one draining reader; cache at most 64KiB, never publish logs."""
    cached = bytearray()
    env = dict(os.environ) if environment is None else environment
    with subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW,
        env=env,
    ) as child:

        def read():
            while chunk := child.stdout.read(4096):
                excess = len(cached) + len(chunk) - MAX_BYTES
                if excess > 0:
                    del cached[:excess]
                cached.extend(chunk)

        reader = threading.Thread(target=read, name="voice-action-output", daemon=False)
        reader.start()
        try:
            result = child.wait(timeout=timeout)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            reader.join(timeout=2)
            if reader.is_alive():
                # A non-daemon reader retains this worker until the owning Job
                # deadline cancels all inherited pipe owners together.
                raise RuntimeError("Voice action output reader has not retired")
    return result


_PROBE = r"""import importlib,json,os,sys
from pathlib import Path
modules=json.loads(sys.argv[1])
for module in modules:importlib.import_module(module)
base=os.environ.get('HF_HOME') or str(Path(os.environ.get('XDG_CACHE_HOME') or Path.home()/'.cache')/'huggingface')
cache=os.environ.get('HF_HUB_CACHE') or os.environ.get('HUGGINGFACE_HUB_CACHE') or str(Path(base)/'hub')
if modules and 'faster_whisper' in modules:
    from huggingface_hub.constants import HF_HUB_CACHE
    cache=HF_HUB_CACHE
cache=Path(cache).expanduser()
Path(sys.argv[2]).write_text(json.dumps({'prefix':sys.prefix,'cache':str(cache.resolve()) if cache.is_absolute() else str(cache)}),encoding='utf8')
"""


def probe(executable, modules, directory):
    result = directory / "probe.json"
    result.unlink(missing_ok=True)
    try:
        if bounded_run(
            [executable, "-E", "-B", "-c", _PROBE, json.dumps(modules), str(result)],
            timeout=15,
        ):
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = load(result)
    if set(data) != {"prefix", "cache"}:
        raise ValueError("Invalid optional Python metadata")
    return data


def _path(value):
    return (
        type(value) is str
        and 0 < len(value.encode("utf8")) <= 4096
        and all(c.isprintable() for c in value)
        and Path(value).is_absolute()
    )


def validate(plan, action, preferences):
    if (
        type(plan) is not dict
        or set(plan) != PLAN_KEYS
        or plan["version"] != 1
        or plan["action"] != action
    ):
        raise ValueError("Voice plan has changed")
    modules = (
        MODULES[preferences["engine"]] if action == "fallback" else MODULES[action]
    )
    if (
        plan["modules"] != modules
        or plan["packages"] != PACKAGES[action]
        or type(plan["install"]) is not bool
    ):
        raise ValueError("Voice packages differ from the requested action")
    if not all(_path(plan[key]) for key in ("interpreter", "launcher", "prefix")):
        raise ValueError("Invalid reviewed interpreter")
    expected = {
        "model": preferences["model"] if action == "stt" else None,
        "engine": preferences["engine"] if action == "fallback" else None,
        "voice": preferences["voice"] if action == "fallback" else None,
        "timeout": preferences["timeout"]
        if action == "fallback"
        else 240
        if action == "edge"
        else 600,
        "phrase": PHRASE if action == "fallback" else None,
        "network": action != "fallback" or preferences["engine"] == "edge",
    }
    if (
        any(plan[key] != value for key, value in expected.items())
        or type(plan["network"]) is not bool
    ):
        raise ValueError("Voice action differs from reviewed preferences")
    if (action == "stt" and not _path(plan["cache"])) or (
        action != "stt" and plan["cache"] is not None
    ):
        raise ValueError("Invalid model cache destination")
    if (
        action == "fallback"
        and plan["install"]
        or action != "fallback"
        and plan["launcher"] != plan["interpreter"]
    ):
        raise ValueError("Voice launcher differs from its action")
    argv = plan["argv"]
    if (
        type(argv) is not list
        or len(argv) > 16
        or any(
            type(x) is not str or len(x) > 4096 or not all(c.isprintable() for c in x)
            for x in argv
        )
    ):
        raise ValueError("Invalid installer command")
    executables = {plan["interpreter"], plan["launcher"]}
    if plan["install"]:
        if not argv or not _path(argv[0]):
            raise ValueError("Missing installer")
        pip = [
            plan["interpreter"],
            "-E",
            "-B",
            "-m",
            "pip",
            "install",
            "--prefix",
            plan["prefix"],
            *PACKAGES[action],
        ]
        uv = [
            argv[0],
            "pip",
            "install",
            "--python",
            plan["interpreter"],
            "--prefix",
            plan["prefix"],
            "--no-python-downloads",
            *PACKAGES[action],
        ]
        if argv != pip and (
            Path(argv[0]).name.lower() not in ("uv", "uv.exe") or argv != uv
        ):
            raise ValueError("Installer target or arguments changed")
        executables.add(argv[0])
    elif argv:
        raise ValueError("Ready dependencies must not install implicitly")
    fingerprints = plan["fingerprints"]
    if (
        type(fingerprints) is not dict
        or set(fingerprints) != executables
        or any(
            type(v) is not str
            or len(v) != 64
            or any(c not in "0123456789abcdef" for c in v)
            for v in fingerprints.values()
        )
    ):
        raise ValueError("Executable review is incomplete")
    return plan


def prepare(action, preferences, directory):
    modules = (
        MODULES[preferences["engine"]] if action == "fallback" else MODULES[action]
    )
    selected = None
    # Same system-first candidates and real import readiness as optional_python,
    # with every probe inside the owned helper and with bounded discarded output.
    for executable in optional_python.candidates():
        if data := probe(executable, modules, directory):
            selected = executable, data
            break
    install = selected is None
    if install:
        if action == "fallback":
            raise ValueError("Fallback engine is not installed")
        executable = optional_python.install_target()
        data = probe(executable, [], directory)
        if data is None:
            raise ValueError("Install target is unavailable")
    else:
        executable, data = selected
    launcher = (
        voice_backend._speaker(executable)[0] if action == "fallback" else executable
    )
    argv = []
    if install:
        uv = shutil.which("uv")
        argv = (
            [
                uv,
                "pip",
                "install",
                "--python",
                executable,
                "--prefix",
                data["prefix"],
                "--no-python-downloads",
            ]
            if uv
            else [
                executable,
                "-E",
                "-B",
                "-m",
                "pip",
                "install",
                "--prefix",
                data["prefix"],
            ]
        ) + PACKAGES[action]
    plan = {
        "version": 1,
        "action": action,
        "interpreter": executable,
        "launcher": launcher,
        "install": install,
        "argv": argv,
        "packages": PACKAGES[action],
        "prefix": data["prefix"],
        "cache": data["cache"] if action == "stt" else None,
        "model": preferences["model"] if action == "stt" else None,
        "engine": preferences["engine"] if action == "fallback" else None,
        "voice": preferences["voice"] if action == "fallback" else None,
        "timeout": preferences["timeout"]
        if action == "fallback"
        else 240
        if action == "edge"
        else 600,
        "phrase": PHRASE if action == "fallback" else None,
        "modules": modules,
        "network": action != "fallback" or preferences["engine"] == "edge",
    }
    plan["fingerprints"] = {
        exe: fingerprint(exe)
        for exe in {executable, launcher, *([argv[0]] if argv else [])}
    }
    return validate(plan, action, preferences)


def check_executables(plan):
    if any(
        fingerprint(path) != expected for path, expected in plan["fingerprints"].items()
    ):
        raise ValueError("Reviewed executable changed; prepare a fresh review")


def checked_run(plan, argv, *, timeout, environment=None):
    check_executables(plan)
    if argv[0] not in plan["fingerprints"]:
        raise ValueError("Unreviewed executable")
    if bounded_run(argv, timeout=timeout, environment=environment):
        raise ValueError("Voice action child failed")


def execute(plan, preferences, directory):
    validate(plan, plan["action"], preferences)
    check_executables(plan)
    context = probe(
        plan["interpreter"], [] if plan["install"] else plan["modules"], directory
    )
    if (
        context is None
        or context["prefix"] != plan["prefix"]
        or (plan["action"] == "stt" and context["cache"] != plan["cache"])
    ):
        raise ValueError(
            "Reviewed interpreter or cache context changed before execution"
        )
    if plan["install"]:
        environment = dict(os.environ)
        # An explicit prefix is part of review. Target-changing environment
        # overrides must never silently redirect package writes elsewhere.
        for key in (
            "PIP_TARGET",
            "PIP_PREFIX",
            "PIP_USER",
            "PYTHONUSERBASE",
            "UV_TARGET",
            "UV_PREFIX",
            "UV_PYTHON",
        ):
            environment.pop(key, None)
        checked_run(
            plan, plan["argv"], timeout=plan["timeout"], environment=environment
        )
    if plan["action"] == "fallback":
        template = (
            voice_backend._EDGE_CHILD
            if plan["engine"] == "edge"
            else voice_backend._SAPI_CHILD
        )
        voice = plan["voice"] or (
            voice_backend.DEFAULT_EDGE_VOICE if plan["engine"] == "edge" else ""
        )
        script = directory / "speech.py"
        script.write_text(
            "import tempfile\ntempfile.tempdir="
            + repr(str(directory))
            + "\n"
            + template.format(text=PHRASE, voice=voice),
            encoding="utf8",
        )
        checked_run(
            plan, [plan["launcher"], "-E", "-B", str(script)], timeout=plan["timeout"]
        )
    elif plan["action"] == "stt":
        code = (
            "from faster_whisper import WhisperModel;WhisperModel("
            + repr(plan["model"])
            + ",device='cpu',compute_type='int8',download_root="
            + repr(plan["cache"])
            + ")"
        )
        checked_run(plan, [plan["interpreter"], "-E", "-B", "-c", code], timeout=600)
    # Fresh independent interpreter imports after execution; never trust the
    # install process exit status or a cached resolver entry as readiness.
    check_executables(plan)
    data = probe(plan["interpreter"], plan["modules"], directory)
    if data is None or data["prefix"] != plan["prefix"]:
        raise ValueError("Independent dependency verification failed")
    if plan["action"] == "stt":
        if data["cache"] != plan["cache"]:
            raise ValueError("Model cache destination changed")
        code = (
            "from faster_whisper import WhisperModel;WhisperModel("
            + repr(plan["model"])
            + ",device='cpu',compute_type='int8',download_root="
            + repr(plan["cache"])
            + ",local_files_only=True)"
        )
        checked_run(
            plan,
            [plan["interpreter"], "-E", "-B", "-c", code],
            timeout=600,
            environment={**os.environ, "HF_HUB_OFFLINE": "1"},
        )
    return {
        "kind": {
            "fallback": "fallback_finished",
            "edge": "edge_ready",
            "stt": "stt_cache_ready",
        }[plan["action"]],
        "interpreter": plan["interpreter"],
        "cache": plan["cache"],
        "model": plan["model"],
        "dependencies_verified": True,
    }


def run(directory):
    parameters = load(directory / "parameters.json")
    action, preferences = parameters["action"], parameters["preferences"]
    if parameters["phase"] == "review":
        result = {"plan": prepare(action, preferences, directory)}
    elif parameters["phase"] == "apply":
        result = {"result": execute(parameters["plan"], preferences, directory)}
    else:
        raise ValueError("Unknown Voice action phase")
    write(directory / "result.json", result)


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
