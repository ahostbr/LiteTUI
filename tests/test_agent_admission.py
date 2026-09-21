"""WS4 local admission: the capacity-reservation boundary as a typed decision.

Negative-first (per the spawn contract): these prove that today's refusal of a
local backend is a *decision* with a distinct capacity reason -- not the generic
'only hosted worktree' string -- and that a verified reservation (the WS3
resolver's verdict) is the seam that would flip a local refusal to admit. No
model is loaded and no capacity is invented: a reservation here is an opaque
token asserting the caller verified headroom externally. The composed services
pass no reservation, so the composed local path stays fail-closed.
"""
import pytest

from litetui.agent_launcher import LaunchBlocked, LaunchSpec, validate_request


def _spec(**over):
    request = {'prompt': 'task', 'backend': 'codex', 'model': 'model',
               'workspace': '/tmp/ws'}
    request.update(over)
    return validate_request(request, parent_profile='autonomous', depth=0)


def test_classify_backend_kinds():
    from litetui.agent_admission import classify_backend
    assert classify_backend('codex') == 'hosted'
    assert classify_backend('ninfer') == 'local'
    assert classify_backend('lmstudio') == 'local'
    assert classify_backend('llamacpp') == 'local'
    assert classify_backend('openai') == 'unknown'


def test_hosted_headless_worktree_is_admitted():
    from litetui.agent_admission import admit_launch
    verdict = admit_launch(_spec())
    assert verdict.admitted is True
    assert verdict.reason is None
    assert verdict.category is None


def test_local_without_reservation_is_refused_on_capacity():
    from litetui.agent_admission import admit_launch
    verdict = admit_launch(_spec(backend='ninfer'))
    assert verdict.admitted is False
    assert verdict.category == 'local-capacity'
    assert 'reservation' in verdict.reason.lower()


def test_local_refusal_is_not_the_generic_hosted_only_string():
    # The old opaque block must NOT be what a local refusal carries: the reason
    # names the missing reservation so the UI can surface capacity/options.
    from litetui.agent_admission import admit_launch
    verdict = admit_launch(_spec(backend='lmstudio'))
    assert 'Only hosted' not in (verdict.reason or '')
    assert 'Only isolated' not in (verdict.reason or '')


def test_local_with_verified_reservation_clears_the_capacity_gate():
    # The seam is real: a caller holding a verified reservation is not refused
    # on capacity grounds. Proves the gate consults the reservation rather than
    # hard-refusing every local request. This does not fake capacity -- it only
    # proves the decision path exists.
    from litetui.agent_admission import admit_launch
    verdict = admit_launch(_spec(backend='ninfer'), reservation=object())
    assert verdict.admitted is True


def test_local_with_reservation_still_refused_when_headed():
    from litetui.agent_admission import admit_launch
    verdict = admit_launch(_spec(backend='ninfer', headed=True), reservation=object())
    assert verdict.admitted is False
    assert verdict.category == 'headed'


def test_headed_is_refused_regardless_of_backend():
    from litetui.agent_admission import admit_launch
    verdict = admit_launch(_spec(headed=True))
    assert verdict.admitted is False
    assert verdict.category == 'headed'


def test_explicit_workspace_is_refused():
    from litetui.agent_admission import admit_launch
    verdict = admit_launch(_spec(workspace_mode='explicit'))
    assert verdict.admitted is False
    assert verdict.category == 'explicit-workspace'


def test_unknown_backend_is_refused_defensively():
    from litetui.agent_admission import admit_launch
    spec = LaunchSpec('task', 'openai', 'model', '/tmp/ws', 'autonomous',
                      False, 'worktree')
    verdict = admit_launch(spec)
    assert verdict.admitted is False
    assert verdict.category == 'unknown-backend'


def test_composed_prepare_child_still_refuses_local_without_filesystem_effects(tmp_path):
    # The composed service passes NO reservation, so a local request is refused
    # with the capacity reason and has no filesystem effects.
    from litetui.agent_preparation import prepare_child
    spec = validate_request({'prompt': 'task', 'backend': 'ninfer', 'model': 'model',
                             'workspace': str(tmp_path)},
                            parent_profile='autonomous', depth=0)
    storage = tmp_path / 'children'
    with pytest.raises(LaunchBlocked) as exc:
        prepare_child(spec, storage=storage, child_id='a' * 32, baseline='HEAD',
                      supported_levels=[])
    assert 'reservation' in str(exc.value).lower()
    assert not storage.exists()
