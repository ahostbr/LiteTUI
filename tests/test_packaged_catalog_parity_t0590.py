"""Checkout-only parity: tracked packaged prompts, never install-directory debris."""
import os
import subprocess
from pathlib import Path, PurePosixPath
from unittest.mock import Mock

import pytest


def _tracked_packaged_names(checkout: Path) -> set[str]:
    marker = checkout / '.git'
    if not (marker.is_file() or marker.is_dir()):
        pytest.skip('checkout-only catalog parity: no local .git metadata (archive/installed context)')

    # A checkout test must fail, not silently pass against an installed/main copy.
    from litetui import prompt_overrides

    assert Path(prompt_overrides.__file__).resolve() == checkout / 'src/litetui/prompt_overrides.py'
    env = os.environ.copy()
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        env.pop(name, None)
    # Even malformed local metadata must not discover an ancestor repository.
    env['GIT_CEILING_DIRECTORIES'] = str(checkout.parent)
    env['GIT_OPTIONAL_LOCKS'] = '0'
    result = subprocess.run(
        ['git', 'ls-files', '--cached', '--full-name', '-z', '--',
         ':(glob)src/litetui/prompts/*.md'],
        cwd=checkout, env=env, capture_output=True, check=True, timeout=10,
    )
    paths = [PurePosixPath(os.fsdecode(raw)) for raw in result.stdout.split(b'\0') if raw]
    assert paths, 'Git returned no tracked packaged prompts; parity was not established'
    assert all(path.parent == PurePosixPath('src/litetui/prompts') for path in paths)
    return {path.name for path in paths}


def test_packaged_catalog_equals_git_tracked_prompt_files():
    checkout = Path(__file__).resolve().parents[1]
    tracked = _tracked_packaged_names(checkout)
    from litetui import prompt_overrides

    assert prompt_overrides.PACKAGED == tracked
    print(f'CHECKOUT_CATALOG_PARITY: root={checkout}; metadata=file:{(checkout / ".git").is_file()}; '
          f'import={prompt_overrides.__file__}; tracked={len(tracked)}')


@pytest.mark.parametrize('context', ['archive', 'site-packages'])
def test_non_checkout_skips_before_any_git_or_ancestor_discovery(tmp_path, monkeypatch, context):
    # A parent marker must not make an archive/installed subtree a checkout.
    (tmp_path / '.git').mkdir()
    non_checkout = tmp_path / context
    non_checkout.mkdir()
    git = Mock(side_effect=AssertionError('non-checkout context must never invoke Git'))
    monkeypatch.setattr(subprocess, 'run', git)
    with pytest.raises(pytest.skip.Exception, match='checkout-only catalog parity'):
        _tracked_packaged_names(non_checkout)
    git.assert_not_called()
