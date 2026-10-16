"""Repositories for the first-chat rehearsal and the look tests (op-062 t-656).

`build_team_repo(dest, today)` makes a small Python project that looks like a team's: tests, CI, hooks,
CODEOWNERS, a CONTRIBUTING.md that names the branch rule, an AGENTS.md, and four people who committed
3, 9, 17 and 26 days before `today`. Dates are set from `today`, so the facts hold on any day. The
repository has no remote: a look reads it as a local folder and an attach clones it as a local path.

`mirror_qtrm64(dest, probe_clone)` copies a clone of the real QTRM64 repository (for the P1 persona)
as a bare mirror, so no network or token is needed during a rehearsal.

`build_repo(dest, files, commits)` is the general builder the two above and the look tests use.
"""
from __future__ import annotations

import datetime
import os
import subprocess

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_CONFIG_COUNT",
         "GIT_CONFIG_PARAMETERS")


def git(path, *args, env=None, check=True):
    full = {k: v for k, v in os.environ.items() if k not in _DROP}
    full.update(GIT_ENV)
    full.update(env or {})
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, env=full)
    if check and done.returncode != 0:
        raise AssertionError("git %s failed in %s: %s" % (" ".join(args), path, done.stderr.strip()))
    return done.stdout.strip()


def _stamp(day, hour=12):
    return datetime.datetime.combine(day, datetime.time(hour, 0), tzinfo=datetime.timezone.utc).isoformat()


def build_repo(dest, commits, *, branch="main", bare=False):
    """Make a repository at `dest`. `commits` is a list of {"files": {path: text}, "author": name, "day": date,
    "message": text}; each is committed in order. Returns `dest` as a string."""
    dest = str(dest)
    os.makedirs(dest, exist_ok=True)
    git(dest, "init", "-q", "-b", branch)
    for c in commits:
        for rel, text in c["files"].items():
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(text)
            if rel.startswith(".husky/"):
                os.chmod(target, 0o755)
        git(dest, "add", "-A")
        who = c.get("author", "Ada Author")
        mail = "%s@example.org" % who.split()[0].lower()
        stamp = _stamp(c["day"])
        git(dest, "commit", "-q", "-m", c.get("message", "change"),
            env={"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": mail, "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": mail,
                 "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
    return dest


TEAM_FILES = {
    "pyproject.toml": "[project]\nname = \"shop-reports\"\nversion = \"0.3.0\"\n\n[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
    "shop_reports/__init__.py": "",
    "shop_reports/cli.py": "import sys\n\n\ndef report(rows):\n    return \"\\n\".join(\" | \".join(map(str, r)) for r in rows)\n\n\n"
                           "def main(argv=None):\n    print(report([(\"apples\", 3), (\"pears\", 5)]))\n    return 0\n\n\n"
                           "if __name__ == \"__main__\":\n    sys.exit(main())\n",
    "tests/test_cli.py": "from shop_reports.cli import report\n\n\ndef test_report_joins_columns():\n    assert report([(\"a\", 1)]) == \"a | 1\"\n",
    ".github/workflows/ci.yml": "name: ci\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                                "      - uses: actions/checkout@v4\n      - run: python -m pytest\n",
    "CODEOWNERS": "* @hana\n",
    "CONTRIBUTING.md": "# Contributing\n\nOpen a pull request against main.\n\nBranches are named `feature/<ticket>`, for example "
                       "`feature/SHOP-12`.\n\nCI must be green before review.\n",
    "AGENTS.md": "# Notes for agents\n\nRun `python -m pytest` before you push.\n",
    ".husky/pre-push": "#!/bin/sh\npython -m pytest -q\n",
    "README.md": "# shop-reports\n\nA small command that prints the weekly shop report.\n",
}


def build_team_repo(dest, today):
    """A repository four people work in; commits 26, 17, 9 and 3 days before `today` (a datetime.date)."""
    people = [("Hana Lead", 26), ("Minh Dev", 17), ("Tuan Dev", 9), ("Lan Dev", 3)]
    commits = [{"files": TEAM_FILES, "author": people[0][0], "day": today - datetime.timedelta(days=people[0][1]),
                "message": "first version"}]
    for i, (who, days) in enumerate(people[1:], 1):
        commits.append({"files": {"notes/%d.md" % i: "notes %d\n" % i}, "author": who,
                        "day": today - datetime.timedelta(days=days), "message": "work by %s" % who})
    return build_repo(dest, commits)


def mirror_qtrm64(dest, probe_clone):
    """A bare mirror of a local clone of QTRM64, for the P1 persona. No network."""
    dest = str(dest)
    done = subprocess.run(["git", "clone", "--mirror", "--no-local", "-q", str(probe_clone), dest], capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV})
    if done.returncode != 0:
        raise AssertionError("git clone --mirror failed: %s" % done.stderr.strip())
    return dest
