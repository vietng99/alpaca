"""Entry point used by generated workflows: project tests only.

Hosted jobs never read the private term policy, never build or check a release candidate and
never hold publication credentials. The real-term scan and publication run on the owner's
machine after an exact-commit approval (see docs/release.md).
"""
import argparse
from pathlib import Path
import shlex
import subprocess
import yaml
from .checks import run_command


#: The whole suite on a hosted runner. It ran 23 minutes on 2026-10-03 and passed 60 minutes at
#: 96% on 2026-10-10 (op-008 t-571); a hosted job itself stops at 6 hours.
TEST_TIMEOUT_S = 3 * 3600


def test(root):
    # Public distribution configuration may intentionally omit private release settings.
    config = yaml.safe_load((root / 'project.yaml').read_text())
    command = config.get('commands', {}).get('test')
    if not isinstance(command, str) or not command.strip():
        raise ValueError('project has no test command')
    log = root / '.alpaca/releases/ci-test.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    code, _ = run_command(shlex.split(command), root, TEST_TIMEOUT_S, log)
    print(log.read_text(errors='replace'))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['test'])
    args = parser.parse_args()
    try:
        return test(Path.cwd().resolve())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('release CI BLOCKED: ' + (str(exc) if type(exc) is ValueError else type(exc).__name__))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
