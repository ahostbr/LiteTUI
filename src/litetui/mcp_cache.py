"""Disposable stdio tool inventory, keyed by launch inputs and local file stamps."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


def cache_key(root: Path, cfg: dict) -> str:
    command = str(cfg.get("command", ""))
    files = []
    cwd = Path(cfg.get("cwd") or root).resolve()
    for value in [shutil.which(command) or command, *cfg.get("args", [])]:
        path = Path(str(value))
        if not path.is_absolute():
            path = cwd / path
        try:
            stat = path.stat()
            if path.is_file():
                files.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except OSError:
            pass
    # env/cwd and other launch settings matter too; only the hash is stored.
    payload = json.dumps([str(root.resolve()), cfg, files], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class ToolCache:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, root: Path, name: str) -> Path:
        identity = hashlib.sha256(f"{root.resolve()}\0{name}".encode()).hexdigest()
        return self.directory / f"{identity}.json"

    def read(self, root: Path, name: str, cfg: dict) -> list[dict] | None:
        try:
            doc = json.loads(self._path(root, name).read_text(encoding="utf-8"))
            tools = doc["tools"]
            if (doc["key"] == cache_key(root, cfg) and isinstance(tools, list)
                    and all(isinstance(t, dict) and isinstance(t.get("name"), str)
                            and isinstance(t.get("inputSchema", {}), dict) for t in tools)):
                return tools
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def write(self, root: Path, name: str, cfg: dict, tools: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(root, name)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"key": cache_key(root, cfg), "tools": tools}, stream)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
