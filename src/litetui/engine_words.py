"""What the user reads while an engine starts, in plain words (T1338, T1339).

The engine's own log is for the log file. ninfer-serve prints timestamped, levelled lines
(`2026-11-17 20:01:21.611  ERROR minimum Engine runtime reservation requires 7683126272 bytes
...`); until now LiteTUI pasted the last few of them into the chat when a start failed, so an
end user read dates, a device-profile history and byte counts, and a VRAM shortfall looked like
"exited before it was ready". Everything here turns that stream into one short sentence, and
leaves the raw lines where they already are (`ninfer_engine.log_path()`).

Pure functions: no I/O, no settings, nothing to mock.
"""
from __future__ import annotations

import re

#: A levelled engine line: `2026-11-17 20:01:21.611  ERROR <text>`. The optional `engine | `
#: is the product logger's own channel tag.
_LINE = re.compile(r"^\s*\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?\s+(?P<level>[A-Z]+)\s+(?:engine \| )?(?P<text>.*?)\s*$")
_ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL"}
#: A trailing `| 258 ms` / `| 5.0s` timing on a phase line.
_TIMING = re.compile(r"\s*\|\s*[\d.]+\s*(?:ms|s)\s*$")
_GIB = 1024 ** 3

#: ninfer-serve, when the weights fit but the runtime's own reservation does not
#: (recorded from a real failure with --max-context 150000 --max-concurrency 2).
_VRAM = re.compile(
    r"requires\s+(?P<need>\d+)\s+bytes\s+in\s+addition\s+to\s+(?P<headroom>\d+)\s+bytes\s+of\s+automatic\s+headroom"
    r",\s*but\s+only\s+(?P<free>\d+)\s+bytes\s+are\s+available\s+after\s+weights", re.I)

#: The start failed for a reason that the same settings will hit again.
REASON_VRAM = "vram"


def _gib(n: int) -> str:
    value = n / _GIB
    return f"{value:.0f} GiB" if abs(value - round(value)) < 0.05 else f"{value:.1f} GiB"


def option_value(args, flag: str) -> str | None:
    """The value after `flag` in an argv, or None."""
    items = [str(a) for a in args or ()]
    for i, item in enumerate(items[:-1]):
        if item == flag:
            return items[i + 1]
    return None


def error_lines(text: str) -> list[str]:
    """The ERROR-level messages in `text`, oldest first, with the timestamp and level gone."""
    out = []
    for raw in text.replace("\r", "\n").splitlines():
        m = _LINE.match(raw)
        if m and m.group("level") in _ERROR_LEVELS and m.group("text"):
            out.append(m.group("text"))
    return out


def _phase(line: str) -> str:
    """`startup failed | finalizing target | 258 ms` -> `startup failed, finalizing target`."""
    return _TIMING.sub("", line).replace(" | ", ", ")


def vram_sentence(need: int, headroom: int, free: int, *, context=None, concurrency=None) -> str:
    knobs = []
    if context:
        knobs.append(f"the context ({context})")
    if concurrency:
        knobs.append(f"concurrency ({concurrency})")
    advice = f"Lower {' or '.join(knobs)}, or free GPU memory." if knobs else "Lower the context or concurrency, or free GPU memory."
    return (f"Not enough VRAM: needs {_gib(need)} + {_gib(headroom)} headroom after the weights, "
            f"{_gib(free)} free. {advice}")


def explain_failure(fresh: str, args=()) -> tuple[str, str | None]:
    """`(sentence, reason)` for a start that ended on its own. `fresh` is the engine's output of
    THIS spawn only. `reason` is set (REASON_*) when the same settings will fail the same way, so
    nothing should start them again unchanged; None when it is not known to repeat.

    Only ERROR lines are read: the INFO lines (build id, device profile, load progress) are the
    engine talking to its log, not to the person who pressed send."""
    errors = error_lines(fresh)
    for line in errors:
        m = _VRAM.search(line)
        if m:
            return (vram_sentence(int(m["need"]), int(m["headroom"]), int(m["free"]),
                                  context=option_value(args, "--max-context"),
                                  concurrency=option_value(args, "--max-concurrency")), REASON_VRAM)
    # `startup failed | <phase>` only says where; any other ERROR line says why.
    why = [e for e in errors if not e.lower().startswith("startup failed")]
    if why:
        return "; ".join(_phase(e) for e in why[-2:])[:300], None
    if errors:
        return _phase(errors[-1]), None
    return "it stopped without saying why", None


def plain_progress(line: str) -> str:
    """One load-progress line as a status: `loading weights | 20.8 GiB` -> `Loading weights 20.8 GiB`,
    `loading weights 82.7% | 45.0 GiB/54.4 GiB | 1.2 GiB/s | ETA 2.0s` -> `Loading weights 82.7% (45.0 of 54.4 GiB)`."""
    text = line.strip()
    low = text.lower()
    if low.startswith("loading weights"):
        percent = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        sizes = re.search(r"(\d+(?:\.\d+)?)\s*GiB\s*/\s*(\d+(?:\.\d+)?)\s*GiB", text)
        total = re.search(r"\|\s*(\d+(?:\.\d+)?\s*GiB)\s*(?:\||$)", text)
        if percent and sizes:
            return f"Loading weights {percent.group(1)}% ({sizes.group(1)} of {sizes.group(2)} GiB)"
        if percent:
            return f"Loading weights {percent.group(1)}%"
        if total:
            return f"Loading weights {total.group(1)}"
        return "Loading weights"
    if low.startswith("weights ready"):
        return "Weights loaded"
    if low.startswith("cuda graphs ready"):
        return "GPU graphs ready"
    if low.startswith("engine ready"):
        return "Ready"
    return text


def clean_tail(fresh: str, lines: int = 3, limit: int = 400) -> str:
    """The end of an engine's output without timestamps or levels, for an engine whose failure is
    not a levelled ERROR line (a Python server's traceback). INFO lines are dropped."""
    kept = []
    for raw in fresh.replace("\r", "\n").splitlines():
        m = _LINE.match(raw)
        if m:
            if m.group("level") in {"INFO", "DEBUG", "TRACE"}:
                continue
            raw = m.group("text")
        if raw.strip():
            kept.append(raw.strip())
    return " ".join(kept[-lines:])[-limit:] or "(it printed nothing)"
