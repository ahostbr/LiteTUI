import sqlite3


def test_absent_stores_are_idle_without_creating_files(tmp_path):
    from litetui.plugin_reload_children import children_pending
    root = tmp_path / 'absent'
    assert children_pending(root, 'parent') is False
    assert not root.exists()


def test_corrupt_or_invalid_identity_is_unknown(tmp_path):
    from litetui.plugin_reload_children import children_pending
    (tmp_path / 'registry.sqlite').write_bytes(b'broken')
    assert children_pending(tmp_path, 'parent') is None
    assert children_pending(tmp_path, None) is None


def test_pending_rows_are_parent_scoped_and_not_acknowledged(tmp_path):
    from litetui.plugin_reload_children import children_pending
    from litetui.agent_registry import AgentRegistry
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    registry.claim('parent', 'child', limit=1)
    assert children_pending(tmp_path, 'parent') is True
    assert children_pending(tmp_path, 'other') is False
    assert len(registry.active('parent')) == 1


def test_applied_receipt_with_unfinished_wake_still_blocks(tmp_path):
    from litetui.plugin_reload_children import children_pending
    from litetui.agent_receipts import ParentReceipts
    ParentReceipts(tmp_path / 'receipts.sqlite')
    with sqlite3.connect(tmp_path / 'receipts.sqlite') as db:
        db.execute("INSERT INTO receipts VALUES ('parent','receipt','{}',0,1,'parent','claimed')")
    assert children_pending(tmp_path, 'parent') is True
    with sqlite3.connect(tmp_path / 'receipts.sqlite') as db:
        db.execute("UPDATE receipts SET wake_state='finished'")
    assert children_pending(tmp_path, 'parent') is False
