"""grep + edit — the surgical file tools (Sentinel/Ryan design, 2026-08-31).

Two token sinks measured on the steam2rs port run drove this pair: whole-file
rewrites (edit kills that) and bash-on-Windows quoting failures (grep invokes
ripgrep DIRECTLY from Python — argv list, no shell layer to get wrong).

The edit guard is the load-bearing contract here: a file must have been read
(via the harness's own tools) since its last change before it may be edited.
Every test below that touches the guard does so through `file_state`, and one
test proves the core_tools hook actually feeds it — because a guard nobody
feeds is a guard that either never fires or always fires, both of which are
broken in different directions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import file_state  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_read_tracker():
    """The tracker is process-global; a read recorded by one test must not
    satisfy another test's guard. Clear before AND after so order never matters."""
    file_state.forget_all()
    yield
    file_state.forget_all()


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    # write_bytes, NOT write_text: on Windows text mode would silently turn
    # the \n's into \r\n and the LF-span tests below could never match their bytes.
    (tmp_path / "a.txt").write_bytes("alpha needle one\nbeta line\ngamma needle two\n".encode("utf-8"))
    (tmp_path / "b.py").write_bytes("# needle in code\nx = 1\n".encode("utf-8"))
    return tmp_path


# ── grep: content mode ──────────────────────────────────────────────────────

def test_grep_content_mode_returns_file_line_text(workdir):
    from litetui import file_tools
    out = file_tools.tool_grep({"pattern": "needle", "path": str(workdir)})
    assert not out.startswith("[error]"), out
    # structured file:line:text — BOTH files, with their real line numbers
    assert f"a.txt:{1}:" in out.replace(str(workdir) + "\\", "").replace(str(workdir), "") or "a.txt" in out
    assert "b.py" in out
    assert "alpha needle one" in out
    assert "# needle in code" in out


def test_grep_line_numbers_are_real(workdir):
    from litetui import file_tools
    out = file_tools.tool_grep({"pattern": "needle", "path": str(workdir)})
    # 'gamma needle two' is line 3 of a.txt — the number must be right, not decorative
    assert any("a.txt" in ln and ":3:" in ln.replace(str(workdir) + "\\", "").replace(str(workdir), "") for ln in out.splitlines()) or "a.txt:3:" in _strip_prefix(out, workdir)


def test_grep_files_mode_lists_paths_only(workdir):
    from litetui import file_tools
    out = file_tools.tool_grep({"pattern": "needle", "path": str(workdir), "mode": "files"})
    assert not out.startswith("[error]"), out
    body = [ln for ln in out.splitlines() if ln.strip()]
    # no match text, no line numbers — just paths
    assert any("a.txt" in ln for ln in body)
    assert any("b.py" in ln for ln in body)
    assert "alpha needle one" not in out


def test_grep_no_match_is_not_an_error(workdir):
    from litetui import file_tools
    out = file_tools.tool_grep({"pattern": "zzz-not-there", "path": str(workdir)})
    assert not out.startswith("[error]"), f"no-match must not be an error: {out}"
    assert "no match" in out.lower()


def test_grep_max_results_is_a_hard_cap(tmp_path):
    from litetui import file_tools
    p = tmp_path / "many.txt"
    p.write_text("\n".join(f"row-{i} hit" for i in range(50)) + "\n", encoding="utf-8")
    out = file_tools.tool_grep({"pattern": "hit", "path": str(p), "max_results": 10})
    result_lines = [ln for ln in out.splitlines() if ":row-" in _strip_prefix(ln, tmp_path) or (":" in ln and "row-" in ln)]
    assert len(result_lines) == 10, f"cap must bite at exactly max_results: {len(result_lines)}\n{out}"
    assert "truncat" in out.lower()


def test_grep_glob_filters_files(workdir):
    from litetui import file_tools
    out = file_tools.tool_grep({"pattern": "needle", "path": str(workdir), "glob": "*.txt"})
    assert "a.txt" in out
    assert "b.py" not in out


