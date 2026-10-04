"""Real child adapters with only SDK/network edges faked."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from litetui import subagent_dispatch as dispatch


@pytest.mark.asyncio
async def test_cline_child_supplies_own_refreshing_auth_and_closes_client(monkeypatch):
    import openai

    captured = {}
    async def key():
        return 'child-only-token'
    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = SimpleNamespace(completions=self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            captured['closed'] = True
        async def create(self, **kwargs):
            captured['request'] = kwargs
            return SimpleNamespace(model_dump=lambda: {'choices': [{'message': {'content': 'ok'}}]})
    monkeypatch.setattr(openai, 'AsyncOpenAI', Client)
    backend = SimpleNamespace(name='cline', api_key_provider=key,
                              base_url=lambda: 'https://child.cline.invalid',
                              reasoning_levels=lambda _: ['none', 'high'])
    result = await dispatch._openai(backend, {'model': 'child', 'messages': [], 'reasoning_effort': 'high'})
    assert captured['api_key'] is key
    assert captured['base_url'] == 'https://child.cline.invalid'
    assert captured['request']['reasoning_effort'] == 'high'
    assert captured['closed']
    assert result['choices'][0]['message']['content'] == 'ok'


@pytest.mark.asyncio
async def test_free_child_uses_own_router_and_reports_pool_refusal(monkeypatch):
    import openai

    router = object()
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['http_client'] is router
            self.chat = SimpleNamespace(completions=self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def create(self, **kwargs):
            raise RuntimeError('pool full')
    monkeypatch.setattr(openai, 'AsyncOpenAI', Client)
    backend = SimpleNamespace(name='free', api_key=lambda: 'free', http_client=lambda: router,
                              base_url=lambda: 'https://free.invalid',
                              error_sentence=lambda _: 'Free pool full; choose another free model')
    with pytest.raises(dispatch.ChildError, match='Free pool full'):
        await dispatch._openai(backend, {'model': 'free-child', 'messages': [], 'reasoning_effort': 'none'})


@pytest.mark.asyncio
async def test_claude_child_is_isolated_no_tools_and_closes_owned_session(monkeypatch):
    from litetui import claude_session

    captured = {}
    TextBlock = type('TextBlock', (), {})
    AssistantMessage = type('AssistantMessage', (), {})
    ResultMessage = type('ResultMessage', (), {})
    block = TextBlock()
    block.text = 'Claude child answer'
    assistant = AssistantMessage()
    assistant.content = [block]
    terminal = ResultMessage()
    terminal.is_error = False
    terminal.usage = {'output_tokens': 9}
    class Session:
        def __init__(self, options):
            captured['options'] = options
            self.lifecycle = SimpleNamespace(cleanup_errors=[])
        async def start(self):
            captured['started'] = True
        async def query(self, id, prompt):
            captured['prompt'] = prompt
        async def events(self):
            yield assistant
            yield terminal
        async def close(self):
            captured['closed'] = True
    async def options(**kwargs):
        return kwargs
    monkeypatch.setattr(claude_session, 'ClaudeSession', Session)
    backend = SimpleNamespace(name='claude', _options=options, reasoning_levels=lambda _: ['low', 'high'])
    result = await dispatch._claude(backend, {'model': 'sonnet', 'reasoning_effort': 'high',
                                           'messages': [{'role': 'system', 'content': 'private system'},
                                                        {'role': 'user', 'content': 'private child prompt'}]})
    assert result['choices'][0]['message']['content'] == 'Claude child answer'
    assert result['usage']['completion_tokens'] == 9
    assert captured['options']['tools'] == []
    assert captured['options']['permission_mode'] == 'dontAsk'
    assert captured['options']['effort'] == 'high'
    assert captured['options']['system_prompt'] == 'private system'
    assert captured['prompt'] == 'private child prompt'
    assert captured['closed']


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['start', 'query'])
async def test_claude_deadline_covers_start_and_query_and_closes(stage, monkeypatch):
    import asyncio

    from litetui import claude_session

    closed = []
    class Session:
        lifecycle = SimpleNamespace(cleanup_errors=[])
        def __init__(self, options):
            pass
        async def start(self):
            if stage == 'start':
                await asyncio.Event().wait()
        async def query(self, id, prompt):
            if stage == 'query':
                await asyncio.Event().wait()
        async def close(self):
            closed.append(True)
    async def options(**kwargs):
        return kwargs
    monkeypatch.setattr(claude_session, 'ClaudeSession', Session)
    monkeypatch.setattr(dispatch, 'CHILD_TIMEOUT', 0.01)
    backend = SimpleNamespace(_options=options)
    with pytest.raises(TimeoutError):
        await dispatch._claude(backend, {'model': 'sonnet', 'messages': []})
    assert closed == [True]


def test_runner_uses_cross_backend_result_label_without_parent_change(tmp_path, monkeypatch):
    from litetui.plugins.subagent_plugin import _make_runner
    from litetui.settings import Settings

    host = SimpleNamespace(settings=Settings(), backend=SimpleNamespace(name='claude'), model_id='opus')
    fake = Mock(return_value={'choices': [{'message': {'content': 'child answer'}}],
                             'usage': {'completion_tokens': 7}, 'backend': 'codex', 'model': 'gpt-child'})
    monkeypatch.setattr(dispatch, 'complete_child', fake)
    result = _make_runner(host)({'prompt': 'hello'})
    assert 'model gpt-child' in result
    assert 'Codex uses its minimum' in result
    assert host.model_id == 'opus'
    assert host.backend.name == 'claude'
