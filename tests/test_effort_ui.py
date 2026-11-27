"""T1398: /think, the header and the settings control show the number each level sends.

Brief step 5: "/think and the settings control must show the number a preset sends. Take a screenshot
or SVG of each ... A green suite is not a look." The pilot arms save SVGs when LITETUI_T1398_SHOTS
names a folder; the assertions hold either way.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from _owned_seat import owned_app

from litetui.effort_scale import EffortScale, parse_effort_map
from litetui.picker import PickerScreen
from litetui.plugins import misc
from litetui.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "effort_templates"
INKLING = (FIXTURES / "inkling2.jinja").read_text(encoding="utf-8")
NAMES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _inkling(number_wire=False):
    return EffortScale(names=NAMES, numbers=parse_effort_map(INKLING), number_wire=number_wire,
                       source="template in Inkling-2-Mini.ninfer", template_read=True)


QWEN = EffortScale(names=("none", "low", "medium", "xhigh"), source="the served template", template_read=True)


class _Backend:
    """An engine that reads its template (the NInfer/llama.cpp shape). No network."""

    def __init__(self, scale, name="ninfer", label="NInfer"):
        self.name, self.label, self._scale = name, label, scale
        self.calls = 0

    def host(self):
        return "http://127.0.0.1:0"

    def effort_scale(self, model):
        self.calls += 1
        return self._scale

    def reasoning_levels(self, model=None):
        return ["none", "low", "medium", "xhigh"]


class _App:
    """Only what /think touches; the pilot arms below use a real LiteTUI."""

    def __init__(self, backend, scale=None, level="high"):
        self.backend, self.model_id, self.thinking_level = backend, "inkling", level
        self.settings = Settings()
        self._rpc = False
        self._effort_scales = {}
        self.messages: list[str] = []
        self.discovered = 0
        if scale is not None:
            self._effort_scales[self._key()] = scale

    def _key(self):
        return (self.backend.name, self.backend.host() if hasattr(self.backend, "host") else "", self.model_id)

    def _cached_effort_scale(self):
        return self._effort_scales.get(self._key())

    def _discover_effort_scale(self):
        self.discovered += 1

    def system_message(self, text):
        self.messages.append(text)

    def update_header(self):
        pass


# ── /think, decisions ───────────────────────────────────────────────────────────────────────────


def test_the_picker_offers_the_templates_levels_each_with_its_number():
    app = _App(_Backend(_inkling()), _inkling())
    labels = dict(misc._thinking_rows(app))
    assert labels["low"] == "low · 0.2"
    assert labels["high"] == "high · 0.9"
    assert labels["xhigh"] == "xhigh · 0.99"
    assert labels["off"] == "off · 0"
    assert "unset" in labels


def test_think_high_says_it_sends_high_and_the_template_reads_0_9():
    app = _App(_Backend(_inkling()), _inkling())
    misc._cmd_think(app, "think", "high")
    assert app.thinking_level == "high"
    assert app.messages[-1] == "Thinking level: high (sends reasoning_effort='high' -> template level 0.9)."


def test_think_0_1_on_ninfer_today_is_set_and_says_it_goes_as_minimal():
    app = _App(_Backend(_inkling()), _inkling())
    misc._cmd_think(app, "think", "0.1")
    assert app.thinking_level == 0.1
    assert app.messages[-1] == "0.1 is sent as 'minimal' (this model's template maps 'minimal' to 0.1)."


def test_think_0_1_where_numbers_travel_is_sent_as_a_number():
    app = _App(_Backend(_inkling(True), name="llamacpp", label="llama.cpp"), _inkling(True))
    misc._cmd_think(app, "think", "0.1")
    assert app.thinking_level == 0.1
    assert app.messages[-1] == "Thinking level: 0.1 (sent as a number to the template)."
    rows = dict(misc._thinking_rows(app))
    assert rows["0.1"] == "0.1 · minimal" and 0.1 not in rows, "T1430: 0.1 is a row of the grid, not a manual row"


def test_think_0_5_on_ninfer_today_is_refused_with_the_numbers_that_work_and_nothing_stored():
    app = _App(_Backend(_inkling()), _inkling())
    misc._cmd_think(app, "think", "0.5")
    assert app.thinking_level == "high"
    assert app.messages[-1] == ("0.5 not set: This server cannot send a number to the template yet. "
                                "Numbers that work here: 0, 0.1, 0.2, 0.7, 0.9, 0.99.")


def test_think_a_number_on_qwen_is_refused_because_its_template_has_none():
    app = _App(_Backend(QWEN), QWEN)
    misc._cmd_think(app, "think", "0.1")
    assert app.thinking_level == "high"
    assert "no effort numbers" in app.messages[-1]


def test_think_a_number_on_a_backend_with_no_template_is_refused_by_name():
    class Strata:
        name, label = "strata", "Strata"

        def reasoning_levels(self, model=None):
            return ["none", "low", "medium", "high"]
    app = _App(Strata())
    misc._cmd_think(app, "think", "0.1")
    assert app.thinking_level == "high"
    assert app.messages[-1].startswith("0.1 not set: Strata takes level names only")


def test_think_a_number_before_the_scale_is_read_asks_to_retry_and_starts_the_read():
    app = _App(_Backend(_inkling()))
    misc._cmd_think(app, "think", "0.1")
    assert app.thinking_level == "high" and app.discovered == 1
    assert "has not been read yet" in app.messages[-1]


def test_a_saved_name_the_model_lacks_says_where_it_lands():
    app = _App(_Backend(QWEN), QWEN)
    misc._cmd_think(app, "think", "xhigh")
    assert app.messages[-1] == "Thinking level: xhigh (sends reasoning_effort='xhigh')."
    app.thinking_level = "high"            # saved under another model
    from litetui.effort_scale import think_chip
    assert think_chip(app) == "think:high -> xhigh"


def test_set_thinking_takes_a_number_by_the_same_rule_as_think():
    from litetui.thinking_capabilities import set_thinking, thinking_capabilities

    app = _App(_Backend(_inkling()), _inkling())
    app._remember_for_this_convo = lambda *a: None
    set_thinking(app, 0.2)
    assert app.thinking_level == 0.2
    with pytest.raises(ValueError, match="cannot send a number"):
        set_thinking(app, 0.5)
    caps = thinking_capabilities(app)
    assert caps["levels"] == ["default", "off", "minimal", "low", "medium", "high", "xhigh", "max"]
    assert caps["numbers"]["high"] == 0.9 and caps["number_wire"] is False


def test_the_picker_has_a_number_row_only_where_the_template_maps_numbers():
    rows = dict(misc._thinking_rows(_App(_Backend(_inkling()), _inkling())))
    assert rows[misc.NUMBER_ROW] == "number…  · type one (This server sends only the template's own numbers: " \
                                    "0, 0.1, 0.2, 0.7, 0.9, 0.99.)"
    rows = dict(misc._thinking_rows(_App(_Backend(_inkling(True)), _inkling(True))))
    assert rows[misc.NUMBER_ROW] == "number…  · type one (Any number from 0 to 0.99.)"
    assert misc.NUMBER_ROW not in dict(misc._thinking_rows(_App(_Backend(QWEN), QWEN)))


def test_the_number_row_is_not_a_level_you_can_type():
    app = _App(_Backend(_inkling()), _inkling())
    misc._cmd_think(app, "think", misc.NUMBER_ROW)
    assert app.thinking_level == "high"
    unknown, valid = app.messages[-1].split("\n")
    assert unknown == f"Unknown level: {misc.NUMBER_ROW}"
    assert valid == "Valid: off, minimal, low, medium, high, xhigh, max, unset"


# ── the settings control ────────────────────────────────────────────────────────────────────────


def test_the_settings_select_rows_carry_the_numbers_and_a_manual_row():
    from litetui.settings_screen import thinking_choices

    rows = thinking_choices(_Backend(_inkling()), "inkling", "high", _inkling())
    assert ("high · 0.9", "high") in rows and ("off · 0", "off") in rows
    rows = thinking_choices(_Backend(_inkling()), "inkling", 0.1, _inkling())
    assert rows[-1] == ("manual · 0.1 (as minimal)", 0.1)
    rows = thinking_choices(_Backend(QWEN), "qwen", "high", QWEN)
    assert rows[-1] == ("high -> xhigh (saved; this model's template has no 'high')", "high")


# ── pilot: the real app, and the pictures ───────────────────────────────────────────────────────


def _shots() -> Path | None:
    where = os.environ.get("LITETUI_T1398_SHOTS")
    if not where:
        return None
    path = Path(where)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _real_app(scale, level="high"):
    a = owned_app()
    a._connect = lambda: None
    a.convo_dir = None
    a.skills = []
    a.backend = _Backend(scale)
    a.model_id = "inkling-2-mini"
    a.thinking_level = level
    a._effort_scales = {a._effort_scale_key(): scale}
    return a


@pytest.mark.asyncio
async def test_pilot_think_picker_shows_the_numbers():
    a = _real_app(_inkling())
    async with a.run_test(size=(120, 40)) as pilot:
        a.update_header()            # connect is stubbed; this is what it would have drawn
        misc._cmd_think(a, "think", "")
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen)
        if shots := _shots():
            a.save_screenshot(filename="think_picker_inkling_ninfer.svg", path=str(shots))
        assert a.sub_title.split(" · ")[0] == "NInfer"
        assert "think:high · 0.9" in a.sub_title


@pytest.mark.asyncio
async def test_pilot_think_number_and_header():
    a = _real_app(_inkling())
    async with a.run_test(size=(120, 40)) as pilot:
        misc._cmd_think(a, "think", "0.1")
        await pilot.pause()
        assert a.thinking_level == 0.1
        assert "think:0.1 (as minimal)" in a.sub_title
        misc._cmd_think(a, "think", "0.5")
        await pilot.pause()
        await pilot.pause()
        assert a.thinking_level == 0.1, "a refused number is not stored"
        screen = a.export_screenshot()
        assert "0.1&#160;is&#160;sent&#160;as" in screen or "0.1 is sent as" in screen
        assert "not&#160;set" in screen or "0.5 not set" in screen, "the refusal must be on screen"
        if shots := _shots():
            a.save_screenshot(filename="think_numbers_inkling_ninfer.svg", path=str(shots))


@pytest.mark.asyncio
async def test_pilot_think_picker_number_row_asks_refuses_then_sets():
    """Prefix 5c1db80f 1a: the picker's "number…" row asks for the value and goes through the /think
    rule; a refused number keeps the dialog open with the reason and stores nothing."""
    from textual.widgets import Input, OptionList, Static

    from litetui.number_choice import NumberScreen

    a = _real_app(_inkling())
    async with a.run_test(size=(120, 40)) as pilot:
        misc._cmd_think(a, "think", "")
        await pilot.pause()
        ol = a.screen.query_one(OptionList)
        ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
        ol.highlighted = ids.index(misc.NUMBER_ROW)
        if shots := _shots():
            a.save_screenshot(filename="think_picker_number_row.svg", path=str(shots))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(a.screen, NumberScreen)
        box = a.screen.query_one("#number-input", Input)
        box.value = "0.5"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(a.screen, NumberScreen), "a refused number keeps the dialog open"
        error = str(a.screen.query_one("#number-error", Static).render())
        assert error.startswith("0.5 not set: This server cannot send a number to the template yet.")
        assert a.thinking_level == "high"
        if shots := _shots():
            a.save_screenshot(filename="think_number_dialog_refused.svg", path=str(shots))
        box.value = "0.1"
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(a.screen, NumberScreen)
        assert a.thinking_level == 0.1
        assert "think:0.1 (as minimal)" in a.sub_title


async def _open_settings_at_thinking(a, pilot):
    from textual.widgets import Input

    from litetui.settings_screen import SettingsBody, SettingsScreen

    a.push_screen(SettingsScreen(a.settings))
    await pilot.pause()
    body = a.screen.query_one(SettingsBody)
    body.query_one("#set-search", Input).value = "Thinking level"
    await pilot.pause()
    await pilot.pause()
    # The search lands on its first hit (the Model tab's "Graded thinking works on"); the picture
    # must show THIS row, so open Generation > Reasoning and bring the row into view.
    from textual.widgets import TabbedContent

    body.query_one("#set-tabs", TabbedContent).active = "tab-generation"
    await pilot.pause()
    body.query_one("#f-thinking_level").scroll_visible(animate=False)
    await pilot.pause()
    await pilot.pause()
    return body


@pytest.mark.asyncio
async def test_pilot_settings_manual_number_saves_refuses_and_save_still_works():
    """Prefix 5c1db80f 1b: Settings takes a number for Thinking level through the /think rule; an
    invalid one is a named error and Save keeps working once it is fixed."""
    from textual.widgets import Input, Select, Static

    from litetui.settings_screen import MANUAL_THINKING, SettingsScreen

    a = _real_app(_inkling(True))               # a server that carries a number (llama.cpp's shape)
    a.settings.thinking_level = "high"
    async with a.run_test(size=(130, 46)) as pilot:
        body = await _open_settings_at_thinking(a, pilot)
        select = body.query_one("#f-thinking_level", Select)
        box = body.query_one("#f-thinking_level-number", Input)
        assert ("manual number…", MANUAL_THINKING) in [(str(p), v) for p, v in select._options]
        assert box.display is False, "the box shows only for a manual number"
        select.value = MANUAL_THINKING
        await pilot.pause()
        assert box.display is True

        box.value = "1.5"                       # outside Inkling's 0..0.99
        body.action_save()
        await pilot.pause()
        assert isinstance(a.screen, SettingsScreen), "a refused number does not save"
        error = str(body.query_one("#set-error", Static).render())
        assert "thinking_level: 1.5 not set: 1.5 is outside this model's scale (0 to 0.99)" in error
        if shots := _shots():
            a.save_screenshot(filename="settings_manual_number_refused.svg", path=str(shots))

        box.value = "0.1"
        assert body._collect().thinking_level == 0.1, "Save works again once the number is fixed"
        box.value = "abc"
        with pytest.raises(ValueError, match="thinking_level: abc is not a reasoning number"):
            body._collect()
        select.value = "low"                    # a preset wins over a stale box
        await pilot.pause()
        assert box.display is False and body._collect().thinking_level == "low"


@pytest.mark.asyncio
async def test_pilot_settings_stored_number_shows_as_manual_and_never_blocks_save():
    """A number saved under a model that maps numbers, opened on a backend with no scale (Strata,
    LM Studio): shown as "manual · 0.1 (not sent)", kept as is on Save; a NEW number is refused."""
    from textual.widgets import Input, Select

    class Strata:
        name, label = "strata", "Strata"

        def reasoning_levels(self, model=None):
            return ["none", "low", "medium", "high"]

    a = owned_app()
    a._connect = lambda: None
    a.convo_dir = None
    a.skills = []
    a.backend = Strata()
    a.model_id = "qwen"
    a.settings.thinking_level = 0.1
    async with a.run_test(size=(130, 46)) as pilot:
        body = await _open_settings_at_thinking(a, pilot)
        select = body.query_one("#f-thinking_level", Select)
        box = body.query_one("#f-thinking_level-number", Input)
        assert select.value == 0.1 and "manual · 0.1 (not sent)" in [str(p) for p, _ in select._options]
        assert box.display is True and box.value == "0.1"
        if shots := _shots():
            a.save_screenshot(filename="settings_manual_number_stored.svg", path=str(shots))
        assert body._collect().thinking_level == 0.1, "an unchanged stored number must not block Save"
        box.value = "0.2"
        with pytest.raises(ValueError, match="thinking_level: 0.2 not set: Strata takes level names only"):
            body._collect()


@pytest.mark.asyncio
async def test_pilot_settings_manual_number_shown_where_numbers_travel():
    from textual.widgets import Input, Select

    a = _real_app(_inkling(True))
    a.settings.thinking_level = 0.1
    async with a.run_test(size=(130, 46)) as pilot:
        body = await _open_settings_at_thinking(a, pilot)
        select = body.query_one("#f-thinking_level", Select)
        assert "manual · 0.1" in [str(p) for p, _ in select._options]
        assert body.query_one("#f-thinking_level-number", Input).value == "0.1"
        if shots := _shots():
            a.save_screenshot(filename="settings_manual_number_llama.svg", path=str(shots))


async def _open_modelcfg_inference(a, pilot):
    from textual.widgets import TabbedContent

    from litetui.plugins.model_switch import ModelConfigBody, ModelConfigScreen

    a.model_rows = {}
    a.push_screen(ModelConfigScreen(a.model_id))
    for _ in range(6):
        await pilot.pause()
    body = a.screen.query_one(ModelConfigBody)
    body.query_one("#mc-tabs", TabbedContent).active = "mc-infer"
    await pilot.pause()
    body.query_one("#inf-reasoning_effort").scroll_visible(animate=False)
    await pilot.pause()
    await pilot.pause()
    return body


@pytest.mark.asyncio
async def test_pilot_modelcfg_per_model_number_saves_refuses_and_keeps_a_stored_one():
    """Prefix 1610170e: /modelcfg's per-model override takes a manual number too, by the Settings
    helpers: a new number goes through the /think rule, a refusal keeps the panel open naming the
    field, and an unchanged stored number is kept."""
    from textual.widgets import Input, Select, Static

    from litetui.plugins.model_switch import ModelConfigBody
    from litetui.settings_screen import MANUAL_THINKING

    a = _real_app(_inkling())                   # NInfer today: only the template's own numbers go
    a.settings.model_infer_overrides = {}
    async with a.run_test(size=(130, 46)) as pilot:
        body = await _open_modelcfg_inference(a, pilot)
        select = body.query_one("#inf-reasoning_effort", Select)
        box = body.query_one("#inf-reasoning_effort-number", Input)
        labels = [str(p) for p, _ in select._options]
        assert "high · 0.9" in labels and "manual number…" in labels
        assert box.display is False
        select.value = MANUAL_THINKING
        await pilot.pause()
        assert box.display is True

        box.value = "0.5"
        body.action_apply()
        await pilot.pause()
        assert a.screen.query(ModelConfigBody), "a refused number keeps the panel open"
        error = str(body.query_one("#mc-error", Static).render())
        assert error.startswith("Cannot apply inkling-2-mini: reasoning_effort: 0.5 not set: "
                                "This server cannot send a number to the template yet.")
        assert "reasoning_effort" not in a.settings.model_infer_overrides.get("inkling-2-mini", {})
        if shots := _shots():
            box.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.pause()
            a.save_screenshot(filename="modelcfg_manual_number_refused.svg", path=str(shots))

        box.value = "0.1"                       # Inkling maps minimal to 0.1: sent as minimal
        body.action_apply()
        for _ in range(4):
            await pilot.pause()
        assert a.settings.model_infer_overrides["inkling-2-mini"]["reasoning_effort"] == 0.1

    # Reopened on a backend with no scale: shown as "manual · 0.1 (not sent)", kept on apply.
    class Strata:
        name, label = "strata", "Strata"

        def reasoning_levels(self, model=None):
            return ["none", "low", "medium", "high"]

        async def apply_load_settings(self, key, config, *, notice=None):
            pass
    b = owned_app()
    b._connect = lambda: None
    b.convo_dir = None
    b.skills = []
    b.backend = Strata()
    b.model_id = "inkling-2-mini"
    b.settings.model_infer_overrides = {"inkling-2-mini": {"reasoning_effort": 0.1}}
    async with b.run_test(size=(130, 46)) as pilot:
        body = await _open_modelcfg_inference(b, pilot)
        select = body.query_one("#inf-reasoning_effort", Select)
        box = body.query_one("#inf-reasoning_effort-number", Input)
        assert select.value == 0.1 and "manual · 0.1 (not sent)" in [str(p) for p, _ in select._options]
        assert box.display is True and box.value == "0.1"
        if shots := _shots():
            box.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.pause()
            b.save_screenshot(filename="modelcfg_manual_number_stored.svg", path=str(shots))
        body.action_apply()
        for _ in range(4):
            await pilot.pause()
        assert not b.screen.query(ModelConfigBody), \
            f"the apply was refused: {body.query_one('#mc-error', Static).render()}"
        assert b.settings.model_infer_overrides["inkling-2-mini"]["reasoning_effort"] == 0.1, \
            "an unchanged stored number must survive an apply on a backend with no scale"


@pytest.mark.asyncio
async def test_pilot_settings_thinking_select_shows_the_numbers():
    from textual.widgets import Input, Select

    from litetui.settings_screen import SettingsBody, SettingsScreen

    a = _real_app(_inkling(True))
    a.settings.thinking_level = "high"
    async with a.run_test(size=(130, 46)) as pilot:
        a.push_screen(SettingsScreen(a.settings))
        await pilot.pause()
        body = a.screen.query_one(SettingsBody)
        search = body.query_one("#set-search", Input)
        search.value = "Thinking level"
        await pilot.pause()
        await pilot.pause()
        select = body.query_one("#f-thinking_level", Select)
        labels = [str(prompt) for prompt, _value in select._options]
        assert "high · 0.9" in labels and "low · 0.2" in labels and "off · 0" in labels
        select.focus()
        await pilot.press("enter")
        await pilot.pause()
        if shots := _shots():
            a.save_screenshot(filename="settings_thinking_select_inkling.svg", path=str(shots))


# ── T1398-B: Inkling on Basalt (Ryan 2026-11-26: "litetui doesnt display the "0.5" value") ────────


def _inkling_basalt():
    return EffortScale(names=NAMES, numbers=parse_effort_map(INKLING), number_wire=True, number_step=0.01,
                       source="template in Inkling-2-Mini-NVFP4-MX8.basalt", template_read=True)


def test_think_0_5_on_basalt_is_set_shown_as_0_5_and_sent_as_50():
    from litetui.effort_scale import think_chip

    app = _App(_Backend(_inkling_basalt(), name="basalt", label="Basalt"), _inkling_basalt())
    misc._cmd_think(app, "think", "0.5")
    assert app.thinking_level == 0.5
    assert think_chip(app) == "think:0.5"
    assert app.messages[-1] == ("Thinking level: 0.5 (sends reasoning_effort=50: this server counts a number "
                                "in steps of 0.01).")
    rows = dict(misc._thinking_rows(app))
    assert rows["0.5"] == "0.5" and 0.5 not in rows, "T1430: 0.5 is a row of the grid, not a manual row"
    assert rows["0.9"] == "0.9 · high" and rows["0.1"] == "0.1 · minimal"
    assert rows[misc.NUMBER_ROW] == "number…  · type one (Any number from 0 to 0.99, in steps of 0.01.)"


def test_think_a_number_between_basalts_steps_is_refused_and_nothing_stored():
    app = _App(_Backend(_inkling_basalt(), name="basalt", label="Basalt"), _inkling_basalt())
    misc._cmd_think(app, "think", "0.555")
    assert app.thinking_level == "high"
    assert app.messages[-1] == ("0.555 not set: 0.555 is between this server's steps: it carries a number "
                                "in steps of 0.01.")


@pytest.mark.asyncio
async def test_pilot_basalt_think_0_5_shows_0_5_in_the_header_and_the_picker():
    """The look Ryan asked for: on Basalt's Inkling the header reads think:0.5 and every level shows its number."""
    a = _real_app(_inkling_basalt())
    a.backend = _Backend(_inkling_basalt(), name="basalt", label="Basalt")
    a._effort_scales = {a._effort_scale_key(): _inkling_basalt()}
    async with a.run_test(size=(120, 40)) as pilot:
        misc._cmd_think(a, "think", "0.5")
        await pilot.pause()
        assert a.thinking_level == 0.5
        assert a.sub_title.split(" · ")[0] == "Basalt"
        assert "think:0.5" in a.sub_title and "think:0.5 (" not in a.sub_title
        if shots := _shots():
            a.save_screenshot(filename="think_header_inkling_basalt.svg", path=str(shots))
        misc._cmd_think(a, "think", "")
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen)
        screen = a.export_screenshot()
        for label in ("0.9 · high", "0.1 · minimal", "0.5"):
            assert label.replace(" ", "&#160;") in screen or label in screen, f"{label!r} must be on screen"
        if shots := _shots():
            a.save_screenshot(filename="think_picker_inkling_basalt.svg", path=str(shots))


