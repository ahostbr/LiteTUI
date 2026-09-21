import json
from litetui import tool_schemas


def test_fresh_read_does_not_mutate_cached_live_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_schemas, '_package_dir', lambda: tmp_path)
    tool_schemas._read.cache_clear()
    path = tmp_path / 'fixture.json'
    def content(text):
        return json.dumps({'type': 'function', 'function': {'name': 'fixture', 'description': text,
                           'parameters': {'type': 'object', 'properties': {}}}})
    try:
        path.write_text(content('old'))
        live = tool_schemas.load('fixture')
        path.write_text(content('new'))
        candidate = tool_schemas.load_fresh('fixture')
        assert candidate['function']['description'] == 'new'
        assert live['function']['description'] == 'old'
        assert tool_schemas.load('fixture')['function']['description'] == 'old'
        path.write_text('{invalid')
        import pytest
        with pytest.raises(ValueError):
            tool_schemas.load_fresh('fixture')
        assert tool_schemas.load('fixture')['function']['description'] == 'old'
    finally:
        tool_schemas._read.cache_clear()
