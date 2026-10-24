"""Strata's run options in Settings (T0695).

Ryan 2026-10-24: "expose that [--vram-reserve-mib] and anything and everything strata exposes we
can edit ... no flags left then organize them like the rest of litetui settings our in the
collapseable sections". The catalog (strata_options.py) is one table; these tests pin the five
places it drives and the launch.
"""
from __future__ import annotations

import json
from dataclasses import fields

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Select

from litetui import settings_scope, strata_engine as eng, strata_options as so
from litetui.settings import Settings
from litetui.settings_screen import SettingsBody, SettingsScreen
from litetui.settings_ui_model import SETTINGS_SECTIONS, search_settings

# The real shape of Ryan's strata-uncensored_iq3_xxs.json, trimmed of paths that do not matter.
RYAN_ARGS = ["--pack", "P", "--native", "N", "--ple-gguf", "N", "--expert-profile", "E",
             "--expert-cache", "auto", "--prefill", "auto", "--spec", "4", "--spec-min-p", "0.5",
             "--mtp", "M", "--max-context", "150000", "--kv", "int8", "--vram-reserve-mib", "1082"]


def _cfg(**extra):
    return {"exe": "strata.exe", "args": list(RYAN_ARGS), "port": 8080, "gpu": 0, **extra}


def _settings(**values):
    return Settings(**values)


# ── the catalog is one table that every place agrees with ──────────────────────

def test_every_catalog_option_is_a_settings_field_of_the_right_type():
    by_name = {f.name: str(f.type) for f in fields(Settings)}
    for opt in so.OPTIONS:
        assert by_name[opt.field] == opt.type_name, opt.field
    assert by_name[so.EXTRA_FIELD] == "str | None"


def test_no_stray_strata_field_exists_outside_the_catalog():
    named = {"strata_host", "strata_root", "strata_config", "strata_max_context"}
    stray = {f.name for f in fields(Settings) if f.name.startswith("strata_")} - set(so.FIELD_NAMES) - named
    assert not stray


def test_option_keys_and_flags_are_unique():
    assert len({o.field for o in so.OPTIONS}) == len(so.OPTIONS)
    targets = [o.target[1:] for o in so.OPTIONS]
    assert len(set(targets)) == len(targets)


def test_options_are_device_settings_applied_at_restart():
    for name in so.FIELD_NAMES:
        spec = settings_scope.SETTING_SPECS[name]
        assert spec.scope is settings_scope.SettingScope.DEVICE, name
        assert spec.apply_timing == "restart", name
    settings_scope.validate_registry()


def test_every_option_has_a_label_a_help_line_and_a_collapsible_section():
    sections = {s.section_id: s for s in SETTINGS_SECTIONS if s.tab_id == "strata"}
    placed = {f.name: s for s in sections.values() for f in s.fields}
    for opt in so.OPTIONS:
        assert opt.label and opt.help, opt.field
        assert opt.field in placed, opt.field
        assert placed[opt.field].section_id == opt.section
    assert so.EXTRA_FIELD in placed
    for section in so.SECTIONS:
        assert not sections[section.section_id].default_expanded
        assert so.options_for(section.section_id) or section.section_id == "strata-extra"


def test_the_vram_reserve_is_found_by_search_under_memory():
    hits = [h for h in search_settings("vram") if h.section_id == "strata-memory"]
    assert hits and "strata_vram_reserve_mib" in hits[0].field_names


def test_the_vram_warning_quotes_the_measurement_from_his_log():
    text = so.BY_FIELD["strata_vram_reserve_mib"].full_help()
    assert "1082 MiB" in text and "11,547" in text and "480-783" in text


# ── applying to the launch config ───────────────────────────────────────────

def _sample_value(opt):
    if opt.kind == "switch":
        return "on"
    if opt.kind == "choice":
        return opt.choices[-1]
    if opt.field == "strata_kv_resident":
        return 20480
    if opt.kind == "int":
        return int(opt.lo or 0) + 1 if opt.hi is None or (opt.lo or 0) + 1 <= opt.hi else int(opt.hi)
    if opt.kind == "float":
        return 0.25 if (opt.lo or 0) <= 0.25 <= (opt.hi if opt.hi is not None else 1e9) else float(opt.lo or 0) + 0.25
    if opt.kind == "auto":
        return "123"
    return {"strata_gpu": "1", "strata_layer_split": "24,36", "strata_prefill": "4096"}.get(opt.field, "x")


@pytest.mark.parametrize("opt", so.OPTIONS, ids=lambda o: o.field)
def test_every_catalog_option_reaches_the_launch_config(opt):
    """The proof behind test_no_dead_controls' CATALOG_READ: each option, set alone, changes the config."""
    value = _sample_value(opt)
    settings = _settings(**{opt.field: value})
    so.validate(settings)
    cfg = _cfg(sampling={"temperature": 0.1})
    assert so.apply(cfg, settings) != cfg, opt.field


def test_the_extra_field_reaches_the_launch_config_too():
    cfg = _cfg()
    assert so.apply(cfg, _settings(strata_extra_args="--stage-timing")) != cfg


