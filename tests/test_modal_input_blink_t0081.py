"""T0081 headless framework contract, NOT running-product visual proof.

No LiteTUI construction, service imports, fleet, focus or driver actions.
--noconftest avoids the broad application fixtures; sync tests use asyncio.run.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from litetui.steady_input import HostedInput


def actual_host_predicate():
    """Compile the verbatim staticmethod without importing application services."""
    tree = ast.parse((ROOT / "src/litetui/app.py").read_text(encoding="utf-8"))
    app_class = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "LiteTUI")
    predicate = next(node for node in app_class.body
                     if isinstance(node, ast.FunctionDef) and node.name == "_hosted_seat")
    namespace = {"os": os}
    exec(compile(ast.Module(body=[predicate], type_ignores=[]),  # noqa: S102 - compiles shipped artifact own AST verbatim, never user input
                 "app.py:_hosted_seat", "exec"), namespace)
    return namespace["_hosted_seat"]


@pytest.mark.parametrize("host_variable", [
    None, "LITESUITE_CANVAS_AGENT", "LITEHARNESS_TIER",
    "LITEHARNESS_AGENT_NAME", "LITEHARNESS_SPAWNED_BY",
])
def test_real_mount_applies_actual_host_policy(monkeypatch, host_variable):
    for name in ("LITESUITE_CANVAS_AGENT", "LITEHARNESS_TIER",
                 "LITEHARNESS_AGENT_NAME", "LITEHARNESS_SPAWNED_BY"):
        monkeypatch.delenv(name, raising=False)
    if host_variable:
        monkeypatch.setenv(host_variable, "1")

    class InputHost(App):
        AUTO_FOCUS = None
        _hosted_seat = actual_host_predicate()

        def compose(self) -> ComposeResult:
            yield HostedInput(value="draft", placeholder="setting", id="setting-input")
            yield Input(value="stock", id="stock-input")

    async def check():
        app = InputHost()
        async with app.run_test(headless=True):
            field = app.query_one("#setting-input", Input)
            stock = app.query_one("#stock-input", Input)
            hosted = bool(host_variable)
            assert field.is_mounted
            assert field.cursor_blink is not hosted
            assert field.value == "draft" and field.placeholder == "setting"
            # The inherited Input mount actually created its real timer. With
            # autofocus disabled, this test never requests a focus transition.
            assert field._blink_timer is not None
            assert field._blink_timer._task is not None
            if hosted:
                assert not field._blink_timer._active.is_set()
                assert field._cursor_visible
            else:
                assert field.cursor_blink == stock.cursor_blink
                assert field._blink_timer._active.is_set() == stock._blink_timer._active.is_set()
            # Let one normal timer interval pass: hosted input remains steady.
            await asyncio.sleep(0.6)
            if hosted:
                assert not field._blink_timer._active.is_set()
                assert field._cursor_visible

    asyncio.run(check())


@pytest.mark.parametrize("hosted_seat", [None, False])
def test_non_litetui_hosts_keep_stock_defaults(monkeypatch, hosted_seat):
    app = (SimpleNamespace() if hosted_seat is None
           else SimpleNamespace(_hosted_seat=hosted_seat))
    monkeypatch.setattr(HostedInput, "app", property(lambda self: app))
    field = HostedInput()
    field.on_mount()
    assert field.cursor_blink == Input().cursor_blink


def test_plain_terminal_preserves_explicit_blink_choice(monkeypatch):
    app = SimpleNamespace(_hosted_seat=lambda: False)
    monkeypatch.setattr(HostedInput, "app", property(lambda self: app))
    field = HostedInput()
    field.cursor_blink = False
    field.on_mount()
    assert not field.cursor_blink


def test_stock_input_events_and_validation_are_preserved():
    from textual.validation import Integer

    class InputHost(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield HostedInput(value="12", validators=Integer(minimum=1),
                              type="integer", id="validated-input")

    async def check():
        app = InputHost()
        async with app.run_test(headless=True):
            field = app.query_one("#validated-input", Input)
            assert isinstance(field, Input)
            assert HostedInput.Changed is Input.Changed
            assert HostedInput.Submitted is Input.Submitted
            assert HostedInput.Blurred is Input.Blurred
            assert Input.Changed(field, "12").input is field
            assert field.validate("12").is_valid
            assert not field.validate("not a number").is_valid
            assert not hasattr(field, "push_history")

    asyncio.run(check())


# Supplementary wiring coverage, not a substitute for the real mount above.
@pytest.mark.parametrize("relative,count", [
    ("settings_screen.py", 6),
    ("user_name_dialog.py", 1),
    ("ask_user_question.py", 1),
    ("hooks_screen.py", 1),
    ("mcp_list.py", 2),
    ("colorpicker.py", 1),
    ("ticker.py", 1),
    ("plugins/model_switch.py", 4),
    ("plugins/scheduler_ui.py", 3),
])
def test_production_constructor_wiring(relative, count):
    tree = ast.parse((ROOT / "src/litetui" / relative).read_text(encoding="utf-8"))
    constructors = [node.func.id for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("Input", "HostedInput")]
    assert constructors == ["HostedInput"] * count
    assert any(isinstance(node, ast.ImportFrom)
               and node.module == "litetui.steady_input"
               and any(alias.name == "HostedInput" for alias in node.names)
               for node in tree.body)
