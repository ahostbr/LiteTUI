"""Trusted local exception frames; external subprocess payload is never captured.

External stderr/stdout cannot authenticate frame/type/message provenance.
Record only fixed parent-known stage, subprocess status and parse-failed flag;
there is deliberately NO external-chain parser or child structured protocol.
Actual in-process exception objects retain their frame locations, chained types
and numeric OS codes, never source lines, locals or exception messages.
"""

from __future__ import annotations

from types import TracebackType

from litetui import runtime_log

OWNED_REGISTRATION_FAILURE = "Owned agent registration blocked: owned-registration-failed; see runtime-errors.log"
OWNED_PRESENCE_FAILURE = "Owned presence registration failed; see runtime-errors.log"


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
    """Walk trusted actual traceback objects without reading locals or source."""
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


def record_failure(
    *,
    stage: str,
    returncode: int | None = None,
    exc: BaseException | None = None,
) -> None:
    """External payload is not even an argument, never parsed or logged.

    Local objects are accurately labeled; their frames do not reconstruct the
    external child exception. The existing sink supplies the timestamp.
    """
    if exc is None:
        runtime_log.record_error(
            "harness_registration_diagnostic",
            stage=stage,
            returncode=returncode,
            parse_failed=True,
        )
    else:
        runtime_log.record_error(
            "harness_registration_diagnostic",
            stage=stage,
            returncode=returncode,
            exceptions=exception_chain(exc),
            parse_failed=False,
            format="trusted-in-process-exception-objects-not-external-chain",
        )