def test_grep_bad_regex_reports_rg_error_not_silence(workdir):
    from litetui import file_tools
    out = file_tools.tool_grep({"pattern": "[", "path": str(workdir)})
    assert out.startswith("[error]"), f"a bad regex is an error, not a no-match: {out}"


def test_grep_missing_pattern_is_an_error():
    from litetui import file_tools
    assert file_tools.tool_grep({}).startswith("[error]")


def test_grep_bad_mode_is_an_error(workdir):
    from litetui import file_tools
    out = file_tools.tool_grep({"pattern": "x", "path": str(workdir), "mode": "bogus"})
    assert out.startswith("[error]") and "files" in out


def test_grep_missing_path_is_an_error(tmp_path):
    from litetui import file_tools
    out = file_tools.tool_grep({"pattern": "x", "path": str(tmp_path / "nope")})
    assert out.startswith("[error]")


# ── edit: the happy path and its byte fidelity ─────────────────────────────

def test_edit_replaces_a_unique_string(workdir):
    from litetui import file_tools
    p = workdir / "a.txt"
    file_state.record_read(p)  # stand-in for a prior `read` tool call
    out = file_tools.tool_edit({
        "path": str(p), "old_string": "beta line", "new_string": "BETA LINE v2"})
    assert not out.startswith("[error]"), out
    text = p.read_text(encoding="utf-8")
    assert "BETA LINE v2" in text and "beta line" not in text
    # the untouched lines are byte-identical
    assert text == "alpha needle one\nBETA LINE v2\ngamma needle two\n"


def test_edit_empty_new_string_deletes_the_span(workdir):
    from litetui import file_tools
    p = workdir / "a.txt"
    file_state.record_read(p)
    out = file_tools.tool_edit({"path": str(p), "old_string": "beta line\n", "new_string": ""})
    assert not out.startswith("[error]"), out
    assert "beta line" not in p.read_text(encoding="utf-8")


def test_edit_preserves_crlf_outside_the_span(tmp_path):
    """Line endings are a byte fact. A CRLF file edited on Windows must come
    back CRLF everywhere except the replaced span — text-mode roundtrips have
    corrupted this exact thing before."""
    from litetui import file_tools
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    file_state.record_read(p)
    out = file_tools.tool_edit({"path": str(p), "old_string": "two", "new_string": "TWO"})
    assert not out.startswith("[error]"), out
    assert p.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_edit_replace_all_replaces_every_occurrence(workdir):
    from litetui import file_tools
    p = workdir / "a.txt"
    file_state.record_read(p)
    out = file_tools.tool_edit({
        "path": str(p), "old_string": "needle", "new_string": "NEEDLE", "replace_all": True})
    assert not out.startswith("[error]"), out
    text = p.read_text(encoding="utf-8")
    assert text.count("NEEDLE") == 2 and "needle" not in text


def test_edit_reports_occurrence_count(workdir):
    from litetui import file_tools
    p = workdir / "a.txt"
    file_state.record_read(p)
    out = file_tools.tool_edit({
        "path": str(p), "old_string": "needle", "new_string": "n", "replace_all": True})
    assert "2" in out  # the count is part of the contract, not a nicety


def test_edit_lf_old_string_matches_a_crlf_file(tmp_path):
    """T147, the defect as measured: read shows the model LF endings, so the
    old_string it types is LF — while this file is CRLF. Exact match finds nothing;
    the normalized path must find the span and write back in the file's own ending.
    Whole-file byte equality IS the 'byte-for-byte outside the span' proof: every
    untouched line keeps its original \r\n."""
    from litetui import file_tools
    p = tmp_path / "crlf.txt"
    before = b"alpha\r\nbeta line\r\ngamma\r\n"
    p.write_bytes(before)
    file_state.record_read(p)
    out = file_tools.tool_edit({
        "path": str(p),
        "old_string": "alpha\nbeta line",       # LF endings, as the model types them
        "new_string": "ALPHA\nBETA LINE v2"})
    assert not out.startswith("[error]"), out
    after = p.read_bytes()
    assert after == b"ALPHA\r\nBETA LINE v2\r\ngamma\r\n", after
    assert b"\n" not in after.replace(b"\r\n", b""), "a bare LF leaked into a CRLF file"


