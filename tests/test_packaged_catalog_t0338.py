"""Package debris never becomes authored configuration; real CLI receiver proof."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Independent oracle: do not derive expected names from production MUTABLE.
EXPECTED = {
    'card-summary.md', 'claude-tools.md', 'compact-owner.md', 'compact.md',
    'conversation-store.md', 'harness-capabilities.md', 'ordinary-instructions.md',
    'owner-store.md', 'plan-mode.md', 'seat-instructions.md', 'systemprompt.md',
    'tool-denied.md', 'tools.md', 'wake-after-compact.md', 'role.md', 'method.md',
}

RECEIVER = r'''
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from litetui import app as app_mod
from litetui import cli, harness, image_viewer, prompt_overrides

root = Path(os.environ['PROBE_ROOT'])
expected = set(json.loads(os.environ['EXPECTED_PROMPTS']))
assert Path(app_mod.__file__).resolve().parent == root / 'copied' / 'litetui'
from hashlib import sha256
print('COPIED_SOURCE_BINDING=' + json.dumps({
    'app_module': str(Path(app_mod.__file__).resolve()),
    'catalog_module': str(Path(prompt_overrides.__file__).resolve()),
    'catalog_sha256': sha256(Path(prompt_overrides.__file__).read_bytes()).hexdigest()}), flush=True)
role = root / 'role.md'
method = root / 'method.md'
role.write_text('# OWNER ROLE\nSYNTHETIC ROLE BODY', encoding='utf-8')
method.write_text('# POLYMATHIC GAMMA\nSYNTHETIC METHOD', encoding='utf-8')
seen = []
saved = {}

def capture(app, **_kwargs):
    home = app._agent_session.memory_root
    assert set(prompt_overrides.MUTABLE) == expected
    assert {p.name for p in (home / 'prompts').glob('*.md')} == expected
    if not seen:
        app._materialise_convo()
        messages = app._request_messages()
        saved.update(home=home, cid=app.convo_id,
                     suffix=app.conversation[0]['content'][len(app.conversation[0]['litetui_prompt_base']):])
        app._append({'role': 'user', 'content': 'SYNTHETIC PRIOR HISTORY'})
    else:
        assert app._agent_session.memory_root == saved['home']
        assert app._cli_convo_id == saved['cid']
        app._system = lambda *_args, **_kwargs: None
        app._render_resumed = lambda *_args, **_kwargs: None
        assert app._resume_cli_conversation()
        app._connect_settled = True
        app._connect_done.set()
        app._gui_connection_success = True
        app._cli_initial_model = None
        app._cli_thinking_level = None
        async def registered():
            return True
        app._register_owned_startup = registered
        asyncio.run(app_mod.LiteTUI._apply_cli_args.__wrapped__(app))
        assert not app._cli_launch_error
        assert app.convo_id == saved['cid'] and app.store.owned
        messages = app._request_messages()
        assert {'role': 'user', 'content': 'SYNTHETIC PRIOR HISTORY'} in messages
        assert messages[0]['content'].endswith(saved['suffix'])
    assert len([m for m in messages if m['role'] == 'system']) == 1
    text = messages[0]['content']
    assert text.count('# OWNER ROLE \u2014 Owner Mode') == 1
    assert text.count('# POLYMATHIC GAMMA') == 1
    assert 'UNSHIPPED BACKUP BODY' not in text and 'UNSHIPPED EXTRA BODY' not in text
    seen.append('fresh' if not seen else 'named-resume')

with patch.object(app_mod.LiteTUI, 'run', capture), patch.object(image_viewer, 'init_image_backend'):
    for selection in (['--create-agent', 'Synthetic', '--agent-id', '11111111-1111-4111-8111-111111111111',
                       '--backend', 'codex', '--model', 'synthetic-model', '--thinking-level', 'high'],
                      ['--agent', 'Synthetic', '--convo', None]):
        if selection[-1] is None:
            selection[-1] = saved['cid']
        # Actual producer process identity is one-shot; restore for each CLI call.
        os.environ.update({harness.SPAWN_IDENTITY_MARKER: '1',
                           'LITEHARNESS_AGENT_ID': '11111111-1111-4111-8111-111111111111',
                           'LITEHARNESS_AGENT_NAME': 'Synthetic', 'LITEHARNESS_TIER': 'leader'})
        sys.argv = ['litetui', '--cwd', str(root / 'workspace'), *selection,
                    '--role-prompt-file', str(role), '--cognitive-file', str(method)]
        cli.main()  # actual finally releases the store and HOME lease
assert seen == ['fresh', 'named-resume']
print('PACKAGED_RECEIVER_PROOF=' + json.dumps({'phases': seen, 'catalog': sorted(expected),
      'one_actual_role': True, 'one_method': True, 'old_cid_history_and_suffix_preserved': True,
      'module': str(Path(app_mod.__file__).resolve())}))
'''


def test_packaged_manifest_ignores_debris_before_real_fresh_and_named_resume(tmp_path):
    from litetui import app as app_mod

    package = Path(app_mod.__file__).resolve().parent
    copied = tmp_path / 'copied'
    shutil.copytree(package, copied / 'litetui', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.lock'))
    folder = copied / 'litetui' / 'prompts'
    debris = {'systemprompt.bak.md': 'UNSHIPPED BACKUP BODY',
              'systemprompt.md.lock': 'SYNTHETIC LOCK', 'extra.md': 'UNSHIPPED EXTRA BODY'}
    for name, text in debris.items():
        (folder / name).write_text(text, encoding='utf-8')
    (tmp_path / 'workspace').mkdir()
    lore = tmp_path / 'lore.md'
    lore.write_text('SYNTHETIC FROZEN LORE', encoding='utf-8')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('LITETUI_', 'LITEHARNESS_', 'LITESUITE_'))
           and key not in ('CLAUDECODE', 'CODEX_SANDBOX', 'CODEX_SANDBOX_NETWORK_DISABLED')}
    env.update(PYTHONPATH=str(copied), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               PROBE_ROOT=str(tmp_path), EXPECTED_PROMPTS=json.dumps(sorted(EXPECTED)),
               LITETUI_DATA_ROOT=str(tmp_path / 'data'), LITETUI_NO_HARNESS='1',
               HOME=str(tmp_path / 'profile'), USERPROFILE=str(tmp_path / 'profile'),
               LITEHARNESS_LORE_FILE=str(lore), CLAUDE_SKILL_DIR=str(tmp_path / 'skills'))
    result = subprocess.run([sys.executable, '-c', RECEIVER], env=env, cwd=tmp_path / 'workspace',
                            capture_output=True, text=True, encoding='utf-8', timeout=90, check=False)
    assert result.returncode == 0, (result.stderr[-5000:], result.stdout[-2000:])
    binding = json.loads(result.stdout.split('COPIED_SOURCE_BINDING=', 1)[1].splitlines()[0])
    assert binding['catalog_sha256'] == hashlib.sha256((package / 'prompt_overrides.py').read_bytes()).hexdigest()
    print('COPIED_SOURCE_BINDING=' + json.dumps(binding, sort_keys=True))
    proof = json.loads(result.stdout.split('PACKAGED_RECEIVER_PROOF=', 1)[1])
    print('PACKAGED_RECEIVER_PROOF=' + json.dumps(proof, sort_keys=True))
    assert proof['phases'] == ['fresh', 'named-resume']
    assert set(proof['catalog']) == EXPECTED
    for name, text in debris.items():
        assert (folder / name).read_text(encoding='utf-8') == text
    # Ignoring install debris must not relax explicit unknown HOME overrides.
    from litetui import prompt_overrides
    explicit_home = tmp_path / 'explicit-home'
    (explicit_home / 'prompts').mkdir(parents=True)
    (explicit_home / 'prompts' / 'extra.md').write_text('UNKNOWN HOME BODY', encoding='utf-8')
    with pytest.raises(prompt_overrides.PromptError, match='override refused'):
        prompt_overrides.validate_layer(explicit_home)
