from litetui import file_tools, file_state


def test_edit_does_not_overwrite_predictable_sibling(tmp_path):
    target = tmp_path / 'target.txt'
    sibling = tmp_path / 'target.txt.edit-tmp'
    target.write_text('old', encoding='utf-8')
    sibling.write_text('unrelated', encoding='utf-8')
    file_state.record_read(target)
    result = file_tools.tool_edit({'path': str(target), 'old_string': 'old', 'new_string': 'new'})
    assert result.startswith('Edited')
    assert sibling.read_text(encoding='utf-8') == 'unrelated'
    assert target.read_text(encoding='utf-8') == 'new'


def test_concurrent_change_is_retained_and_temp_cleaned(tmp_path, monkeypatch):
    import tempfile
    target = tmp_path / 'target.txt'
    target.write_text('old', encoding='utf-8')
    file_state.record_read(target)
    real = tempfile.mkstemp
    def racing_temp(*args, **kwargs):
        result = real(*args, **kwargs)
        target.write_text('other writer', encoding='utf-8')
        return result
    monkeypatch.setattr(tempfile, 'mkstemp', racing_temp)
    result = file_tools.tool_edit({'path': str(target), 'old_string': 'old', 'new_string': 'new'})
    assert 'changed during edit' in result
    assert target.read_text(encoding='utf-8') == 'other writer'
    assert not list(tmp_path.glob('.*.edit-*.tmp'))


def test_replace_failure_cleans_exclusive_temp(tmp_path, monkeypatch):
    target = tmp_path / 'target.txt'
    target.write_text('old', encoding='utf-8')
    file_state.record_read(target)
    def fail(*args): raise OSError('injected replace failure')
    monkeypatch.setattr(file_tools.os, 'replace', fail)
    result = file_tools.tool_edit({'path': str(target), 'old_string': 'old', 'new_string': 'new'})
    assert 'injected replace failure' in result
    assert target.read_text(encoding='utf-8') == 'old'
    assert not list(tmp_path.glob('.*.edit-*.tmp'))


def test_cancellation_cleans_temp_without_swallowing_interrupt(tmp_path, monkeypatch):
    import pytest
    target = tmp_path / 'target.txt'
    target.write_text('old', encoding='utf-8')
    file_state.record_read(target)
    def cancel(*args): raise KeyboardInterrupt()
    monkeypatch.setattr(file_tools.os, 'replace', cancel)
    with pytest.raises(KeyboardInterrupt):
        file_tools.tool_edit({'path': str(target), 'old_string': 'old', 'new_string': 'new'})
    assert target.read_text(encoding='utf-8') == 'old'
    assert not list(tmp_path.glob('.*.edit-*.tmp'))


def test_edit_holds_coordinator_during_replace(tmp_path, monkeypatch):
    import pytest
    from litetui.shared_state import Lease, OwnershipError
    target = tmp_path / 'target.txt'
    target.write_text('old', encoding='utf-8')
    file_state.record_read(target)
    real_replace = file_tools.os.replace
    def checked_replace(source, destination):
        with pytest.raises(OwnershipError):
            with Lease(str(target) + '.lock'):
                pass
        return real_replace(source, destination)
    monkeypatch.setattr(file_tools.os, 'replace', checked_replace)
    assert file_tools.tool_edit({'path': str(target), 'old_string': 'old', 'new_string': 'new'}).startswith('Edited')
    with Lease(str(target) + '.lock'):
        assert target.read_text(encoding='utf-8') == 'new'
