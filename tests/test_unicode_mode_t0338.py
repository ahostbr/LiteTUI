"""Independent true-Unicode oracles: never derive expected text from production regexes."""
import pytest
from _owned_seat import owned_app

from litetui import seat_prompts


@pytest.mark.parametrize("header", ["# POLYMATHIC MIYAMOTO — Worker Mode", "# POLYMATHIC GAMMA — Worker Mode"])
def test_single_true_em_dash_mode_header_is_not_a_pure_method(tmp_path, header):
    assert header.count("—") == 1  # literal U+2014, independent of implementation escapes
    assert header.count("# POLYMATHIC") == 1  # no second heading to rescue the negative test
    source = tmp_path / "single-wrapper.md"
    source.write_text(header + "\nMethod quotation and Who I Am follow", encoding="utf-8")
    with pytest.raises(ValueError, match="method.*scaffold|method.*pure"):
        seat_prompts.method_text(source)
    with pytest.raises(ValueError, match="method.*scaffold|method.*pure"):
        owned_app(cognitive_file=str(source))


@pytest.mark.parametrize("wire,expected", [("leader", "# OWNER ROLE — Owner Mode"),
                                            ("worker", "# BUILDER ROLE — Builder Mode"),
                                            ("reviewer", "# CRITIC ROLE — Critic Mode")])
def test_actual_emitted_modern_header_is_literal_unicode(wire, expected):
    from uuid import uuid4
    app = owned_app(spawn_identity=(str(uuid4()), "probe", wire))
    text = app._request_messages()[0]["content"]
    assert text.splitlines()[0] == expected
    assert "â€”" not in text and "ΓÇö" not in text