def test_nothing_set_returns_the_same_config_object():
    cfg = _cfg()
    assert so.apply(cfg, _settings()) is cfg


def test_a_reserve_replaces_the_one_in_the_config_and_leaves_the_original_alone():
    cfg = _cfg()
    new = so.apply(cfg, _settings(strata_vram_reserve_mib=2048))
    assert new["args"][-2:] == ["--vram-reserve-mib", "2048"]
    assert new["args"].count("--vram-reserve-mib") == 1
    assert cfg["args"] == RYAN_ARGS


def test_a_value_flag_missing_from_the_config_is_appended():
    new = so.apply(_cfg(), _settings(strata_suffix_draft=5, strata_prompt_cache=0))
    assert new["args"][-4:] == ["--suffix-draft", "5", "--prompt-cache", "0"]


def test_a_float_flag_and_an_auto_flag_are_written_as_text():
    new = so.apply(_cfg(), _settings(strata_spec_min_p=0.6, strata_expert_cache="4096", strata_rope_scale=4.0))
    args = new["args"]
    assert args[args.index("--spec-min-p") + 1] == "0.6"
    assert args[args.index("--expert-cache") + 1] == "4096"
    assert args[args.index("--rope-scale") + 1] == "4"


def test_a_presence_flag_goes_on_and_off():
    on = so.apply(_cfg(), _settings(strata_kv_grow="on"))
    assert "--kv-grow" in on["args"]
    cfg = _cfg()
    cfg["args"] += ["--kv-grow"]
    off = so.apply(cfg, _settings(strata_kv_grow="off"))
    assert "--kv-grow" not in off["args"]


def test_a_flag_pair_switches_the_one_that_is_present():
    cfg = _cfg()
    cfg["args"] += ["--no-coupled-draft"]
    on = so.apply(cfg, _settings(strata_coupled_draft="on"))
    assert "--coupled-draft" in on["args"] and "--no-coupled-draft" not in on["args"]
    off = so.apply(_cfg(), _settings(strata_coupled_draft="off"))
    assert "--no-coupled-draft" in off["args"] and "--coupled-draft" not in off["args"]


def test_server_keys_are_written_as_json_not_args():
    new = so.apply(_cfg(), _settings(
        strata_fit_max_tokens="on", strata_idle_unload_s=600.0, strata_anthropic_thinking="on_request",
        strata_reasoning_loop_recovery="off", strata_aliases="a, b", strata_gpu="1", strata_layer_split="24,36"))
    assert new["fit_max_tokens"] is True
    assert new["idle_unload_s"] == 600.0
    assert new["anthropic_thinking"] == "on_request"
    assert new["reasoning_loop_recovery"] is False
    assert new["aliases"] == ["a", "b"]
    assert new["gpu"] == 1 and new["layer_split"] == "24,36"
    assert new["args"] == RYAN_ARGS


def test_sampling_defaults_merge_into_the_sampling_block():
    cfg = _cfg(sampling={"temperature": 0.7})
    new = so.apply(cfg, _settings(strata_top_p=0.9))
    assert new["sampling"] == {"temperature": 0.7, "top_p": 0.9}
    assert cfg["sampling"] == {"temperature": 0.7}


def test_extra_arguments_replace_a_flag_and_append_new_ones():
    new = so.apply(_cfg(), _settings(strata_extra_args="--vram-reserve-mib 900 --stage-timing --pool-affinity p-cores"))
    args = new["args"]
    assert args.count("--vram-reserve-mib") == 1 and args[args.index("--vram-reserve-mib") + 1] == "900"
    assert "--stage-timing" in args
    assert args[args.index("--pool-affinity") + 1] == "p-cores"


def test_extra_arguments_keep_windows_paths_whole():
    new = so.apply(_cfg(), _settings(strata_extra_args='--expert-profile-save "C:\\Strata data\\p.bin"'))
    args = new["args"]
    assert args[args.index("--expert-profile-save") + 1] == "C:\\Strata data\\p.bin"


# ── checking ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("values, field", [
    ({"strata_vram_reserve_mib": -1}, "strata_vram_reserve_mib"),
    ({"strata_spec_min_p": 1.5}, "strata_spec_min_p"),
    ({"strata_kv": "fp8"}, "strata_kv"),
    ({"strata_kv_grow": "maybe"}, "strata_kv_grow"),
    ({"strata_expert_cache": "lots"}, "strata_expert_cache"),
    ({"strata_prefill": "big"}, "strata_prefill"),
    ({"strata_kv_resident": 4096}, "strata_kv_resident"),
    ({"strata_parallel": 9}, "strata_parallel"),
    ({"strata_gpu": "first"}, "strata_gpu"),
    ({"strata_extra_args": "stage-timing"}, "strata_extra_args"),
])
def test_a_value_that_cannot_be_launched_is_refused_by_name(values, field):
    with pytest.raises(ValueError, match=field):
        so.validate(_settings(**values))


def test_blank_and_good_values_pass():
    so.validate(_settings())
    so.validate(_settings(strata_vram_reserve_mib=2048, strata_prefill="auto:16384", strata_expert_cache="auto",
                          strata_kv_resident=0, strata_gpu="0,2", strata_extra_args="--stage-timing"))