# ── T1430: the number grid (Ryan 2026-11-26: "still not showing me the the fine thinking numbers the
# 0.1-0.9 range") ────────────────────────────────────────────────────────────────────────────────


def _basalt_app(level="high"):
    return _App(_Backend(_inkling_basalt(), name="basalt", label="Basalt"), _inkling_basalt(), level=level)


GRID = ["0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9", "0.99"]


def test_basalt_inkling_picker_offers_off_then_every_tenth_then_0_99_each_naming_its_preset():
    rows = misc._thinking_rows(_basalt_app())
    ids = [level for level, _ in rows]
    assert ids == ["off", *GRID, misc.NUMBER_ROW, "unset"]
    labels = dict(rows)
    assert labels["off"] == "0 · off"
    assert labels["0.1"] == "0.1 · minimal" and labels["0.2"] == "0.2 · low"
    assert labels["0.7"] == "0.7 · medium" and labels["0.9"] == "0.9 · high"
    assert labels["0.99"] == "0.99 · xhigh, max"
    assert [labels[n] for n in ("0.3", "0.4", "0.5", "0.6", "0.8")] == ["0.3", "0.4", "0.5", "0.6", "0.8"]


@pytest.mark.parametrize("row,count", [(n, round(float(n) * 100)) for n in GRID])
def test_picking_a_grid_row_stores_that_number_shows_it_and_sends_its_hundredths(row, count):
    from litetui.effort_scale import effort_body, think_chip

    app = _basalt_app()
    misc._on_thinking_picked(app, row)
    assert app.thinking_level == float(row)
    assert think_chip(app) == f"think:{row}"
    assert app.messages[-1] == (f"Thinking level: {row} (sends reasoning_effort={count}: this server counts a "
                                "number in steps of 0.01).")
    assert effort_body(_inkling_basalt(), app.thinking_level) == {"reasoning_effort": count}


