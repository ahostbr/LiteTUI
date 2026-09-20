"""Process identity, not a reused PID, authorizes managed task cleanup."""
import pytest


def test_reused_pid_is_not_terminated():
    from litetui.task_supervisor import TaskSupervisor, ProcessIdentity
    current = {42: 'new-process'}
    killed = []
    supervisor = TaskSupervisor('instance-a', identity_probe=current.get, terminate=killed.append)
    task = supervisor.register(ProcessIdentity(42, 'old-process'))
    outcome = supervisor.cancel(task)
    assert outcome.state == 'identity_mismatch'
    assert killed == []


def test_only_owner_can_cancel_and_terminal_outcome_is_idempotent():
    from litetui.task_supervisor import TaskSupervisor, ProcessIdentity
    killed = []
    supervisor = TaskSupervisor('instance-a', identity_probe=lambda pid: 'birth', terminate=killed.append)
    task = supervisor.register(ProcessIdentity(42, 'birth'))
    with pytest.raises(PermissionError):
        supervisor.cancel(task, requester='instance-b')
    assert supervisor.cancel(task).state == 'cancelled'
    assert supervisor.cancel(task).state == 'cancelled'
    assert killed == [42]


def test_own_process_cannot_be_assigned_to_kill_job(monkeypatch):
    import os
    from litetui import jobkill
    opened = []
    monkeypatch.setattr(jobkill, 'available', lambda: True)
    class Kernel:
        def OpenProcess(self, *args): opened.append(args); return None
    monkeypatch.setattr(jobkill, '_k32', Kernel())
    assert not jobkill.assign(123, os.getpid())
    assert not opened


def test_new_background_task_has_instance_identity_and_long_id():
    from litetui import tasks
    task = tasks.new_task('bash', {'command': 'echo test'}, 'fixture')
    assert len(task.id.removeprefix('t-')) >= 32
    assert task.owner_instance
    assert task.owner_created


def test_foreign_instance_with_same_pid_cannot_be_cancelled():
    import os
    from litetui import tasks
    task = tasks.new_task('bash', {}, 'fixture')
    task.owner_instance = 'another-instance'
    task.proc = object()
    assert tasks.request_kill(task) == (False, None)


def test_reused_foreign_pid_is_not_reported_alive(monkeypatch):
    from litetui import tasks, task_supervisor
    task = tasks.new_task('bash', {}, 'fixture')
    task.owner_pid = 123456
    task.owner_created = 'old'
    monkeypatch.setattr(task_supervisor, 'process_creation_identity', lambda pid: 'new')
    monkeypatch.setattr(tasks.router_record, 'pid_is_live', lambda pid: True)
    assert not tasks._owner_alive(task)
