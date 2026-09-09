"""T540/T542 — thinking probe: refusal fast path + classifier fallback."""
import json
import re

from litetui.thinking_probe import (
    probe_levels,
    probe_via_refusal,
    effective_levels,
    get_effective_levels,
    clear_cache,
    GRADED_LEVELS,
    _parse_supported,
    _interpret_refusal,
    _translate_native_to_wire,
)


# ── refusal parsing ────────────────────────────────────────────────────


class TestParseSupportedSettings:
    def test_official_qwen(self):
        msg = "Reasoning setting 'high' is not supported by model 'qwen/qwen3.8-27b'. Supported settings: 'off', 'low', 'medium', 'xhigh', 'on'."
        assert _parse_supported(msg) == ["off", "low", "medium", "xhigh", "on"]

    def test_minicpm(self):
        msg = "Reasoning setting 'high' is not supported by model 'minicpm5-2b'. Supported settings: 'off', 'on'."
        assert _parse_supported(msg) == ["off", "on"]

    def test_no_match(self):
        assert _parse_supported("some other error") is None


class TestInterpretRefusal:
    def test_200_is_partial(self):
        kind, levels = _interpret_refusal(200, {})
        assert kind == "partial"

    def test_invalid_value_reasoning_is_known(self):
        body = {"error": {
            "code": "invalid_value",
            "param": "reasoning",
            "message": "Reasoning setting 'high' is not supported. Supported settings: 'off', 'on'.",
        }}
        kind, levels = _interpret_refusal(400, body)
        assert kind == "known"
        assert levels == ["off", "on"]

    def test_invalid_enum_value_is_unknown(self):
        body = {"error": {
            "code": "invalid_enum_value",
            "param": "reasoning",
            "message": "Invalid enum value. Expected 'off' | 'low' | 'medium' | 'high' | 'xhigh' | 'on'",
        }}
        kind, levels = _interpret_refusal(400, body)
        assert kind == "unknown"

    def test_unrelated_400_is_unknown(self):
        body = {"error": {"code": "bad_request", "message": "whatever"}}
        kind, levels = _interpret_refusal(400, body)
        assert kind == "unknown"


class TestTranslateNativeToWire:
    def test_off_stays_off(self):
        assert _translate_native_to_wire(["off", "on"]) == ["off", "xhigh"]

    def test_full_set(self):
        native = ["off", "low", "medium", "xhigh", "on"]
        wire = _translate_native_to_wire(native)
        assert "off" in wire
        assert "low" in wire
        assert "medium" in wire
        assert "xhigh" in wire
        assert "on" not in wire


# ── refusal probe ──────────────────────────────────────────────────────


def _refusal_transport(response_map):
    """Return a transport(url, body, timeout) -> (status, body_dict)."""
    def transport(url, body_bytes, timeout):
        payload = json.loads(body_bytes)
        setting = payload.get("reasoning", "")
        if setting in response_map:
            return response_map[setting]
        return (200, {"choices": [{"message": {"content": "ok"}}]})
    return transport


class TestRefusalProbe:
    def test_authoritative_refusal(self):
        t = _refusal_transport({
            "high": (400, {"error": {
                "code": "invalid_value", "param": "reasoning",
                "message": "Supported settings: 'off', 'low', 'medium', 'xhigh', 'on'.",
            }}),
        })
        levels = probe_via_refusal("http://fake", "model", transport=t)
        assert levels is not None
        assert "off" in levels
        assert "low" in levels
        assert "xhigh" in levels

    def test_on_off_only(self):
        t = _refusal_transport({
            "high": (400, {"error": {
                "code": "invalid_value", "param": "reasoning",
                "message": "Supported settings: 'off', 'on'.",
            }}),
        })
        levels = probe_via_refusal("http://fake", "model", transport=t)
        assert levels == ["off", "xhigh"]

    def test_endpoint_enum_ignored(self):
        t = _refusal_transport({
            "high": (400, {"error": {
                "code": "invalid_enum_value", "param": "reasoning",
                "message": "Invalid enum value.",
            }}),
        })
        levels = probe_via_refusal("http://fake", "model", transport=t)
        assert levels is None

    def test_accepted_then_follow_up_refusal(self):
        t = _refusal_transport({
            "high": (200, {"choices": [{"message": {"content": "ok"}}]}),
            "low": (400, {"error": {
                "code": "invalid_value", "param": "reasoning",
                "message": "Supported settings: 'off', 'medium', 'xhigh', 'on'.",
            }}),
        })
        levels = probe_via_refusal("http://fake", "model", transport=t)
        assert levels is not None
        assert "medium" in levels

    def test_transport_error_returns_none(self):
        def boom(url, body, timeout):
            raise ConnectionRefusedError("nope")
        assert probe_via_refusal("http://fake", "model", transport=boom) is None


