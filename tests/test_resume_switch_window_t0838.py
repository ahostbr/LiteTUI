"""T0838 (revised): picking another agent's conversation in /resume switches THIS terminal to it.

Ryan 2026-11-04: "i want the resume to switch to that agent and convo in that terminal when i do it ....
agents must of confused litesuite agnets spawn side with manual use". No spawn, no split, no new window.
Same relaunch as /agent-new (cli.py runs `litetui.cli --agent NAME [--convo ID]` in this console).
"""
import sys
from types import SimpleNamespace

from litetui.plugins import convo
from test_agent_picker_tasks import AID, BID, CID, home, transcript


def _pick_recorder(monkeypatch):
    picks = []
    monkeypatch.setattr(convo, 'pick', lambda app, title, items, callback, **kw:
                        picks.append((title, items, callback)))
    return picks


def test_agent_with_no_conversations_offers_new_and_switches_with_agent_only(tmp_path, monkeypatch):
    with home(tmp_path, 'OtherAgent', BID):
        pass  # no transcript: the picker offers 'new'
    with home(tmp_path, 'QuietHelm', AID) as session:
        exits, messages = [], []
        app = SimpleNamespace(_agent_session=session, store=SimpleNamespace(persist_error=None),
                              system_message=messages.append, exit=lambda: exits.append(True))
        picks = _pick_recorder(monkeypatch)
        convo._open_convos_picker(app)
        picks[0][2](BID)
        assert picks[1][1] == [('new', 'Start a new conversation')]
        picks[1][2]('new')
        assert app._relaunch_argv == [sys.executable, '-m', 'litetui.cli', '--agent', 'OtherAgent']
        assert exits == [True]


def test_a_running_turn_refuses_the_switch(tmp_path, monkeypatch):
    with home(tmp_path, 'OtherAgent', BID) as other:
        transcript(other, 'other conversation')
    with home(tmp_path, 'QuietHelm', AID) as session:
        exits, messages = [], []
        app = SimpleNamespace(_agent_session=session, store=SimpleNamespace(persist_error=None),
                              system_message=messages.append, exit=lambda: exits.append(True),
                              _chat_running=lambda: True)
        picks = _pick_recorder(monkeypatch)
        convo._open_convos_picker(app)
        picks[0][2](BID)
        picks[1][2](CID)
        assert not hasattr(app, '_relaunch_argv')
        assert exits == []
        assert messages == ['Finish or stop the current turn before switching agents.']


def test_a_spawned_seat_keeps_its_agent(tmp_path, monkeypatch):
    with home(tmp_path, 'OtherAgent', BID) as other:
        transcript(other, 'other conversation')
    with home(tmp_path, 'QuietHelm', AID) as session:
        exits, messages = [], []
        app = SimpleNamespace(_agent_session=session, store=SimpleNamespace(persist_error=None),
                              system_message=messages.append, exit=lambda: exits.append(True),
                              _spawned_marker=True)
        picks = _pick_recorder(monkeypatch)
        convo._open_convos_picker(app)
        picks[0][2](BID)
        picks[1][2](CID)
        assert not hasattr(app, '_relaunch_argv')
        assert exits == []
        assert 'keeps its agent' in messages[0]


def test_a_hand_launched_seat_hands_its_child_an_env_without_the_agent_shell_marker(monkeypatch):
    monkeypatch.setenv('LITETUI_AGENT_SHELL', '1')
    app = SimpleNamespace(_agent_launched=False, _spawned_seat=False)
    env = convo._relaunch_env(app)
    assert env is not None and 'LITETUI_AGENT_SHELL' not in env
