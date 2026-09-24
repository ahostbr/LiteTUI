"""Runtime recursion guard, not a sandbox against arbitrary shell execution.

The launcher injects depth; model tool arguments cannot lower it. A process
with unrestricted shell access can start other programs, so this bounds the
managed spawn interface only, not every process an autonomous agent can create.
"""
import os
from litetui.agent_launcher import LaunchBlocked


def require_root_launcher():
    if os.environ.get('LITETUI_AGENT_DEPTH', '0') != '0':
        raise LaunchBlocked('Managed child spawn depth budget exhausted or invalid')
    return 0
