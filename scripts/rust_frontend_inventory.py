"""P0 source discovery from a committed revision; never import the application.

Static records are leads, not runtime schemas or parity results. Generated evidence
is disposable; the optional parity seed is exclusively created, never overwritten.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

BASELINE = "9ef53373fc222805fa1970a16aa1d6d8887d833f"
REGISTRIES = (
    ("src/litetui/gui_rpc.py", "OPERATIONS", "gui_operation"),
    ("src/litetui/plugins/__init__.py", "PLUGIN_LOAD_ORDER", "plugin"),
    ("src/litetui/llm_backend.py", "BACKENDS", "backend"),
)
LIMITATIONS = [
    "Only committed Python files under src/litetui and tests/test*.py are read.",
    "All classes are candidates: bases, aliases and inheritance are not resolved.",
    "Bindings retain expressions; inherited/shared/conditional keys need manual reconciliation.",
    "RPC commands cover literal cmd_type equality/membership inside rpc._dispatch only.",
    "Emitter discovery matches rpc_emit/_rpc_emit call spellings, not aliases or indirect calls.",
    "Emitter fields are source expressions, NOT serialized schemas or correlation guarantees.",
    "Dynamic and installed plugins, rendered UI, services and runtime side effects are unverified.",
    "Test filenames are candidates, not execution, coverage or runner evidence.",
]


def serialize(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_sources(root: Path, revision: str) -> tuple[str, dict[str, str]]:
    """Read exact regular-file blobs; export attributes and working files cannot alter them."""
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"],
        text=True,
    ).strip()
    tree = subprocess.check_output(
        ["git", "-C", str(root), "ls-tree", "-rz", "--full-tree", commit, "src/litetui", "tests"],
    )
    entries = []
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        meta, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = meta.split()
        path = raw_path.decode("utf-8")
        selected = path.startswith("src/litetui/") or (
            path.startswith("tests/") and Path(path).name.startswith("test")
        )
        if selected and path.endswith(".py"):
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                raise ValueError(f"Non-regular source entry: {path}")
            entries.append((path, oid))
    data = subprocess.check_output(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input=b"".join(oid + b"\n" for _, oid in entries),
    )
    stream = io.BytesIO(data)
    sources = {}
    for path, expected_oid in entries:
        oid, kind, size = stream.readline().split()
        if oid != expected_oid or kind != b"blob":
            raise ValueError(f"Unexpected Git blob header: {path}")
        payload = stream.read(int(size))
        if len(payload) != int(size) or stream.read(1) != b"\n":
            raise ValueError(f"Truncated Git blob: {path}")
        sources[path] = payload.decode("utf-8")
    if stream.read(1):
        raise ValueError("Unexpected trailing Git blob data")
    return commit, sources


def _assignment(node: ast.AST, name: str) -> ast.expr | None:
    targets = node.targets if isinstance(node, ast.Assign) else (
        [node.target] if isinstance(node, ast.AnnAssign) else []
    )
    if any(isinstance(target, ast.Name) and target.id == name for target in targets):
        return node.value  # type: ignore[attr-defined]
    return None


def _literal(tree: ast.Module, name: str) -> tuple[ast.expr, Any]:
    values = [value for node in tree.body if (value := _assignment(node, name)) is not None]
    if len(values) != 1:
        raise ValueError(f"{name}: expected one top-level declaration, got {len(values)}")
    try:
        return values[0], ast.literal_eval(values[0])
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name}: nonliteral registry requires extractor review") from exc


def build_inventory(sources: dict[str, str], revision: str) -> dict[str, Any]:
    required = {path for path, _, _ in REGISTRIES} | {"src/litetui/rpc.py"}
    if missing := required - sources.keys():
        raise ValueError(f"Missing required source: {', '.join(sorted(missing))}")
    trees = {path: ast.parse(text, filename=path) for path, text in sorted(sources.items())}
    dispatchers = [node for node in trees["src/litetui/rpc.py"].body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_dispatch"]
    if len(dispatchers) != 1:
        raise ValueError("rpc.py: expected one top-level _dispatch function")
    records: list[dict[str, Any]] = []
    diagnostics: list[str] = []

    def add(category, name, path, node, symbol="", **details):
        records.append({
            "id": f"{category}:{path}:{node.lineno}:{node.col_offset}:{len(records)}",
            "category": category, "name": name, "status": "unverified",
            "source": {"path": path, "line": node.lineno, "end_line": node.end_lineno,
                       "column": node.col_offset, "end_column": node.end_col_offset, "symbol": symbol},
            "details": details,
        })

    _, protocol = _literal(trees["src/litetui/gui_rpc.py"], "PROTOCOL_VERSION")
    if type(protocol) is not int:
        raise ValueError("PROTOCOL_VERSION must be a literal integer")
    for path, variable, category in REGISTRIES:
        node, values = _literal(trees[path], variable)
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"{variable}: expected nonempty literal sequence")
        seen = set()
        for ordinal, value in enumerate(values):
            if category == "backend":
                if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(isinstance(v, str) for v in value):
                    raise ValueError(f"{variable}: expected name/label string pairs")
                name = value[0]
            else:
                name = value
            if not isinstance(name, str) or not name:
                raise ValueError(f"{variable}: expected nonempty string names")
            if name in seen:
                diagnostics.append(f"{path}:{node.lineno}: duplicate {variable} entry {name!r}")
            seen.add(name)
            add(category, "gui." + name if category == "gui_operation" else name,
                path, node, variable, ordinal=ordinal, value=value)

    class Scan(ast.NodeVisitor):
        def __init__(self, path):
            self.path = path
            self.scope: list[str] = []

        def record(self, category, name, node, **details):
            add(category, name, self.path, node, ".".join(self.scope), **details)

        def visit_ClassDef(self, node):
            self.record("class_candidate", ".".join([*self.scope, node.name]), node,
                        bases=[ast.unparse(base) for base in node.bases])
            self.scoped(node)

        def scoped(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = scoped
        visit_AsyncFunctionDef = scoped

        def visit_Assign(self, node):
            if (value := _assignment(node, "BINDINGS")) is not None:
                self.record("bindings", ".".join(self.scope) + ".BINDINGS", node,
                            expression=ast.unparse(value), resolution="manual_required")
            self.generic_visit(node)

        visit_AnnAssign = visit_Assign

        def visit_Compare(self, node):
            if self.path == "src/litetui/rpc.py" and self.scope == ["_dispatch"]:
                values = None
                if len(node.ops) == 1:
                    left, right = node.left, node.comparators[0]
                    is_cmd = lambda n: isinstance(n, ast.Name) and n.id == "cmd_type"
                    if isinstance(node.ops[0], ast.Eq) and (is_cmd(left) or is_cmd(right)):
                        values = [right if is_cmd(left) else left]
                    elif isinstance(node.ops[0], ast.In) and is_cmd(left):
                        values = right.elts if isinstance(right, (ast.Tuple, ast.List, ast.Set)) else [right]
                if values is not None:
                    for value in values:
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            self.record("rpc_command", value.value, node)
                        else:
                            diagnostics.append(f"{self.path}:{node.lineno}: unresolved command comparison")
            self.generic_visit(node)

        def visit_Call(self, node):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            )
            if name in {"rpc_emit", "_rpc_emit"}:
                payload = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg == "data"), None)
                fields = {}
                unresolved = []
                event_type = "<dynamic>"
                if isinstance(payload, ast.Dict):
                    for key, value in zip(payload.keys, payload.values):
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            fields[key.value] = ast.unparse(value)
                            if key.value == "type":
                                event_type = value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else "<dynamic>"
                        else:
                            event_type = "<dynamic>"
                            unresolved.append("**" + ast.unparse(value) if key is None else ast.unparse(key))
                else:
                    unresolved.append(ast.unparse(payload) if payload is not None else "<missing payload>")
                self.record("event_emitter", event_type, node, callee=ast.unparse(node.func),
                            fields=fields, unresolved=unresolved)
            self.generic_visit(node)

    for path, tree in trees.items():
        if path.startswith("src/litetui/"):
            Scan(path).visit(tree)
    return {
        "schema_version": 1, "extractor_version": 1, "source_revision": revision,
        "generator": {"path": "scripts/rust_frontend_inventory.py",
                      "sha256": hashlib.sha256(Path(__file__).read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
                      "python": list(sys.version_info[:3])},
        "evidence_kind": "static_source", "imports_application": False,
        "protocol_version": protocol, "limitations": LIMITATIONS,
        "inputs": [{"path": path, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                   for path, text in sorted(sources.items())],
        "counts": dict(sorted(Counter(row["category"] for row in records).items())),
        "records": records, "diagnostics": diagnostics,
        "tests": [{"path": path, "status": "not_run"} for path in sorted(trees) if path.startswith("tests/")],
    }


def seed_parity(inventory: dict[str, Any], path: Path) -> None:
    rows = []
    for record in inventory["records"]:
        rows.append({
            "id": record["id"], "source_revision": inventory["source_revision"],
            "source_paths": [record["source"]["path"]], "discovery": record,
            **dict.fromkeys(("entry_action", "preconditions", "request", "response", "events",
                             "state_owner", "rust_surface", "positive_check", "negative_probe")),
            "terminals": {"litesuite_embedded": "not_run", "windows_terminal": "not_run"},
            "status": "blocked", "blockers": ["Static discovery only; reconcile behavior and contracts"],
            "evidence": [],
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(serialize({"schema_version": 1, "rows": rows}))


def write_static(path: Path, rendered: str) -> None:
    """Replace only generated static evidence, never a parity ledger or arbitrary file."""
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("evidence_kind") != "static_source":
            raise ValueError(f"Refusing to overwrite non-static evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".static-inventory-", delete=False) as output:
            staging = Path(output.name)
            output.write(rendered)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, path)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", default=BASELINE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="compare only; never write")
    parser.add_argument("--seed-parity", type=Path, help="create blocked candidate rows; refuses overwrite")
    args = parser.parse_args()
    if args.check and args.seed_parity:
        parser.error("--check cannot be combined with --seed-parity")
    if args.seed_parity and args.output.resolve() == args.seed_parity.resolve():
        parser.error("static output and parity seed must be different files")
    try:
        revision, sources = load_sources(args.root, args.revision)
        inventory = build_inventory(sources, revision)
        rendered = serialize(inventory)
        if args.check:
            if args.output.read_text(encoding="utf-8") != rendered:
                raise ValueError("static inventory is stale")
        else:
            # A failed static write must never strand a parity seed. If seed creation
            # fails afterwards, refreshed static evidence remains; existing parity is untouched.
            write_static(args.output, rendered)
            if args.seed_parity:
                seed_parity(inventory, args.seed_parity)
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Inventory failed: {exc}\n")
    print(f"Static evidence only: {revision}; {inventory['counts']}; {len(inventory['tests'])} test candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
