"""Monitored-target configuration.

A *target* is one URL the monitor should visit on each sweep, plus the
optional expectations we have about what it should show. Keeping the config as
plain data (a frozen dataclass) means the engine, the probe, the metrics
renderer and the tests all agree on one shape, and the on-disk file stays
boring to edit by hand.

Two on-disk formats are accepted so the file can be either hand-friendly
(TOML) or machine-friendly (JSON) without a second loader to maintain:
a path ending in ``.toml`` is parsed with stdlib :mod:`tomllib`, anything else
as JSON. Both expect the same shape:

    {"targets": [{"name": "example", "url": "https://example.com",
                   "expect_text": "Example Domain"}]}
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Target:
    """One URL to visit, and what a healthy visit looks like.

    ``expect_text`` is the lightest useful health check that needs no DOM
    knowledge: if set, a visit is only ``ok`` when the page text contains the
    substring. ``text_selector`` narrows the text we read to one subtree so a
    big page does not drown a small expectation.
    """

    name: str
    url: str
    expect_text: str | None = None
    text_selector: str | None = None
    screenshot: bool = True
    timeout_ms: int = 30000

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Target.name must be non-empty")
        if not self.url or not self.url.startswith(("http://", "https://")):
            raise ValueError(
                f"Target.url must be an http(s) URL, got {self.url!r}"
            )

    def as_labels(self) -> dict[str, str]:
        """Prometheus labels for this target. Deliberately a fixed, small set:
        ``job`` is added by the sink, so cardinality is bounded by the number
        of targets, not by page content."""
        return {"name": self.name, "url": self.url}


def _coerce(raw: dict[str, Any]) -> Target:
    return Target(
        name=str(raw["name"]),
        url=str(raw["url"]),
        expect_text=raw.get("expect_text"),
        text_selector=raw.get("text_selector"),
        screenshot=bool(raw.get("screenshot", True)),
        timeout_ms=int(raw.get("timeout_ms", 30000)),
    )


def load_targets(path: str | Path) -> list[Target]:
    """Parse a target file into a list. An empty file yields an empty list —
    that is a valid (if useless) config, not an error, so a freshly-created
    file does not crash the first ``monitor run``."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".toml":
        data: dict[str, Any] = tomllib.loads(text)
    else:
        data = json.loads(text)
    raw_list = data.get("targets", [])
    return [_coerce(item) for item in raw_list]


def default_config() -> dict[str, Any]:
    """A seed config the CLI can write when the file does not exist yet.
    example.com and httpbin.org are both stable, low-churn public pages."""
    return {
        "targets": [
            {
                "name": "example",
                "url": "https://example.com",
                "expect_text": "Example Domain",
                "screenshot": True,
            },
            {
                "name": "httpbin",
                "url": "https://httpbin.org/html",
                "screenshot": True,
            },
        ]
    }
