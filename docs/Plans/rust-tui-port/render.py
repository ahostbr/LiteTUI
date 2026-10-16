"""Generate the static source inventory and render/copy the Rust-port plan.

Documentation only. No application imports, runtime launches or state mutation.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[3]
PLAN = ROOT / "Docs/Plans/rust-tui-port"
EXPORT = ROOT / "artifacts/rust-tui-port"
STYLE = PLAN / "plan.css"


def source_tree(relative):
    return ast.parse((ROOT / relative).read_text(encoding="utf-8-sig"))


def assignment(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                return ast.literal_eval(node.value)
    raise ValueError(name)


def inventory():
    tracked = subprocess.check_output(["git", "ls-files", "src/litetui", "tests", "e2e"], cwd=ROOT, text=True).splitlines()
    revision = subprocess.check_output(["git", "rev-parse", "9ef5337"], cwd=ROOT, text=True).strip()
    sources = [p for p in tracked if p.endswith(".py") and p.startswith("src/")]
    lines = ["# Static source inventory", "", f"Baseline: `{revision}`. Generated from tracked source only; {len(sources)} Python source files.",
             "", "This is discovery evidence, NOT an exhaustive behavioral parity inventory. Dynamic/custom plugins, resolved theme colors, live UI paths and event schemas still require P0. No application modules were imported to generate this file.",
             "", "Master: [master.md](master.md). Specification: [sub-terminal.md](sub-terminal.md).", "", "## GUI management operations", ""]
    operations = assignment(source_tree("src/litetui/gui_rpc.py"), "OPERATIONS")
    lines += [f"- [] `gui.{name}`" for name in operations]
    lines += ["", "## Backends", ""]
    lines += [f"- [] `{name}` — {description}" for name, description in assignment(source_tree("src/litetui/llm_backend.py"), "BACKENDS")]
    lines += ["", "## Plugin load order", ""]
    lines += [f"- [] `{item}`" for item in assignment(source_tree("src/litetui/plugins/__init__.py"), "PLUGIN_LOAD_ORDER")]
    lines += ["", "## Literal RPC command comparisons", "", "Includes only literal cmd_type comparisons; dynamic dispatch is covered separately by GUI operations.", ""]
    rpc = source_tree("src/litetui/rpc.py")
    commands = set()
    for n in ast.walk(rpc):
        if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "cmd_type":
            for v in n.comparators:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    commands.add(v.value)
    lines += [f"- [] `{command}`" for command in sorted(commands)]
    lines += ["", "## UI classes and literal Binding calls", "", "Syntactic candidates, not proof each is user-visible. Base classes and tuples may introduce additional bindings. Each row needs reconciliation against runtime navigation.", ""]
    for path in sources:
        tree = source_tree(path)
        relevant = []
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                bases = [ast.unparse(b) for b in n.bases]
                if any(any(tag in b for tag in ("Screen", "Widget", "App", "Vertical", "Horizontal", "Static", "Input", "Button", "TextArea", "Scroll")) for b in bases):
                    relevant.append(f"- [] `{path}:{n.lineno}` — `{n.name}({', '.join(bases)})`")
            elif isinstance(n, ast.Call) and ((isinstance(n.func, ast.Name) and n.func.id == "Binding") or (isinstance(n.func, ast.Attribute) and n.func.attr == "Binding")):
                vals = [a.value for a in n.args[:3] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if vals:
                    relevant.append(f"- [] `{path}:{n.lineno}` — Binding: `{' | '.join(vals)}`")
        if relevant:
            lines.extend(relevant)
    lines += ["", "## Test candidates", "", "Filename discovery only. Determine pytest versus script runner, dependency scope and live-service opt-in before execution.", ""]
    groups = ("rpc", "gui", "sidecar", "stream", "turn", "plugin", "settings", "input", "cli", "conversation", "resume", "tool_approval", "task", "footer", "owned", "agent", "stt", "voice", "image", "scheduler", "calendar", "mcp", "hook")
    lines += [f"- [] `{p}`" for p in tracked if p.startswith(("tests/", "e2e/")) and p.endswith(".py") and any(g in Path(p).stem for g in groups)]
    return "\n".join(lines) + "\n"


def render(source, name, css):
    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(source)
    for token in tokens:
        for child in token.children or []:
            if child.type == "link_open":
                href = child.attrGet("href")
                if href and href.endswith(".md") and ":" not in href:
                    child.attrSet("href", href[:-3] + ".html")
    body = md.renderer.render(tokens, md.options, {})
    digest = hashlib.sha256(source.encode()).hexdigest()
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>LiteTUI Rust port — {name}</title><style>{css}</style></head>'
            f'<body><main><nav><a href="master.html">Master</a> · '
            f'<a href="sub-terminal.html">Specification</a> · '
            f'<a href="inventory.html">Inventory</a></nav>'
            f'<!-- Markdown SHA256: {digest} -->{body}</main></body></html>\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--preserve-inventory", action="store_true",
                        help="Render the existing inventory without refreshing historical discovery evidence")
    args = parser.parse_args()
    css = STYLE.read_text(encoding="utf-8")
    expected = {"inventory.md": (PLAN / "inventory.md").read_text(encoding="utf-8")
                if args.preserve_inventory else inventory()}
    for name in ("master", "sub-terminal", "runtime-contract"):
        expected[f"{name}.md"] = (PLAN / f"{name}.md").read_text(encoding="utf-8")
    for name in ("master", "sub-terminal", "inventory", "runtime-contract"):
        expected[f"{name}.html"] = render(expected[f"{name}.md"], name, css)
    for name, content in expected.items():
        for directory in (PLAN, EXPORT):
            path = directory / name
            if args.check:
                if not path.exists() or path.read_text(encoding="utf-8") != content:
                    raise SystemExit(f"STALE/MISSING: {path}")
            else:
                directory.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
    for directory in (PLAN, EXPORT):
        for name in expected:
            if name.endswith(".html"):
                for href in re.findall(r'href="([^"]+)"', (directory / name).read_text(encoding="utf-8")):
                    if ":" not in href and not (directory / href).is_file():
                        raise SystemExit(f"BROKEN LINK: {name}: {href}")
    print(f"{'CHECKED' if args.check else 'GENERATED'}: 4 Markdown + 4 HTML files in both Docs/Plans and artifacts; links valid.")
    print("Documentation-only result; no product verification implied.")


if __name__ == "__main__":
    main()