def test_edit_lf_file_never_enters_the_normalization_path(tmp_path):
    """A pure-LF file contains no CRLF, so the exact-match path is the only one —
    behaviour must be byte-identical to pre-T147: LF in, LF out (no \r introduced),
    and a CRLF-spelled old_string still finds nothing, because there are no CRLFs in
    the file to normalize against."""
    from litetui import file_tools
    p = tmp_path / "lf.txt"
    p.write_bytes(b"one\ntwo\nthree\n")
    file_state.record_read(p)
    out = file_tools.tool_edit({"path": str(p), "old_string": "two", "new_string": "TWO"})
    assert not out.startswith("[error]"), out
    assert p.read_bytes() == b"one\nTWO\nthree\n"
    out2 = file_tools.tool_edit(
        {"path": str(p), "old_string": "one\r\ntwo", "new_string": "x"})
    assert out2.startswith("[error]") and "not found" in out2.lower(), out2


def test_edit_refuses_a_mixed_ending_file_instead_of_reencoding(tmp_path):
    """MIXED endings: the normalized path CAN find the span, but writing back would
    re-encode every bare-LF line the edit never touched — a silent history rewrite.
    Refuse with both counts; the file comes back byte-identical."""
    from litetui import file_tools
    p = tmp_path / "mixed.txt"
    before = b"crlf one\r\ncrlf two\r\nbare lf line\nlast crlf\r\n"   # 3 CRLF, 1 bare LF
    p.write_bytes(before)
    file_state.record_read(p)
    out = file_tools.tool_edit({
        "path": str(p),
        "old_string": "crlf one\ncrlf two",     # matches in normalized space... 
        "new_string": "CHANGED"})
    assert out.startswith("[error]"), f"mixed endings must be refused: {out}"
    assert "MIXED" in out, "the refusal must name the condition"
    assert "3 CRLF" in out and "1 bare LF" in out, \
        "both counts are the point — they tell the model what to normalize"
    assert p.read_bytes() == before, "a refused edit writes nothing"

# ── edit: the uniqueness check ──────────────────────────────────────────────

def test_edit_ambiguous_old_string_errors_with_the_count(workdir):
    from litetui import file_tools
    p = workdir / "a.txt"
    file_state.record_read(p)
    out = file_tools.tool_edit({"path": str(p), "old_string": "needle", "new_string": "x"})
    assert out.startswith("[error]"), f"ambiguous edit must fail: {out}"
    assert "2" in out, "the error must say HOW MANY times it occurs"
    # and the file is untouched — a failed edit writes nothing
    assert p.read_text(encoding="utf-8").count("needle") == 2


def test_edit_not_found_errors_and_leaves_file_untouched(workdir):
    from litetui import file_tools
    p = workdir / "a.txt"
    before = p.read_bytes()
    file_state.record_read(p)
    out = file_tools.tool_edit({"path": str(p), "old_string": "never was here", "new_string": "x"})
    assert out.startswith("[error]") and "not found" in out.lower()
    assert p.read_bytes() == before


def test_edit_missing_old_string_is_an_error(workdir):
    from litetui import file_tools
    p = workdir / "a.txt"
    file_state.record_read(p)
    assert file_tools.tool_edit({"path": str(p), "old_string": "", "new_string": "x"}).startswith("[error]")


def test_edit_missing_file_is_an_error(tmp_path):
    from litetui import file_tools
    out = file_tools.tool_edit({
        "path": str(tmp_path / "ghost.txt"), "old_string": "a", "new_string": "b"})
    assert out.startswith("[error]") and "not found" in out.lower()


# ── edit: the read-before-edit guard ───────────────────────────────────────

