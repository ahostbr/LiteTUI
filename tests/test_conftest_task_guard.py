"""The task-store guard preserves legacy routing and owned storage authority."""
import pytest
from conftest import _never_write_the_live_task_store

from litetui import paths, tasks


def install_guard(tmp_path, monkeypatch):
    """Exercise the actual fixture against a temporary stand-in checkout."""
    # A valid ID ensures a misroute fails the ownership boundary, not ID syntax.
    checkout = tmp_path / "00000000-0000-4000-8000-000000000001"
    checkout.mkdir()
    monkeypatch.setattr(paths, "ROOT", checkout)
    _never_write_the_live_task_store.__wrapped__(tmp_path, monkeypatch)
    return checkout


@pytest.mark.parametrize("live", [True, False])
def test_no_session_save_keeps_legacy_routing(tmp_path, monkeypatch, live):
    calls = []
    monkeypatch.setattr(tasks, "save", lambda rows, root, **kw: calls.append((rows, root, kw)))
    checkout = install_guard(tmp_path, monkeypatch)
    explicit = tmp_path / "explicit"
    rows = [object()]
    tasks.save(rows, checkout if live else explicit)
    expected = tmp_path / "task-store" if live else explicit
    assert calls == [(rows, expected, {})]
    assert expected.exists() is live, "only the legacy redirect creates its directory"
    assert not list(checkout.iterdir())


def test_owned_save_redirect_stays_inside_test_directory(tmp_path, monkeypatch, make_app):
    app = make_app()
    app._materialise_convo()
    checkout = install_guard(tmp_path, monkeypatch)
    task = tasks.new_task("bash", {"command": "fixture"}, app.convo_id)
    destination = app._agent_session.conversation_directory(task.convo_id)
    assert destination.resolve().is_relative_to(tmp_path.resolve())
    tasks.save([task], checkout, agent_session=app._agent_session)
    assert task.id in tasks.load(destination)
    assert (destination / tasks.STORE).is_file()
    assert not list(checkout.iterdir())


def test_owned_save_rejects_a_destination_outside_test_directory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(tasks, "save", lambda *a, **kw: calls.append((a, kw)))
    checkout = install_guard(tmp_path, monkeypatch)
    session = SimpleNamespace(conversation_directory=lambda _: tmp_path.parent / "outside")
    with pytest.raises(AssertionError, match="escaped the per-test directory"):
        tasks.save([SimpleNamespace(convo_id="fixture")], checkout, agent_session=session)
    assert calls == [], "the containment check precedes every writer"
    assert not list(checkout.iterdir())
