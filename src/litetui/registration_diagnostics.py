"""Complete sanitized registration frames, never raw exception payloads.

The error sink gets frame locations, chained exception types and numeric OS
codes. It never gets source lines, locals, arbitrary messages or subprocess
output. Unparseable external output is an explicit diagnostic, not silence.
"""

from __future__ import annotations

import re
from types import TracebackType

from litetui import runtime_log

OWNED_REGISTRATION_FAILURE = "Owned agent registration blocked: owned-registration-failed; see runtime-errors.log"
OWNED_PRESENCE_FAILURE = "Owned presence registration failed; see runtime-errors.log"

_FRAME = re.compile(r'^\s*File "([^"\r\n]+)", line ([0-9]+), in ([^\r\n]+)$')
_TYPE = re.compile(r"^((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)(?::|$)")
_ERRNO = re.compile(r"\[Errno (-?[0-9]+)\]")
_WINERROR = re.compile(r"\[WinError (-?[0-9]+)\]")
_CAUSE = "The above exception was the direct cause of the following exception:"
_CONTEXT = "During handling of the above exception, another exception occurred:"


def _frames(tb: TracebackType | None) -> list[dict]:
    frames = []
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append(
            {"file": code.co_filename, "line": tb.tb_lineno, "function": code.co_name}
        )
        tb = tb.tb_next
    return frames


def exception_chain(exc: BaseException) -> list[dict]:
    """Walk actual traceback objects without reading locals or source lines."""
    seen = set()
    chain = []

    def visit(error, relation):
        if id(error) in seen:
            return
        seen.add(id(error))
        if error.__cause__ is not None:
            visit(error.__cause__, "cause")
        elif error.__context__ is not None and not error.__suppress_context__:
            visit(error.__context__, "context")
        item = {
            "type": type(error).__name__,
            "relation": relation,
            "frames": _frames(error.__traceback__),
        }
        for key in ("errno", "winerror"):
            value = getattr(error, key, None)
            if type(value) is int:
                item[key] = value
        chain.append(item)
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                visit(child, "group-member")

    visit(exc, "root")
    return chain


def subprocess_chain(stderr: str) -> tuple[list[dict], bool]:
    """Keep all standard Python frame headers; discard every payload line."""
    chain = []
    frames = []
    relation = "root"
    parse_failed = False
    for line in stderr.splitlines():
        match = _FRAME.fullmatch(line)
        if match:
            frames.append(
                {"file": match[1], "line": int(match[2]), "function": match[3]}
            )
            continue
        match = _TYPE.match(line)
        if match and frames:
            item = {"type": match[1], "relation": relation, "frames": frames}
            frames = []
            for key, pattern in (("errno", _ERRNO), ("winerror", _WINERROR)):
                number = pattern.search(line)
                if number:
                    item[key] = int(number[1])
            chain.append(item)
            relation = "root"
        elif line == _CAUSE:
            if chain:
                chain[-1]["relation"] = "cause"
            else:
                parse_failed = True
        elif line == _CONTEXT:
            if chain:
                chain[-1]["relation"] = "context"
            else:
                parse_failed = True
        elif line.strip().startswith(
            ("File ", "[Previous line repeated", "+ Exception Group")
        ):
            parse_failed = True  # unsupported/compressed frame syntax is explicit
    if frames or not chain:
        chain.append({"type": "unknown", "relation": "root", "frames": frames})
        parse_failed = True
    return chain, parse_failed


def record_failure(
    *,
    stage: str,
    returncode: int | None = None,
    exc: BaseException | None = None,
    stderr: str = "",
) -> None:
    """Structured raw-sink metadata avoids its detail-length truncation.

    No raw `exc` or `detail` goes to the recorder: those would include arbitrary
    payloads/source lines. All sanitized frames survive the old 200-char cutoff.
    """
    if exc is not None:
        chain, failed = exception_chain(exc), False
    else:
        chain, failed = subprocess_chain(stderr)
    runtime_log.record_error(
        "harness_registration_diagnostic",
        stage=stage,
        returncode=returncode,
        exceptions=chain,
        parse_failed=failed,
        format="complete-sanitized-frames-not-raw-payload",
    )