def test_edit_refuses_a_file_that_was_never_read(workdir):
    from litetui import file_tools
    p = workdir / "a.txt"
    before = p.read_bytes()
    out = file_tools.tool_edit({"path": str(p), "old_string": "beta line", "new_string": "x"})
    assert out.startswith("[refused]"), f"blind edit must be refused: {out}"
    assert "read" in out.lower(), "the refusal must say what to do about it"
    assert p.read_bytes() == before, "a refused edit writes nothing"


def test_edit_refuses_when_the_file_changed_after_the_read(workdir):
    """The guard tracks (mtime, size) at read time. A change AFTER the read —
    by anyone — voids the read and forces a fresh look."""
    from litetui import file_tools
    p = workdir / "a.txt"
    file_state.record_read(p)
    # simulate an external change: new content, force a distinct mtime
    p.write_text("alpha needle one\nCHANGED externally\ngamma needle two\n", encoding="utf-8")
    _bump_mtime(p)
    out = file_tools.tool_edit({"path": str(p), "old_string": "beta line", "new_string": "x"})
    assert out.startswith("[refused]"), f"stale read must be refused: {out}"


def test_a_successful_read_tool_call_feeds_the_guard(workdir):
    """THE wiring test. The guard is only as good as its feeder: core_tools'
    `read` handler must record the file it serves, or every edit in a fresh
    session fails until someone discovers the hidden dependency."""
    from litetui.plugins import core_tools as ct
    p = workdir / "a.txt"
    read_out = ct.tool_read({"path": str(p)})
    assert not read_out.startswith("[error]"), read_out
    out = file_tools_edit_after_read(p)
    assert not out.startswith(("[refused]", "[error]")), (
        f"a file just served by the `read` tool must be editable: {out}")


def test_a_successful_write_tool_call_feeds_the_guard(workdir):
    """Same logic for `write`: after a full rewrite the model knows exactly
    what is in the file, so an immediate edit must not demand another read."""
    from litetui.plugins import core_tools as ct
    p = workdir / "fresh.txt"
    w = ct.tool_write({"path": str(p), "content": "line one\nline two\n"})
    assert not w.startswith("[error]"), w
    out = file_tools_edit_after_read(p, old="line two", new="LINE TWO")
    assert not out.startswith(("[refused]", "[error]")), (
        f"a file just written must be editable: {out}")


def test_edit_records_fresh_state_so_a_second_edit_needs_no_reread(workdir):
    """After an edit the model has seen the new content by construction —
    demanding a re-read for the very next edit would make sequential edits
    impossible without a pointless roundtrip."""
    from litetui import file_tools
    p = workdir / "a.txt"
    file_state.record_read(p)
    first = file_tools.tool_edit({"path": str(p), "old_string": "beta line", "new_string": "B1"})
    assert not first.startswith(("[error]", "[refused]")), first
    second = file_tools.tool_edit({"path": str(p), "old_string": "gamma needle two", "new_string": "G2"})
    assert not second.startswith(("[error]", "[refused]")), (
        f"the edit's own write must count as having seen the file: {second}")


# ── helpers ─────────────────────────────────────────────────────────────────

def _strip_prefix(text: str, root: Path) -> str:
    """rg prints paths relative to how it was invoked; normalize both spellings."""
    return text.replace(str(root) + "\\", "").replace(str(root), "")


def file_tools_edit_after_read(p: Path, old: str = "beta line", new: str = "x") -> str:
    from litetui import file_tools
    return file_tools.tool_edit({"path": str(p), "old_string": old, "new_string": new})


def _bump_mtime(p: Path) -> None:
    """Force a mtime strictly later than the read's snapshot (100ns ticks on
    NTFS are not enough to guarantee separation from a same-instant write)."""
    import os, time
    st = p.stat()
    future = (st.st_mtime_ns // 100) * 100 + 2_000_000  # +2s in 100ns units
    os.utime(p, ns=(future, future))
