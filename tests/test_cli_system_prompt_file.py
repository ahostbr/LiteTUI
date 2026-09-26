from pathlib import Path

from litetui import cli


def test_system_prompt_file_is_read_as_utf8_and_not_left_as_a_path(tmp_path, monkeypatch):
    prompt = 'worker doctrine "quoted" & | % ^'
    path = tmp_path / "worker.md"
    path.write_text(prompt, encoding="utf-8")
    captured = {}

    class FakeApp:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, **_kwargs):
            return None

    monkeypatch.setattr("litetui.app.LiteTUI", FakeApp)
    monkeypatch.setattr("litetui.shared_state.check_data_version", lambda _root: None)
    monkeypatch.setattr("sys.argv", ["litetui", "--system-prompt-file", str(path)])
    cli.main()
    assert captured["system_prompt"] == prompt


def test_system_prompt_and_file_are_mutually_exclusive(tmp_path, monkeypatch, capsys):
    path = tmp_path / "worker.md"
    path.write_text("doctrine", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["litetui", "--system-prompt", "inline", "--system-prompt-file", str(path)],
    )
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("CLI accepted two system-prompt sources")
    assert "mutually exclusive" in capsys.readouterr().err