def test_picking_off_on_the_grid_sends_none_through_the_template():
    from litetui.effort_scale import effort_body, think_chip

    app = _basalt_app()
    misc._on_thinking_picked(app, "off")
    assert app.thinking_level == "off" and think_chip(app) == "think:off · 0"
    assert effort_body(_inkling_basalt(), "off") == {"chat_template_kwargs": {"reasoning_effort": "none"}}


def test_the_grid_picker_opens_on_the_number_a_saved_preset_sends():
    assert misc._picker_current(_basalt_app("max")) == "0.99"
    assert misc._picker_current(_basalt_app("medium")) == "0.7"
    assert misc._picker_current(_basalt_app(0.4)) == "0.4"
    assert misc._picker_current(_basalt_app("off")) == "off"
    assert misc._picker_current(_basalt_app(None)) == "unset"


def test_a_number_off_the_grid_keeps_its_manual_row_and_preset_names_stay_typeable():
    app = _basalt_app(0.55)
    assert dict(misc._thinking_rows(app))[0.55] == "manual · 0.55"
    misc._cmd_think(app, "think", "high")
    assert app.thinking_level == "high"
    assert app.messages[-1] == "Thinking level: high (sends reasoning_effort='high' -> template level 0.9)."


def test_models_without_a_fractional_number_wire_keep_their_named_levels():
    """Brief: "Other models keep their named levels exactly as today"."""
    ninfer_today = [level for level, _ in misc._thinking_rows(_App(_Backend(_inkling()), _inkling()))]
    assert ninfer_today == ["off", "minimal", "low", "medium", "high", "xhigh", "max", misc.NUMBER_ROW, "unset"]
    assert [level for level, _ in misc._thinking_rows(_App(_Backend(QWEN), QWEN))] == \
        ["off", "low", "medium", "xhigh", "unset"]
    deepseek = EffortScale(names=("low", "high", "max"), numbers={"low": 50, "high": 75, "max": 100},
                           number_wire=True, template_read=True)
    assert [level for level, _ in misc._thinking_rows(_App(_Backend(deepseek), deepseek))][:3] == \
        ["low", "high", "max"]


