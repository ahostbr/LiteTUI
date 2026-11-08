"""NInfer speed presets (Ryan 2026-11-08: "add this to settings default to fastest, add these speed examples and tierd preset options in a dropdown").

A DeepSeek launch recipe streams experts from disk; two of its flags decide the speed: how many of the 7,680 experts stay in pinned RAM
(--expert-host-mib) and --prefill-chunk. The bench of 2026-11-08 (C:/Projects/.scratch/T1192/bench-*) measured three tiers; the setting picks one.
"""
from pathlib import Path

from litetui import ninfer_engine as ne
from litetui.settings import Settings

LIVE = ["--ngram-table", "G:/t.ninfer", "--expert-residency", "disk", "--expert-host-mib", "46080", "--host-kv-mib", "1024",
        "--spec", "dspark", "--draft-confidence", "0.7"]


def value(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def test_the_default_is_fastest():
    assert Settings().ninfer_speed_preset == "fastest" == ne.SPEED_PRESET_DEFAULT


def test_every_dropdown_choice_is_a_preset_with_a_label():
    assert list(ne.SPEED_PRESETS) == ["fastest", "fast", "standard", "recipe"]
    assert all(p.label for p in ne.SPEED_PRESETS.values())


def test_fastest_keeps_every_expert_in_ram_and_chunks_4096():
    out, note = ne.apply_speed_preset(LIVE, "fastest", free_ram_gib=80)
    assert value(out, "--expert-host-mib") == "63488" and value(out, "--prefill-chunk") == "4096"
    assert value(out, "--host-kv-mib") == "1024", "the recipe's other flags are untouched"
    assert "fastest" in note


def test_fast_and_standard():
    fast, _ = ne.apply_speed_preset(LIVE, "fast", free_ram_gib=80)
    assert value(fast, "--expert-host-mib") == "46080" and value(fast, "--prefill-chunk") == "4096"
    standard, _ = ne.apply_speed_preset(LIVE + ["--prefill-chunk", "4096"], "standard", free_ram_gib=80)
    assert value(standard, "--expert-host-mib") == "46080" and "--prefill-chunk" not in standard, "standard is the engine's own chunk"


def test_recipe_leaves_the_flags_as_written():
    out, _ = ne.apply_speed_preset(LIVE, "recipe")
    assert out == LIVE


def test_a_tier_whose_ram_is_not_free_falls_back_one_tier_at_a_time():
    out, note = ne.apply_speed_preset(LIVE, "fastest", free_ram_gib=60)
    assert value(out, "--expert-host-mib") == "46080" and value(out, "--prefill-chunk") == "4096" and "fast" in note and "70 GiB" in note
    out, _ = ne.apply_speed_preset(LIVE, "fastest", free_ram_gib=40)
    assert value(out, "--expert-host-mib") == "46080" and "--prefill-chunk" not in out


def test_unknown_ram_does_not_block_the_chosen_tier():
    out, _ = ne.apply_speed_preset(LIVE, "fastest", free_ram_gib=None)
    assert value(out, "--expert-host-mib") == "63488"


def test_a_recipe_without_a_disk_expert_tier_is_not_touched():
    qwen = ["--spec", "mtp", "--kv-dtype", "fp8"]
    assert ne.apply_speed_preset(qwen, "fastest", free_ram_gib=80)[0] == qwen
    host = ["--expert-residency", "host", "--host-kv-mib", "1024"]
    assert ne.apply_speed_preset(host, "fastest", free_ram_gib=80)[0] == host


def test_an_unknown_preset_keeps_the_recipe():
    out, note = ne.apply_speed_preset(LIVE, "turbo", free_ram_gib=80)
    assert out == LIVE and "unknown" in note


def test_build_recipe_args_applies_the_preset_and_reports_it():
    recipe = ne.LaunchRecipe(path=Path("x.launch.json"), executable=Path("ninfer-serve.exe"), model_id="m", max_context=131072, args=tuple(LIVE))
    notes = []
    args = ne.build_recipe_args(Path("a.ninfer"), 8151, "m", recipe, speed_preset="fast", free_ram_gib=80, on_note=notes.append)
    assert value(args, "--prefill-chunk") == "4096" and value(args, "--max-context") == "131072"
    assert notes and "fast" in notes[0]
    plain = ne.build_recipe_args(Path("a.ninfer"), 8151, "m", recipe, speed_preset="recipe")
    assert plain[plain.index("--max-context") + 2:] == LIVE


# ── the two opt-in cache flags (Ryan 2026-11-08: "add those flags to settings default them to off") ──────────────────

def test_the_cache_flags_default_off_and_add_nothing():
    s = Settings()
    assert s.ninfer_auto_long_anchors is False and s.ninfer_derive_session_keys is False
    assert ne.cache_flags(s) == []


def test_each_cache_flag_is_added_only_when_switched_on():
    s = Settings()
    s.ninfer_auto_long_anchors = True
    assert ne.cache_flags(s) == ["--auto-long-anchors"]
    s.ninfer_derive_session_keys = True
    assert ne.cache_flags(s) == ["--auto-long-anchors", "--derive-session-keys"]
    s.ninfer_auto_long_anchors = False
    assert ne.cache_flags(s) == ["--derive-session-keys"]


def test_a_truthy_non_bool_does_not_switch_a_flag_on():
    class Fake:
        ninfer_auto_long_anchors = "yes"
        ninfer_derive_session_keys = 1
    assert ne.cache_flags(Fake()) == []
