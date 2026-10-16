"""Release tags: vMAJOR.MINOR.PATCH on a git repository, read with `git ls-remote`.

Stdlib only. `alpaca update` (alpaca/update.py) and the release gate in setup/release_checks.py
share this one parser, so the tag that update installs and the tag the gate upgrades from are
chosen by the same rule: exactly `vN.N.N`, compared as integer tuples (v1.10.0 is above v1.9.0).
"""
import os
import re
import subprocess

DEFAULT_REPO = "https://github.com/vietng99/alpaca.git"

_N = r"(0|[1-9][0-9]*)"   # ASCII digits, no leading zero, so a parsed tag always prints back as itself
TAG_RE = re.compile(r"v%s\.%s\.%s" % (_N, _N, _N))
VERSION_RE = re.compile(r"%s\.%s\.%s" % (_N, _N, _N))
_VERSION_LINE = re.compile(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', re.M)
_CHANNEL_LINE = re.compile(r'^CHANNEL = "([a-z]+)"$', re.M)


class Unreachable(Exception):
    """The repository could not be read (no git, no network, bad URL, timeout). str(e) is the reason."""


def parse_tag(name):
    """'v1.3.0' -> (1, 3, 0). Anything else ('v1.3', 'v1.3.0-rc1', '1.3.0', 'main') -> None."""
    got = TAG_RE.fullmatch(name) if isinstance(name, str) else None
    return tuple(int(part) for part in got.groups()) if got else None


def parse_version(text):
    """'1.3.0' -> (1, 3, 0). Anything else -> None."""
    got = VERSION_RE.fullmatch(text) if isinstance(text, str) else None
    return tuple(int(part) for part in got.groups()) if got else None


def tag_of(version):
    """(1, 3, 0) -> 'v1.3.0'."""
    return "v%d.%d.%d" % tuple(version)


def _read(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()
    except (OSError, ValueError):
        return ""


def tree_version(root):
    """The VERSION of the Alpaca tree at `root` ('1.3.0'), or None when it cannot be read."""
    got = _VERSION_LINE.search(_read(root, os.path.join("alpaca", "__init__.py")))
    return got.group(1) if got else None


def tree_channel(root):
    """The CHANNEL of the Alpaca tree at `root` ('dev' or 'release'), or None."""
    got = _CHANNEL_LINE.search(_read(root, os.path.join("alpaca", "_channel.py")))
    return got.group(1) if got else None


def git_env(base=None):
    """`base` (default: the process environment) with every way git or ssh could ask a question off."""
    env = dict(os.environ if base is None else base)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", SSH_ASKPASS="true",
               GIT_SSH_COMMAND="ssh -oBatchMode=yes")
    return env


def tags_from_ls_remote(text):
    """The release tags of `git ls-remote --tags` text: '<sha>\\trefs/tags/<name>' lines, peeled
    ('^{}') lines dropped, only exact vN.N.N names kept; unique, ascending by version."""
    found = {}
    for line in (text or "").splitlines():
        _, _, ref = line.strip().partition("\t")
        if ref.endswith("^{}") or not ref.startswith("refs/tags/"):
            continue
        key = parse_tag(ref[len("refs/tags/"):])
        if key:
            found[key] = ref[len("refs/tags/"):]
    return [found[key] for key in sorted(found)]


def newest(tags):
    """The greatest release tag of `tags`, or None."""
    keyed = [(parse_tag(t), t) for t in tags]
    keyed = [pair for pair in keyed if pair[0]]
    return max(keyed)[1] if keyed else None


def previous(tags, version):
    """The greatest release tag strictly below `version` ('1.3.0' or a tuple), or None."""
    current = parse_version(version) if isinstance(version, str) else tuple(version)
    if not current:
        return None
    below = [(parse_tag(t), t) for t in tags]
    below = [pair for pair in below if pair[0] and pair[0] < current]
    return max(below)[1] if below else None


def ls_remote(repo, timeout=30, run=subprocess.run):
    """The release tags of `repo` (a URL, an ssh address or a path), ascending. Never prompts.
    Raises Unreachable with the reason when git is missing, hangs or fails."""
    argv = ["git", "-c", "credential.helper=", "ls-remote", "--tags", "--refs", "--", repo]
    try:
        done = run(argv, env=git_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=timeout)
    except FileNotFoundError:
        raise Unreachable("git is not installed")
    except subprocess.TimeoutExpired:
        raise Unreachable("git ls-remote %s timed out after %ds" % (repo, timeout))
    if done.returncode != 0:
        lines = [ln.strip() for ln in (done.stderr or "").splitlines() if ln.strip()]
        raise Unreachable("git ls-remote %s failed: %s" % (repo, (lines[-1] if lines else "exit %d" % done.returncode)[:200]))
    return tags_from_ls_remote(done.stdout)
