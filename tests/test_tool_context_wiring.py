"""The three context modes are WIRED — plan_tool_result now has a caller.

tool_context.py shipped in aa5ebcb deliberately unwired (app.py was busy).
This file guards the wiring: _contextualise_tool_result routes a tool result
through the planned mode, parks the raw in a sidecar the model can read back,
and fails SAFE — every early exit returns the raw, because the direction that
keeps everything is the direction a bug should point.
"""
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

import tool_context
from app import LiteTUI

call = LiteTUI._contextualise_tool_result
BIG = "x" * 5000 + "\nline two\n"          # comfortably over the 2000 default
APP_SRC = (Path(__file__).resolve().parent.parent / "src" / "app.py").read_text(encoding="utf-8")


class FakeMsg:
    def __init__(self, content): self.content = content
class FakeResp:
    def __init__(self, content): self.choices = [SimpleNamespace(message=FakeMsg(content))]
class FakeCompletions:
    def __init__(self, reply=None, raise_=False):
        self.reply, self.raise_, self.calls = reply, raise_, []
    async def create(self, **kw):
        self.calls.append(kw)
        if self.raise_: raise RuntimeError("side call down")
        return FakeResp(self.reply)


def app_double(tmp_path, mode, reply=None, raise_=False, convo_dir="set"):
    comp = FakeCompletions(reply, raise_)
    ns = SimpleNamespace(
        settings=SimpleNamespace(
            tool_context_mode=mode,
            tool_context_threshold_chars=2000,
            compact_max_tokens=4096,
        ),
        convo_dir=(tmp_path if convo_dir == "set" else None),
        conversation=[{"role": "user", "content": "find the bug in run_all"}],
        _flatten=lambda self_or_c: self_or_c if isinstance(self_or_c, str) else "",
        client=SimpleNamespace(chat=SimpleNamespace(completions=comp)),
        model_id="m",
    )
    return ns, comp


def run(ns, name="bash", raw=BIG):
    return asyncio.run(call(ns, name, raw))


# --- off / verbatim ----------------------------------------------------------
def test_off_returns_raw_and_writes_nothing(tmp_path):
    ns, _ = app_double(tmp_path, "off")
    assert run(ns) == BIG
    assert not (tmp_path / "tool-raw").exists()


def test_small_results_are_verbatim_even_in_mask_mode(tmp_path):
    ns, _ = app_double(tmp_path, "llm-tool-mask")
    assert run(ns, raw="short output") == "short output"


def test_view_image_is_never_processed(tmp_path):
    """Its content is an image payload; a summary would destroy it."""
    ns, _ = app_double(tmp_path, "llm-tool-mask")
    assert run(ns, name="view_image") == BIG


def test_no_convo_dir_returns_raw_rather_than_destroying(tmp_path):
    ns, _ = app_double(tmp_path, "llm-tool-mask", convo_dir=None)
    assert run(ns) == BIG


# --- mask --------------------------------------------------------------------
def test_mask_replaces_context_and_parks_the_raw(tmp_path):
    ns, comp = app_double(tmp_path, "llm-tool-mask")
    out = run(ns)
    assert out != BIG and "masked" in out and str(len(BIG)) in out
    files = list((tmp_path / "tool-raw").iterdir())
    assert len(files) == 1
    # BYTE-EXACT: the sidecar is the only copy the model can get back.
    assert files[0].read_text(encoding="utf-8") == BIG
    assert str(files[0]) in out          # the pointer is dereferenceable
    assert comp.calls == []              # mask makes NO model call


# --- summarise ---------------------------------------------------------------
def test_summ_puts_the_summary_and_pointer_in_context(tmp_path):
    ns, comp = app_double(tmp_path, "llm-tool-summ", reply="run_all exits 7 on gate X")
    out = run(ns)
    assert "run_all exits 7 on gate X" in out and "summarised" in out
    assert len(comp.calls) == 1
    sent = comp.calls[0]["messages"][0]["content"]
    assert "find the bug in run_all" in sent    # anchored to the TASK
    assert BIG.strip()[:40] in sent             # the raw actually went out


def test_summ_side_call_failure_degrades_to_mask_not_to_loss(tmp_path):
    ns, _ = app_double(tmp_path, "llm-tool-summ", raise_=True)
    out = run(ns)
    assert "masked" in out
    files = list((tmp_path / "tool-raw").iterdir())
    assert files and files[0].read_text(encoding="utf-8") == BIG


def test_summ_empty_reply_degrades_to_mask(tmp_path):
    """A model that says nothing must not produce an empty 'summary' — that
    would read as 'the output contained nothing relevant', which is a CLAIM."""
    ns, _ = app_double(tmp_path, "llm-tool-summ", reply="   ")
    assert "masked" in run(ns)


def test_summ_failure_records_why_it_fell_back(tmp_path):
    """The mask fallback is correct; falling back SILENTLY is the defect. A
    backend that can never summarise degrades every big result to a mask, and
    without a recorded reason that persistent failure is invisible forever."""
    ns, _ = app_double(tmp_path, "llm-tool-summ", raise_=True)
    run(ns)
    log = tmp_path / "tool-raw" / "summarise-failures.log"
    assert log.exists(), "the fallback must leave a recorded reason"
    text = log.read_text(encoding="utf-8")
    assert "RuntimeError" in text and "side call down" in text
    assert "-bash.txt" in text  # the reason names the parked raw it belongs to


def test_summ_empty_reply_records_why_it_fell_back(tmp_path):
    ns, _ = app_double(tmp_path, "llm-tool-summ", reply="   ")
    run(ns)
    text = (tmp_path / "tool-raw" / "summarise-failures.log").read_text(encoding="utf-8")
    assert "empty summary" in text


def test_summ_success_records_no_failure(tmp_path):
    """Control: the log only ever carries failures — a reason written on
    success would bury the real ones under noise."""
    ns, _ = app_double(tmp_path, "llm-tool-summ", reply="a fine summary")
    run(ns)
    assert not (tmp_path / "tool-raw" / "summarise-failures.log").exists()


# --- placement ---------------------------------------------------------------
def test_exactly_one_call_site_and_it_is_not_in_compact():
    """_compact's tool loop feeds a THROWAWAY `ask` list — summarising there
    would spend a side call shrinking a context that is discarded. The one
    real site pairs with tc_id inside _stream."""
    assert APP_SRC.count("await self._contextualise_tool_result(") == 1
    compact_body = APP_SRC.split("async def _compact(", 1)[1].split("\n    def ", 1)[0]
    assert "_contextualise_tool_result" not in compact_body