# ── the launch ──────────────────────────────────────────────────────────────

def _install(tmp_path, monkeypatch):
    from test_strata_engine import _install
    monkeypatch.setattr(eng, "log_path", lambda: tmp_path / "logs" / "strata.log")
    settings, (path,) = _install(tmp_path, "iq3_xxs")
    return settings, path


def test_launch_writes_the_copy_with_the_option_and_leaves_the_install_file_alone(tmp_path, monkeypatch):
    settings, path = _install(tmp_path, monkeypatch)
    before = path.read_text(encoding="utf-8")
    argv, _root, cfg, _host = eng.launch_plan(Settings(
        strata_root=settings.strata_root, strata_vram_reserve_mib=2048, strata_kv="q4_0"))
    written = json.loads((tmp_path / "logs" / "strata-iq3_xxs.litetui.json").read_text(encoding="utf-8"))
    args = written["args"]
    assert args[args.index("--vram-reserve-mib") + 1] == "2048"
    assert args[args.index("--kv") + 1] == "q4_0"
    assert argv[argv.index("--config") + 1].endswith("strata-iq3_xxs.litetui.json")
    assert path.read_text(encoding="utf-8") == before


def test_launch_with_no_option_still_serves_the_install_file(tmp_path, monkeypatch):
    settings, path = _install(tmp_path, monkeypatch)
    argv, _root, _cfg, _host = eng.launch_plan(Settings(strata_root=settings.strata_root))
    assert argv[argv.index("--config") + 1] == str(path)


def test_launch_refuses_a_bad_option_in_plain_words(tmp_path, monkeypatch):
    settings, _path = _install(tmp_path, monkeypatch)
    with pytest.raises(eng.BackendError, match="strata_kv.*nothing was started"):
        eng.launch_plan(Settings(strata_root=settings.strata_root, strata_kv="fp8"))


def test_a_context_past_the_trained_one_needs_rope_scaling_set(tmp_path, monkeypatch):
    settings, _path = _install(tmp_path, monkeypatch)
    with pytest.raises(eng.BackendError, match="RoPE scaling"):
        eng.launch_plan(Settings(strata_root=settings.strata_root, strata_max_context=400000))
    argv, *_ = eng.launch_plan(Settings(
        strata_root=settings.strata_root, strata_max_context=400000, strata_rope_scaling="yarn", strata_rope_scale=1.5))
    written = json.loads((tmp_path / "logs" / "strata-iq3_xxs.litetui.json").read_text(encoding="utf-8"))
    assert "--rope-scaling" in written["args"] and "400000" in written["args"]


# ── the dialog ──────────────────────────────────────────────────────────────

class _Host(App):
    def __init__(self, settings):
        super().__init__()
        self._settings = settings

    def compose(self) -> ComposeResult:
        return []

    def on_mount(self):
        self.push_screen(SettingsScreen(self._settings))


@pytest.mark.asyncio
async def test_the_dialog_has_a_control_for_every_option_inside_its_section_header():
    app = _Host(Settings())
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        screen = app.screen
        for opt in so.OPTIONS:
            assert screen.query(f"#f-{opt.field}"), opt.field
        assert screen.query(f"#f-{so.EXTRA_FIELD}")
        for section in so.SECTIONS:
            assert screen.query(f"#set-section-{section.section_id}")


@pytest.mark.asyncio
async def test_typing_a_reserve_and_picking_options_saves_them_typed():
    app = _Host(Settings())
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        body.query_one("#f-strata_vram_reserve_mib", Input).value = "2048"
        body.query_one("#f-strata_spec_min_p", Input).value = "0.6"
        body.query_one("#f-strata_kv", Select).value = "q4_0"
        body.query_one("#f-strata_kv_grow", Select).value = "on"
        body.query_one(f"#f-{so.EXTRA_FIELD}", Input).value = "--stage-timing"
        out = body._collect()
    assert out.strata_vram_reserve_mib == 2048
    assert out.strata_spec_min_p == 0.6
    assert out.strata_kv == "q4_0"
    assert out.strata_kv_grow == "on"
    assert out.strata_extra_args == "--stage-timing"
    assert out.strata_expert_cache is None and out.strata_pool_workers is None


@pytest.mark.asyncio
async def test_a_bad_value_blocks_the_save_by_name():
    app = _Host(Settings())
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        body.query_one("#f-strata_expert_cache", Input).value = "lots"
        with pytest.raises(ValueError, match="strata_expert_cache"):
            body._collect()


@pytest.mark.asyncio
async def test_saved_options_come_back_into_the_controls():
    app = _Host(Settings(strata_vram_reserve_mib=1500, strata_kv_grow="off", strata_extra_args="--a 1"))
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#f-strata_vram_reserve_mib", Input).value == "1500"
        assert screen.query_one("#f-strata_kv_grow", Select).value == "off"
        assert screen.query_one(f"#f-{so.EXTRA_FIELD}", Input).value == "--a 1"