# ── classifier (existing) ─────────────────────────────────────────────


def _make_classifier_transport(reasoning_map: dict[str, str]):
    def transport(url, body_bytes, timeout):
        payload = json.loads(body_bytes)
        level = payload.get("reasoning_effort", "omitted")
        reasoning = reasoning_map.get(level, "")
        return {
            "choices": [{"message": {"content": "PROBE", "reasoning_content": reasoning}}],
            "usage": {"completion_tokens": 10},
        }
    return transport


class TestClassifier:
    def test_all_same_means_one_class(self):
        t = _make_classifier_transport({l: "same thought" for l in GRADED_LEVELS})
        classes = probe_levels("http://fake", "model-a", transport=t)
        assert len(classes) == 1

    def test_official_qwen_has_three_classes(self):
        t = _make_classifier_transport({
            "minimal": "thought A", "low": "thought A",
            "medium": "thought B",
            "high": "thought C", "xhigh": "thought C",
        })
        classes = probe_levels("http://fake", "official", transport=t)
        assert len(classes) == 3


class TestEffectiveLevels:
    def test_one_class_means_off_xhigh(self):
        assert effective_levels({"abc": list(GRADED_LEVELS)}) == ["off", "xhigh"]

    def test_empty_classes_returns_full_set(self):
        levels = effective_levels({})
        assert all(l in levels for l in GRADED_LEVELS)


# ── get_effective_levels (integration) ─────────────────────────────────


class TestGetEffective:
    def test_refusal_wins_over_classifier(self):
        clear_cache()
        refusal_t = _refusal_transport({
            "high": (400, {"error": {
                "code": "invalid_value", "param": "reasoning",
                "message": "Supported settings: 'off', 'on'.",
            }}),
        })
        classifier_t = _make_classifier_transport({l: "all same" for l in GRADED_LEVELS})
        levels = get_effective_levels(
            "http://fake", "test-refusal-wins",
            refusal_transport=refusal_t,
            transport=classifier_t,
        )
        assert levels == ["off", "xhigh"]

    def test_falls_back_to_classifier_when_refusal_fails(self):
        clear_cache()
        def boom(url, body, timeout):
            raise ConnectionRefusedError("nope")
        classifier_t = _make_classifier_transport({l: "all same" for l in GRADED_LEVELS})
        levels = get_effective_levels(
            "http://fake", "test-fallback",
            refusal_transport=boom,
            transport=classifier_t,
        )
        assert levels == ["off", "xhigh"]

    def test_seed_bypasses_probe(self):
        clear_cache()
        levels = get_effective_levels(
            "http://fake", "qwen/qwen3.8-27b",
            seed_models=["qwen/qwen3.8-27b"],
        )
        assert all(l in levels for l in GRADED_LEVELS)

    def test_cache_hit(self):
        clear_cache()
        refusal_t = _refusal_transport({
            "high": (400, {"error": {
                "code": "invalid_value", "param": "reasoning",
                "message": "Supported settings: 'off', 'on'.",
            }}),
        })
        levels1 = get_effective_levels("http://fake", "cache-test", refusal_transport=refusal_t)
        levels2 = get_effective_levels("http://fake", "cache-test", refusal_transport=refusal_t)
        assert levels1 == levels2
