"""T539 — reasoning_effort collapse: lmstudio=binary, llamacpp=verbatim."""

from litetui.turn_engine import TurnEngine, _resolve_reasoning_effort


class TestResolve:
    def test_llamacpp_off_sends_none(self):
        assert _resolve_reasoning_effort("off", "llamacpp") == "none"

    def test_llamacpp_graded_sends_verbatim(self):
        assert _resolve_reasoning_effort("xhigh", "llamacpp") == "xhigh"
        assert _resolve_reasoning_effort("low", "llamacpp") == "low"
        assert _resolve_reasoning_effort("medium", "llamacpp") == "medium"

    def test_lmstudio_off_sends_none(self):
        assert _resolve_reasoning_effort("off", "lmstudio") == "none"

    def test_lmstudio_graded_omits(self):
        for level in ("minimal", "low", "medium", "high", "xhigh"):
            assert _resolve_reasoning_effort(level, "lmstudio") is None, f"{level} should be omitted on lmstudio"

    def test_none_level_omits(self):
        assert _resolve_reasoning_effort(None, "llamacpp") is None
        assert _resolve_reasoning_effort(None, "lmstudio") is None

    def test_empty_level_omits(self):
        assert _resolve_reasoning_effort("", "llamacpp") is None


class TestChatRequest:
    def test_lmstudio_xhigh_omits_reasoning_effort(self):
        kwargs = TurnEngine.chat_request(
            model_id="test",
            messages=[{"role": "user", "content": "hi"}],
            tools_enabled=False,
            max_tokens_tools=4096,
            max_tokens_chat=4096,
            request_overrides={},
            thinking_level="xhigh",
            backend_name="lmstudio",
        )
        extra = kwargs.get("extra_body", {})
        assert "reasoning_effort" not in extra

    def test_llamacpp_xhigh_sends_verbatim(self):
        kwargs = TurnEngine.chat_request(
            model_id="test",
            messages=[{"role": "user", "content": "hi"}],
            tools_enabled=False,
            max_tokens_tools=4096,
            max_tokens_chat=4096,
            request_overrides={},
            thinking_level="xhigh",
            backend_name="llamacpp",
        )
        assert kwargs["extra_body"]["reasoning_effort"] == "xhigh"

    def test_lmstudio_off_sends_none(self):
        kwargs = TurnEngine.chat_request(
            model_id="test",
            messages=[{"role": "user", "content": "hi"}],
            tools_enabled=False,
            max_tokens_tools=4096,
            max_tokens_chat=4096,
            request_overrides={},
            thinking_level="off",
            backend_name="lmstudio",
        )
        assert kwargs["extra_body"]["reasoning_effort"] == "none"


class TestCompactRequest:
    def test_lmstudio_low_omits(self):
        kwargs = TurnEngine.compact_request(
            model_id="test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096,
            thinking_level="low",
            tools_enabled=False,
            backend_name="lmstudio",
        )
        extra = kwargs.get("extra_body", {})
        assert "reasoning_effort" not in extra

    def test_llamacpp_low_sends(self):
        kwargs = TurnEngine.compact_request(
            model_id="test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096,
            thinking_level="low",
            tools_enabled=False,
            backend_name="llamacpp",
        )
        assert kwargs["extra_body"]["reasoning_effort"] == "low"


class TestOfficialModelCarveOut:
    """T539-A: the collapse is right for LM Studio in general and WRONG for the
    official build, where the graded levels demonstrably work (measured
    2026-09-08: four distinct behaviours vs byte-identical on the NVFP4 quant).
    """

    OFFICIAL = ["qwen/qwen3.8-27b"]

    def test_allowlisted_model_keeps_the_graded_level(self):
        for lvl in ("minimal", "low", "medium", "high", "xhigh"):
            assert TurnEngine.resolve_reasoning_effort(
                lvl, "lmstudio", "qwen/qwen3.8-27b", self.OFFICIAL
            ) == lvl

    def test_other_lmstudio_models_still_collapse(self):
        for lvl in ("minimal", "low", "medium", "high", "xhigh"):
            assert TurnEngine.resolve_reasoning_effort(
                lvl, "lmstudio", "qwen3.8-27b-nvfp4-mtp", self.OFFICIAL
            ) is None

    def test_match_is_exact_not_substring(self):
        # "qwen3.8-27b-nvfp4-mtp" CONTAINS "qwen3.8-27b" -- a substring rule
        # would allowlist the one model measured to drop every graded level.
        assert TurnEngine.resolve_reasoning_effort(
            "xhigh", "lmstudio", "qwen3.8-27b-nvfp4-mtp", ["qwen3.8-27b"]
        ) is None

    def test_match_is_case_insensitive(self):
        assert TurnEngine.resolve_reasoning_effort(
            "medium", "lmstudio", "QWEN/Qwen3.8-27B", self.OFFICIAL
        ) == "medium"

    def test_off_is_still_none_on_the_allowlisted_model(self):
        # `none` is honoured there too (measured 0 reasoning chars), and the
        # carve-out must not turn "off" into a graded level.
        assert TurnEngine.resolve_reasoning_effort(
            "off", "lmstudio", "qwen/qwen3.8-27b", self.OFFICIAL
        ) == "none"

    def test_empty_allowlist_is_the_shipped_collapse(self):
        assert TurnEngine.resolve_reasoning_effort(
            "xhigh", "lmstudio", "qwen/qwen3.8-27b", []
        ) is None

    def test_llamacpp_ignores_the_allowlist_entirely(self):
        assert TurnEngine.resolve_reasoning_effort(
            "xhigh", "llamacpp", "anything-at-all", []
        ) == "xhigh"
