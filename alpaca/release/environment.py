"""Project commands never inherit publication credentials or parent gate bypasses."""
import os
from pathlib import Path
import sys


def command_environment():
    env = dict(os.environ)
    for key in ('PUBLIC_REPO_TOKEN', 'ALPACA_RELEASE_TERMS', 'GH_TOKEN', 'GITHUB_TOKEN', 'GIT_ASKPASS',
                'GIT_CONFIG_COUNT', 'ALPACA_ROOT', 'ALPACA_SESSION_ID', 'PYTHONPATH',
                'ALPACA_REGRESSION_SUITE_ACTIVE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PROJECT_DIR'):
        env.pop(key, None)
    for key in list(env):
        if key.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')):
            env.pop(key)
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
    return env
