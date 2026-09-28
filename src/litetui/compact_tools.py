"""Pure, deliberately lossy *headers* for compact tools. Original payload stays on ToolMessage."""
import json
import re

from rich.cells import cell_len


def _path(value):
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def _count_lines(value):
    return len(value.splitlines()) if value else 0


def summary(name: str, args_json: str, result: str | None, ok: bool, width: int, duration: str) -> str | None:
    """Return one cell-width line, or None to retain the full JSON view.

    Unknown/incomplete arguments must not be upgraded into invented facts.
    """
    try:
        args = json.loads(args_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(args, dict):
        return None
    kind = name.lower().replace("__", "/").rsplit("/", 1)[-1].split(".")[-1]
    target = ""
    detail = ""
    glyph = "▸"
    if kind in ("read", "edit", "write"):
        target = _path(args.get("path"))
        if not target:
            return None
        if kind == "read":
            offset, limit = args.get("offset"), args.get("limit")
            detail = (f":{offset}" if isinstance(offset, int) else "") + (f" +{limit}" if isinstance(limit, int) else "")
        elif kind == "write":
            glyph = "✎"
            content = args.get("content")
            detail = f" new {_count_lines(content)} lines" if isinstance(content, str) else ""
        else:
            glyph = "✎"
            # old_string/new_string are not a diff: no line count is honest until
            # a unified patch or actual edit result supplies one.
            match = re.search(r"(\d+) insertions?\(\+\).*?(\d+) deletions?\(-\)", result or "")
            detail = f" +{match[1]} −{match[2]}" if match else ""
    elif kind in ("bash", "powershell", "shell", "exec", "command", "commandexecution"):
        command = args.get("command") or args.get("cmd")
        if not isinstance(command, str) or not command.strip():
            return None
        glyph = "$"
        target = command.strip().split()[0]
        if result is not None:
            match = re.search(r"(?:exit code|code|status)[: =]+(-?\d+)", result, re.I)
            detail = f" exit {match[1]}" if match else (" ✓" if ok else " ✗")
            tests = re.search(r"(\d+) passed(?:, (\d+) failed)?", result)
            if tests:
                detail += f" ✓{tests[1]}" + (f" ✗{tests[2]}" if tests[2] else "")
    elif kind in ("grep", "search"):
        glyph = "⌕"
        target = str(args.get("pattern") or "")
        if not target:
            return None
        if result is not None:
            match = re.search(r"(\d+) hits? (?:in|/) (\d+) files?", result)
            detail = f" {match[1]} hits/{match[2]} files" if match else ""
    elif kind == "file changes" or kind == "filechange":
        glyph = "✎"
        target = _path(args.get("path")) or "files"
        changes = args.get("changes")
        if isinstance(changes, list):
            detail = f" {len(changes)} changes"
    elif kind == "web search" or kind == "websearch":
        glyph = "⌕"
        target = str(args.get("query") or "web")
    elif "." in name or "/" in name or name.startswith("mcp__"):
        target = name.replace("mcp__", "", 1).replace("__", ".")
    else:
        return None
    suffix = f"  {duration}"
    prefix = f"{glyph} {kind}  "
    if result is None:
        suffix = "  running " + duration
    elif not ok and "✗" not in detail:
        suffix = "  ✗" + suffix
    budget = max(0, width - cell_len(prefix + detail + suffix))
    if budget == 0:
        target = ""
    elif cell_len(target) > budget:
        left = (budget - 1) // 2
        right = budget - 1 - left
        target = target[:left] + "…" + (target[-right:] if right else "") if budget > 1 else "…"
    # Detail can exceed the budget at very narrow widths. Rich cell slicing
    # handles wide glyphs without turning one terminal row into two.
    from rich.text import Text
    return Text(prefix + target + detail + suffix)[:width].plain
