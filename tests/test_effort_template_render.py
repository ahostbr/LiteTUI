"""T1398: Inkling's REAL chat template, rendered with jinja2, for exactly what LiteTUI sends.

Brief step 4: "render Inkling's real chat_template.jinja with jinja2 for each preset and the manual
number, and assert the line 'Thinking effort level: 0.2' / '0.1' / '0.9'".

jinja2 is not in the app's .venv (adding it is a download), so this file skips there and is run with
the system interpreter, which has it:
    C:/Users/Ryan/AppData/Local/Programs/Python/Python311/python.exe -m pytest --noconftest
        tests/test_effort_template_render.py
It imports nothing from litetui: the values below are what tests/test_effort_wire_capture.py
captured LiteTUI sending, and each is rendered through the real template file.
"""
from pathlib import Path

import pytest

jinja2 = pytest.importorskip("jinja2")
import jinja2.ext  # noqa: E402
import jinja2.nodes  # noqa: E402

TEMPLATE = (Path(__file__).parent / "fixtures" / "effort_templates" / "inkling2.jinja").read_text(encoding="utf-8")


class _Refused(Exception):
    """The template's own raise_exception, kept apart from jinja2's errors so a syntax error can
    never pass as a refusal (it did, before the extension below)."""


def _raise(message):
    raise _Refused(message)


class _Generation(jinja2.ext.Extension if hasattr(jinja2, "ext") else object):
    """Hugging Face's `{% generation %}...{% endgeneration %}` (transformers' AssistantTracker), which
    the template uses around assistant turns: renders its body unchanged, as transformers does."""
    tags = {"generation"}

    def parse(self, parser):
        lineno = next(parser.stream).lineno
        body = parser.parse_statements(("name:endgeneration",), drop_needle=True)
        return jinja2.nodes.CallBlock(self.call_method("_body", []), [], [], body).set_lineno(lineno)

    def _body(self, caller):
        return caller()


def render(**context):
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True, extensions=[_Generation])
    env.globals["raise_exception"] = _raise
    return env.from_string(TEMPLATE).render(messages=[{"role": "user", "content": "probe"}],
                                            add_generation_prompt=True, **context)


@pytest.mark.parametrize("sent,printed", [
    ("low", "0.2"),          # /think low, every engine
    ("high", "0.9"),         # /think high (the fold sent "xhigh" -> 0.99)
    ("xhigh", "0.99"),       # what every Inkling turn got before T1398
    ("minimal", "0.1"),      # /think 0.1 on NInfer today: the equal name
    (0.1, "0.1"),            # /think 0.1 where a number travels (llama.cpp, NInfer after T1398-B)
    ("none", "0"),           # /think off, through chat_template_kwargs
])
def test_what_liteTUI_sends_is_the_line_the_template_prints(sent, printed):
    assert f"Thinking effort level: {printed}<|end_message|>" in render(reasoning_effort=sent)


def test_no_effort_is_0_9_which_is_why_a_dropped_off_was_a_bug():
    assert "Thinking effort level: 0.9<|end_message|>" in render()


@pytest.mark.parametrize("bad,why", [
    (1.5, "must be in \\[0.0, 0.99\\]"), (-0.1, "must be in \\[0.0, 0.99\\]"),
    ("ultra", "Unknown reasoning_effort: ultra"),
])
def test_the_template_refuses_what_liteTUI_refuses(bad, why):
    with pytest.raises(_Refused, match=why):
        render(reasoning_effort=bad)
