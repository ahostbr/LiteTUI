"""The probe: turn one visit to a target into a structured result.

The engine never talks to a browser directly; it calls a ``BrowserProbe``.
That indirection is the whole reason this file exists — the real probe wraps
the chrome-bridge ``Chrome`` class, and a ``FakeProbe`` stands in for the
tests. Everything downstream of the probe (metrics, regression, reporting)
sees the same ``ProbeResult`` either way.

Load time is measured as wall-clock around ``navigate()``, which the bridge
documents as waiting for the load to complete. It is a coarse proxy, not a
Performance-API LCP — honest about that is better than a fake-precise number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from litetui.monitor.targets import Target

# Where the chrome-bridge lives, derived from this file so the probe finds it
# without configuration — the same logic chrome_tool.py uses for its SCRIPT
# constant. probe.py is at <ROOT>/src/litetui/monitor/, so ROOT is parents[3].
_DEFAULT_BRIDGE_DIR = Path(__file__).resolve().parents[3] / "tools" / "chrome-bridge"


@dataclass
class ProbeResult:
    """The outcome of visiting one target. ``status`` is ``"ok"`` or
    ``"error"``; ``ok`` is the derived boolean the engine and the ``up``
    metric both read, so the two can never disagree."""

    url: str
    ok: bool
    status: str = "ok"
    error: str = ""
    final_url: str = ""
    title: str = ""
    load_ms: float = 0.0
    text_chars: int = 0
    expect_met: bool = True
    screenshot_path: str = ""
    screenshot_bytes: int = 0

    def __post_init__(self) -> None:
        # Derive, never store: ok and status are two views of the same fact.
        self.ok = self.status == "ok"
        # A failed visit never read the page, so it cannot have met its
        # expectation. Forcing this keeps the "missing expect" signal honest —
        # it means "loaded but wrong", never "couldn't load at all" (that is
        # the up metric's job).
        if not self.ok:
            self.expect_met = False


class BrowserProbe(Protocol):
    """Anything that can visit a target and report back. The engine only ever
    sees this, which is what keeps it testable with no browser and no network."""

    def probe(self, target: Target) -> ProbeResult: ...


class ChromeProbe:
    """The real probe. Wraps the chrome-bridge ``Chrome`` class.

    ``bridge`` is imported lazily inside :meth:`probe` so that merely importing
    this module (which happens when the plugin registers) never drags in the
    websocket stack or a missing bridge dir. A missing bridge surfaces as a
    clean ``"error"`` result for the target, not an import crash of the engine.
    """

    def __init__(
        self,
        bridge_dir: str | Path | None = None,
        shot_dir: str | Path = Path("shots"),
        connect_timeout: float = 15.0,
        call_timeout: float = 45.0,
        port: int = 7461,
    ) -> None:
        self.bridge_dir = Path(bridge_dir) if bridge_dir else _DEFAULT_BRIDGE_DIR
        self.shot_dir = Path(shot_dir)
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self.port = port

    def _import_chrome(self):
        import sys

        if self.bridge_dir is not None:
            sys.path.insert(0, str(self.bridge_dir))
        from bridge import Chrome  # type: ignore  # noqa: PLC0415

        return Chrome

    def probe(self, target: Target) -> ProbeResult:
        shot_dir = self.shot_dir
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot_path = shot_dir / f"{target.name}.png"

        try:
            Chrome = self._import_chrome()
        except Exception as exc:  # missing bridge / extension: report, don't crash
            return ProbeResult(
                url=target.url,
                ok=False,
                status="error",
                error=f"bridge unavailable: {type(exc).__name__}: {exc}",
            )

        try:
            with Chrome(
                port=self.port,
                connect_timeout=self.connect_timeout,
                call_timeout=self.call_timeout,
            ) as ch:
                started = time.perf_counter()
                nav = ch.navigate(
                    target.url, new_tab=True, timeout_ms=target.timeout_ms
                )
                load_ms = (time.perf_counter() - started) * 1000.0
                final_url = nav.get("url", target.url) if isinstance(nav, dict) else target.url

                content = ch.content()
                text = content.get("text", "") if isinstance(content, dict) else ""
                title = content.get("title", "") if isinstance(content, dict) else ""

                screenshot_path = ""
                screenshot_bytes = 0
                if target.screenshot:
                    res = ch.screenshot(path=str(shot_path))
                    screenshot_path = str(shot_path)
                    screenshot_bytes = int(res.get("bytes", 0)) if isinstance(res, dict) else 0

        except Exception as exc:
            return ProbeResult(
                url=target.url,
                ok=False,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
            )

        expect_met = True
        if target.expect_text is not None:
            expect_met = target.expect_text in text
            if not expect_met:
                # The page loaded but is missing the expected content: that is a
                # real degradation, not a crash, so keep status "ok" and let the
                # expect metric carry the signal rather than the up metric.
                pass

        return ProbeResult(
            url=target.url,
            ok=True,
            status="ok",
            final_url=final_url,
            title=title,
            load_ms=load_ms,
            text_chars=len(text),
            expect_met=expect_met,
            screenshot_path=screenshot_path,
            screenshot_bytes=screenshot_bytes,
        )


class FakeProbe:
    """A deterministic probe for tests and for the ``monitor demo`` command:
    no browser, no network. Given a name it returns a canned result; names in
    ``fail`` raise the browser-error path so the engine's error handling gets
    exercised too."""

    def __init__(
        self,
        fail: frozenset[str] = frozenset(),
        load_ms: float = 120.0,
        text: str = "Example Domain\n\nThis domain is for use in illustrative examples.",
    ) -> None:
        self.fail = frozenset(fail)
        self.load_ms = load_ms
        self.text = text
        self.calls: list[str] = []

    def probe(self, target: Target) -> ProbeResult:
        self.calls.append(target.name)
        if target.name in self.fail:
            return ProbeResult(
                url=target.url,
                ok=False,
                status="error",
                error="FakeBrowserError: connection reset",
            )
        expect_met = True
        if target.expect_text is not None:
            expect_met = target.expect_text in self.text
        return ProbeResult(
            url=target.url,
            ok=True,
            status="ok",
            final_url=target.url,
            title=target.name,
            load_ms=self.load_ms,
            text_chars=len(self.text),
            expect_met=expect_met,
        )
