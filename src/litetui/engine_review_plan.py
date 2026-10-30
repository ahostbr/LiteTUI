"""Immutable descriptions consumed by guarded native engine starts.

Provenance here is bounded header/config bytes plus filesystem identity. It is
NOT a full artifact fingerprint or calibrated resource admission evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from litetui import ninfer_engine, strata_engine
from litetui import settings as settings_mod
from litetui.llm_backend import BackendError

MAX_FILE_METADATA = 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _identity(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_evidence(path, *, complete=False):
    path = Path(path).absolute()
    if len(str(path).encode()) > 4096:
        raise ValueError("Engine source path exceeds its review budget")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or complete and before.st_size > MAX_FILE_METADATA:
        raise ValueError("Engine configuration must be a bounded regular file")
    target = str(path.resolve(strict=True))
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read(MAX_FILE_METADATA + 1 if complete else 65536)
        after = os.fstat(stream.fileno())
    path_after = path.stat()
    if (_identity(before) != _identity(opened) or _identity(after) != _identity(before)
            or _identity(path_after) != _identity(before) or path_after.st_mode != before.st_mode or str(path.resolve(strict=True)) != target):
        raise ValueError("Engine source changed while reading; refresh the review")
    if complete and len(data) > MAX_FILE_METADATA:
        raise ValueError("Engine configuration exceeds its review budget")
    return (str(path), target, _identity(before), hashlib.sha256(data).hexdigest(), complete, before.st_mode)


@dataclass(frozen=True)
class ReviewedLaunch:
    backend: str
    model: str
    settings_json: bytes
    summary_json: bytes
    files: tuple
    payload_json: bytes

    @property
    def summary(self):
        return json.loads(self.summary_json)

    def settings(self):
        return settings_mod.Settings(**json.loads(self.settings_json))

    def verify(self):
        for evidence in self.files:
            if file_evidence(evidence[0], complete=evidence[4]) != evidence:
                raise BackendError("Reviewed engine input changed; nothing further was authorized")

    def ninfer_inputs(self):
        self.verify()
        values = json.loads(self.payload_json)
        return Path(values["executable"]), Path(values["artifact"]), values["directory"]

    def strata_inspection(self):
        self.verify()
        values = json.loads(self.payload_json)
        return strata_engine.LaunchInspection(tuple(values["argv"]), Path(values["cwd"]), values["config"],
                                             values["host"], Path(values["derived_config"]) if values["derived_config"] else None)


def inspect(settings, backend, desired_model):
    """Describe only the configured choice; no discovery by RPC path or argv."""
    frozen = encoded(asdict(settings))
    selected = settings_mod.Settings(**json.loads(frozen))
    if backend == "ninfer":
        if str(selected.ninfer_host or "").strip():
            raise ValueError("Native NInfer start requires an empty explicit endpoint; reconnect-only endpoint authority is preserved")
        executable = ninfer_engine.ninfer_executable(selected).absolute()
        artifact = ninfer_engine.ninfer_artifact(selected)
        if artifact is None:
            raise ValueError("Choose an explicit NInfer artifact before reviewing an engine start")
        artifact = artifact.absolute()
        evidence = (file_evidence(executable), file_evidence(artifact))
        # Canonical directory decoding is reused after bounding its declared size.
        with artifact.open("rb") as stream:
            header = stream.read(ninfer_engine.NINFER_V3_HEADER_BYTES)
        if (len(header) >= 16 and header[:8] == ninfer_engine.NINFER_V3_MAGIC
                and struct.unpack_from("<Q", header, 8)[0] > MAX_FILE_METADATA):
            raise ValueError("NInfer artifact directory exceeds the native review budget")
        directory = ninfer_engine.read_artifact_directory(artifact, max_bytes=MAX_FILE_METADATA)
        model = ninfer_engine.artifact_model_id(directory, fallback=artifact.stem)
        selected.ninfer_executable, selected.ninfer_artifact = str(executable), str(artifact)
        args = ninfer_engine.build_ninfer_args(artifact, 0, model,
            max_context=selected.ninfer_max_context, max_concurrency=selected.ninfer_max_concurrency,
            components=ninfer_engine.artifact_components(directory), kv_dtype=selected.ninfer_kv_dtype,
            kv_capacity=selected.ninfer_kv_capacity, host_kv_mib=selected.ninfer_host_kv_mib)
        context = int(args[args.index("--max-context") + 1])
        concurrency = ninfer_engine.concurrency_in(args)
        summary = {"model": model, "executable": str(executable), "cwd": str(executable.parent),
                   "artifact": str(artifact), "config": None, "derived_config": None,
                   "context": context, "concurrency": concurrency,
                   "load_shape": {"arguments": args, "provenance": "header_stat_not_full_artifact_hash"},
                   "endpoint_policy": "loopback_ephemeral", "endpoint": None}
        payload = {"executable": str(executable), "artifact": str(artifact), "directory": directory}
    elif backend == "strata":
        config_path = strata_engine.strata_config(selected, max_bytes=MAX_FILE_METADATA)
        if config_path is None:
            raise ValueError("Choose an explicit Strata configuration before reviewing an engine start")
        config_path = config_path.absolute()
        selected.strata_config = str(config_path)
        selected.strata_root = str(strata_engine.strata_root(selected).absolute())
        config_evidence = file_evidence(config_path, complete=True)
        plan = strata_engine.inspect_launch(selected, max_config_bytes=MAX_FILE_METADATA)
        endpoint = urlsplit(plan.host)
        if (endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1")
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or endpoint.path not in ("", "/") or not endpoint.port or endpoint.port > 65535):
            raise ValueError("Native Strata start requires an explicit valid local loopback endpoint")
        model = str(plan.config.get("model_name") or "strata")
        paths = [Path(plan.argv[0]), Path(plan.argv[1])]
        engine_executable = Path(str(plan.config["exe"]))
        if not engine_executable.is_absolute():
            engine_executable = plan.cwd / engine_executable
        paths.append(engine_executable)
        native = strata_engine._arg_value([str(value) for value in plan.config["args"]], "--native")
        if native:
            native_path = Path(native)
            if not native_path.is_absolute():
                native_path = plan.cwd / native_path
            native = str(native_path.absolute())
            paths.append(native_path)
        # Resolve the same execution paths for canonical resource sizing and
        # the child. Relative paths otherwise make native_bytes use app cwd.
        config = dict(plan.config)
        config["exe"] = str(engine_executable.absolute())
        config["args"] = list(config["args"])
        if native:
            config["args"][config["args"].index("--native") + 1] = native
        # Every native launch consumes captured configuration bytes, including
        # when no context override was requested; the child never rereads source.
        derived = strata_engine.log_path().with_name("review-" + digest(config) + ".json")
        argv = list(plan.argv)
        argv[argv.index("--config") + 1] = str(derived)
        plan = strata_engine.LaunchInspection(tuple(argv), plan.cwd, config, plan.host, derived)
        evidence = (config_evidence, *(file_evidence(path) for path in paths))
        summary = {"model": model, "executable": plan.argv[0], "cwd": str(plan.cwd),
                   "artifact": native, "config": str(config_path),
                   "derived_config": str(plan.derived_config) if plan.derived_config else None,
                   "context": strata_engine.config_context(plan.config), "concurrency": 1,
                   "load_shape": {"engine": str(engine_executable), "arguments": plan.config["args"],
                                  "provenance": "config_header_stat_not_full_artifact_hash"},
                   "endpoint_policy": "fixed_loopback", "endpoint": plan.host}
        payload = {"argv": plan.argv, "cwd": str(plan.cwd), "config": plan.config, "host": plan.host,
                   "derived_config": str(plan.derived_config) if plan.derived_config else None}
    else:
        raise ValueError("Only NInfer and Strata have native engine controls")
    if model != desired_model:
        raise ValueError("Configured engine model disagrees with the explicit owned model; no fallback or identity change is permitted")
    if len(encoded(summary["load_shape"])) > 16384:
        raise ValueError("Engine load shape exceeds its review budget")
    selected_json = encoded(asdict(selected))
    summary["selection_id"] = digest([backend, model, selected_json.decode(), evidence, payload])
    result = ReviewedLaunch(backend, model, selected_json, encoded(summary), tuple(evidence), encoded(payload))
    result.verify()
    return result