def test_the_scale_is_read_with_no_served_model():
    """The snip: Basalt with nothing serving had no model_id, so the pack's template was never read and
    the header said think:max. Now the reader is asked with None (Basalt: the chosen pack)."""
    a = owned_app()
    a.backend = _Backend(_inkling_basalt(), name="basalt", label="Basalt")
    asked = []
    a.backend.effort_scale = lambda model: asked.append(model) or _inkling_basalt()
    a.model_id = ""
    a.thinking_level = "max"
    assert a._effort_scale_now() is not None and asked == [None]
    from litetui.effort_scale import think_chip
    assert think_chip(a) == "think:max · 0.99"
    a._effort_scale_now()
    assert asked == [None], "cached: read once"
    a._effort_scale_now(fresh=True)
    assert asked == [None, None], "fresh (a new pack was chosen) reads again"


@pytest.mark.asyncio
async def test_pilot_basalt_grid_picker_picture():
    """The headless picture of the selector (LITETUI_T1398_SHOTS names the folder)."""
    a = _real_app(_inkling_basalt(), level=0.4)
    a.backend = _Backend(_inkling_basalt(), name="basalt", label="Basalt")
    a._effort_scales = {a._effort_scale_key(): _inkling_basalt()}
    async with a.run_test(size=(120, 40)) as pilot:
        a.update_header()
        await pilot.pause()
        assert "think:0.4" in a.sub_title and "think:0.4 (" not in a.sub_title
        misc._cmd_think(a, "think", "")
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen)
        screen = a.export_screenshot()
        for label in ("0 · off", "0.3", "0.4", "0.7 · medium", "0.99 · xhigh, max"):
            assert label.replace(" ", "&#160;") in screen or label in screen, f"{label!r} must be on screen"
        if shots := _shots():
            a.save_screenshot(filename="t1430_think_grid_inkling_basalt.svg", path=str(shots))
