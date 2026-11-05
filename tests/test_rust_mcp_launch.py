"""MCP capability is an invocation-only startup-off overlay, never a startup grant."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from litetui import cli, launch_options, mcp_client, settings
from litetui.launch_options import LaunchOptions


def args(extra=()):
    parser = argparse.ArgumentParser()
    for name in ("backend", "model", "prompt"):
        parser.add_argument("--" + name)
    parser.add_argument("--rpc", action="store_true")
    parser.add_argument("--hold-restored-input", action="store_true")
    launch_options.add_arguments(parser)
    return parser.parse_args(extra)


@pytest.mark.parametrize("value", [True, False, 0, 2, "1", 1.0])
def test_bad_mcp_version_is_rejected_before_any_effect(value):
    options = LaunchOptions(work_lifecycle_version=1, mcp_lifecycle_version=value)
    with pytest.raises(ValueError, match="mcp-lifecycle"):
        options.overrides(settings.Settings(), "custom", "model")
    with pytest.raises(ValueError, match="mcp-lifecycle"):
        launch_options.to_argv(options)
    with pytest.raises(ValueError, match="mcp-lifecycle"):
        asyncio.run(launch_options.prepare(SimpleNamespace(_launch_options=options)))


def test_launch_flag_roundtrip_sets_only_off_overlay_and_legacy_default_stays_unchanged():
    old = launch_options.from_args(args())
    assert old.mcp_lifecycle_version is None
    assert "--mcp-lifecycle-version" not in launch_options.to_argv(old)
    selected = launch_options.from_args(
        args(
            [
                "--rpc",
                "--hold-restored-input",
                "--work-lifecycle-version",
                "1",
                "--mcp-lifecycle-version",
                "1",
            ]
        )
    )
    assert selected.overrides(
        settings.Settings(mcp_enabled=True), "custom", "model"
    ) == {"mcp_enabled": False}
    assert (
        launch_options.from_args(
            args(["--rpc", "--hold-restored-input", *launch_options.to_argv(selected)])
        )
        == selected
    )


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--work-lifecycle-version", "1"],
        ["--rpc", "--hold-restored-input"],
        [
            "--rpc",
            "--hold-restored-input",
            "--work-lifecycle-version",
            "1",
            "--prompt",
            "unsafe",
        ],
        [
            "--rpc",
            "--hold-restored-input",
            "--work-lifecycle-version",
            "1",
            "--export-output",
            "never.md",
        ],
    ],
)
def test_cli_refuses_missing_work_or_prompt_or_export_before_ownership(
    monkeypatch, capsys, extra
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Invalid MCP launch reached settings")

    monkeypatch.setattr(settings, "load", forbidden)
    monkeypatch.setattr(
        sys, "argv", ["litetui", "--mcp-lifecycle-version", "1", *extra]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and "error:" in capsys.readouterr().err


@pytest.mark.real_mcp_load
@pytest.mark.parametrize("environment", [False, True])
def test_constructor_and_conversation_adoption_keep_saved_true_off(
    make_app, monkeypatch, environment
):
    # This synthetic environment mapping exercises the ordinary Settings loader;
    # no actual MCP environment override is currently registered in the product.
    if environment:
        monkeypatch.setitem(
            settings.ENV_OVERRIDES, "mcp_enabled", "PRIVATE_MCP_STARTUP"
        )
        monkeypatch.setenv("PRIVATE_MCP_STARTUP", "true")
    settings.save(settings.Settings(backend="custom", mcp_enabled=True))

    def forbidden(*_args, **_kwargs):
        pytest.fail("Startup-off capability opened or reloaded MCP")

    for name in ("reload_configs", "load", "connect"):
        monkeypatch.setattr(mcp_client.MCPManager, name, forbidden)
    app = make_app(
        rpc=True,
        initial_backend="custom",
        initial_model="fixture",
        hold_restored_input=True,
        launch_options=LaunchOptions(work_lifecycle_version=1, mcp_lifecycle_version=1),
    )
    assert (
        app.settings.mcp_enabled is False
        and app._invocation_saved_values["mcp_enabled"] is True
    )
    assert not app.mcp.configs and not app.mcp.servers
    app._materialise_convo()
    saved = json.loads((app.convo_dir / "settings.json").read_text())
    saved["execution"]["mcp_enabled"] = True
    (app.convo_dir / "settings.json").write_text(json.dumps(saved), encoding="utf8")
    app._adopt_convo_settings(False)
    assert app.settings.mcp_enabled is False and not app.mcp.servers
    assert (
        json.loads((app.convo_dir / "settings.json").read_text())["execution"][
            "mcp_enabled"
        ]
        is True
    )
