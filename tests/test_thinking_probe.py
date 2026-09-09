"""T540 — thinking probe: classify model levels without network."""
from litetui.thinking_probe import (
    probe_levels,
    effective_levels,
    get_effective_levels,
    clear_cache,
    GRADED_LEVELS,
)


def _make_transport(reasoning_map: dict[str, str]):
    """Return a transport that maps reasoning_effort level -> reasoning_content."""
    import json

    def transport(url, body_bytes, timeout):
        payload = json.loads(body_bytes)
        level = payload.get("reasoning_effort", "omitted")
        reasoning = reasoning_map.get(level, "")
        return {
            "choices": [{"message": {"content": "PROBE", "reasoning_content": reasoning}}],
            "usage": {"completion_tokens": 10},
        }

    return transport


class TestProbe:
    def test_all_same_means_one_class(self):
        t = _make_transport({l: "same thought" for l in GRADED_LEVELS})
        classes = probe_levels("http://fake", "model-a", transport=t)
        assert len(classes) == 1

    def test_official_qwen_has_four_classes(self):
        t = _make_transport({
            "minimal": "thought A",
            "low": "thought A",
            "medium": "thought B",
            "high": "thought C",
            "xhigh": "thought C",
        })
        classes = probe_levels("http://fake", "official", transport=t)
        assert len(classes) == 3  # A, B, C

    def test_transport_failure_returns_empty(self):
        def boom(url, body, timeout):
            raise ConnectionRefusedError("nope")
        classes = probe_levels("http://fake", "model", transport=boom)
        assert classes == {}


class TestEffectiveLevels:
    def test_one_class_means_off_xhigh(self):
        classes = {"abc": ["minimal", "low", "medium", "high", "xhigh"]}
        assert effective_levels(classes) == ["off", "xhigh"]

    def test_multiple_classes_picks_representatives(self):
        classes = {
            "a": ["minimal", "low"],
            "b": ["medium"],
            "c": ["high", "xhigh"],
        }
        levels = effective_levels(classes)
        assert "off" in levels
        assert "low" in levels
        assert "medium" in levels
        assert "xhigh" in levels
        assert "minimal" not in levels
        assert "high" not in levels

    def test_empty_classes_returns_full_set(self):
        levels = effective_levels({})
        assert "off" in levels
        assert all(l in levels for l in GRADED_LEVELS)


class TestCache:
    def test_seed_model_bypasses_probe(self):
        clear_cache()
        levels = get_effective_levels(
            "http://fake", "qwen/qwen3.8-27b",
            seed_models=["qwen/qwen3.8-27b"],
        )
        assert "off" in levels
        assert all(l in levels for l in GRADED_LEVELS)

    def test_cache_hit(self):
        clear_cache()
        t = _make_transport({l: "same" for l in GRADED_LEVELS})
        levels1 = get_effective_levels("http://fake", "cached-model", transport=t)
        levels2 = get_effective_levels("http://fake", "cached-model", transport=t)
        assert levels1 == levels2 == ["off", "xhigh"]

    def test_clear_cache_forces_reprobe(self):
        clear_cache()
        call_count = [0]
        orig_t = _make_transport({l: "same" for l in GRADED_LEVELS})

        def counting_t(url, body, timeout):
            call_count[0] += 1
            return orig_t(url, body, timeout)

        get_effective_levels("http://fake", "reprobe-model", transport=counting_t)
        first_calls = call_count[0]
        clear_cache("reprobe-model")
        get_effective_levels("http://fake", "reprobe-model", transport=counting_t)
        assert call_count[0] > first_calls
