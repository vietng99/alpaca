"""The core installer downloads no domain toolchain (t-115; FR-057, FR-059, EC-031, SC-027).

setup/bootstrap.sh installs only the exact pins of requirements.txt into the copy's .venv. A pack's
tools are the pack's own setup: the pack declares them in research.fingerprint.tools and
`alpaca doctor` checks the declaration (alpaca/tests/test_research_doctor.py). Held here:

1. bootstrap.sh names no fetcher or system installer and runs no other setup script;
2. run in a copy with a recording fake python and a trap program for every fetcher on PATH, it
   makes exactly one pip call, for requirements.txt, and calls no fetcher; with --offline and a
   wheelhouse that one call reads only the wheelhouse;
3. every package requirements.txt pins is imported by core code (alpaca/, setup/): a pack's
   dependency added to the core list fails here.

No test touches the network: the fake python records its arguments and installs nothing."""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess

import pytest

from alpaca.tests.conftest import REPO

FETCHERS = ("curl", "wget", "apt", "apt-get", "dnf", "yum", "brew", "npm", "npx", "pnpm", "yarn",
            "cargo", "rustup", "go", "docker", "podman", "snap", "conda", "mamba", "pipx", "uv",
            "pip", "pip3", "git", "sudo", "gem", "nix")

PIP_LINE = '.venv/bin/python -m pip install "${ALPACA_BOOT_PIP_ARGS[@]}" -r requirements.txt'


def _code_lines(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                yield stripped


def _fetch_lines(path):
    bad = []
    for line in _code_lines(path):
        if line == PIP_LINE:
            continue
        if "pip" in line and "install" in line:
            bad.append(line)                         # a second pip install line
        bare = re.sub(r"'[^']*'|\"[^\"]*\"", "''", line)   # drop quoted text (messages, -c code)
        if re.search(r"https?://|urlopen|urlretrieve|install-sqlite-runtime|vendor_spec_kits|"
                     r"git\s+clone|packs/", line):
            bad.append(line)
        for tool in FETCHERS:
            if re.search(r"(^|[\s;|&(`$])%s(\s|$)" % re.escape(tool), bare):
                bad.append(line)
    return bad


def test_bootstrap_text_names_no_fetcher():
    assert not _fetch_lines(os.path.join(REPO, "setup", "bootstrap.sh"))


@pytest.mark.parametrize("added", [
    "curl -fsSL https://example.invalid/tool.tar.gz | tar xz",
    "bash packs/refpack/setup.sh",
    ".venv/bin/python -m pip install -r packs/refpack/repair/requirements.txt",
    "apt-get install -y sometool",
], ids=["curl", "pack-setup", "pack-pip", "apt"])
def test_the_text_scan_catches_a_domain_download(tmp_path, added):
    copy = tmp_path / "bootstrap.sh"
    with open(os.path.join(REPO, "setup", "bootstrap.sh"), encoding="utf-8") as fh:
        copy.write_text(fh.read() + added + "\n", encoding="utf-8")
    assert _fetch_lines(str(copy)), added


def _release_fetch(line):
    """install.sh's one allowed fetch: the shallow clone of the Alpaca release named by $REPO (its
    usage text, its default, the git presence check and the clone line)."""
    return bool((line.startswith("git clone ") and '"$REPO"' in line)
                or re.match(r'REPO="https://[^"\s]+\.git"$', line)
                or line.startswith("command -v git ")
                or line.startswith(("Source (one of", "--repo URL")))


def _install_fetch_lines(path):
    return [line for line in _fetch_lines(path) if not _release_fetch(line)]


def test_install_sh_fetches_only_the_alpaca_release():
    """install.sh, the other core installer, clones an Alpaca release and runs bootstrap.sh in it;
    it fetches nothing else."""
    assert not _install_fetch_lines(os.path.join(REPO, "install.sh"))


@pytest.mark.parametrize("added", [
    "curl -fsSL https://example.invalid/tool.tar.gz -o tool.tgz",
    "git clone -q https://example.invalid/toolchain.git \"$DEST/tools\"",
    "bash packs/refpack/setup.sh",
], ids=["curl", "other-clone", "pack-setup"])
def test_the_install_sh_scan_catches_a_domain_download(tmp_path, added):
    copy = tmp_path / "install.sh"
    with open(os.path.join(REPO, "install.sh"), encoding="utf-8") as fh:
        copy.write_text(fh.read() + added + "\n", encoding="utf-8")
    assert _install_fetch_lines(str(copy)), added


def _run_bootstrap(tmp_path, *args):
    """bootstrap.sh run in a copy with a fake python that records each call and fetches nothing,
    and a trap program for every fetcher first on PATH. (completed process, python calls, trap log)."""
    root = tmp_path / "copy"
    (root / "setup").mkdir(parents=True)
    (root / "bin").mkdir()
    shutil.copy(os.path.join(REPO, "setup", "bootstrap.sh"), root / "setup" / "bootstrap.sh")
    shutil.copy(os.path.join(REPO, "requirements.txt"), root / "requirements.txt")
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), root / "ALPACA-MANIFEST")
    log = tmp_path / "python.log"
    trap_log = tmp_path / "trap.log"
    fake = tmp_path / "fakepython"
    fake.write_text(
        "#!/bin/bash\n"
        "printf '%%s\\n' \"$*\" >> %s\n"
        "case \"$*\" in\n"
        "  *'-m venv .venv'*) mkdir -p .venv/bin && cp \"$0\" .venv/bin/python ;;\n"
        "  *sysconfig*) echo %s ;;\n"
        "  -) cat >/dev/null ;;\n"
        "esac\n"
        "exit 0\n" % (log, tmp_path / "site"))
    fake.chmod(0o755)
    (root / "bin" / "alpaca-python").write_text("#!/bin/sh\nexit 0\n")
    (root / "bin" / "alpaca-python").chmod(0o755)
    trap = tmp_path / "trap"
    trap.mkdir()
    for tool in FETCHERS:
        p = trap / tool
        p.write_text("#!/bin/sh\necho %s \"$@\" >> %s\nexit 1\n" % (tool, trap_log))
        p.chmod(0o755)
    env = {"PATH": "%s:/usr/bin:/bin" % trap, "HOME": str(tmp_path / "home"),
           "ALPACA_PYTHON": str(fake)}
    done = subprocess.run(["/bin/bash", str(root / "setup" / "bootstrap.sh"), *args], cwd=str(root),
                          env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return done, calls, (trap_log.read_text() if trap_log.exists() else "")


def test_bootstrap_makes_one_pip_call_for_requirements_txt(tmp_path):
    done, calls, trapped = _run_bootstrap(tmp_path)
    assert done.returncode == 0, done.stderr
    assert not trapped, trapped
    pip = [c for c in calls if "-m pip" in c]
    assert pip == ["-m pip install --disable-pip-version-check -r requirements.txt"], calls


def test_an_offline_bootstrap_reads_only_the_wheelhouse(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    done, calls, trapped = _run_bootstrap(tmp_path, "--offline", "--wheelhouse", str(wheels))
    assert done.returncode == 0, done.stderr
    assert not trapped, trapped
    pip = [c for c in calls if "-m pip" in c]
    assert pip == ["-m pip install --disable-pip-version-check --no-index --find-links %s "
                   "-r requirements.txt" % wheels], calls


IMPORT_NAME = {"pyyaml": "yaml"}


def _unused_pins(requirements_text):
    names = [re.match(r"[A-Za-z0-9._-]+", l.strip()).group(0).lower()
             for l in requirements_text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    sources = []
    for top in ("alpaca", "setup"):
        for path in glob.glob(os.path.join(REPO, top, "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8", errors="replace") as fh:
                sources.append(fh.read())
    with open(os.path.join(REPO, "setup", "bootstrap.sh"), encoding="utf-8") as fh:
        sources.append(fh.read())
    text = "\n".join(sources)
    return [n for n in names
            if not re.search(r"^\s*(import|from)\s+%s\b" % re.escape(IMPORT_NAME.get(n, n).replace("-", "_")),
                             text, re.M)]


def test_every_core_requirement_is_imported_by_core_code():
    with open(os.path.join(REPO, "requirements.txt"), encoding="utf-8") as fh:
        unused = _unused_pins(fh.read())
    assert not unused, "requirements.txt pins packages core never imports: %s" % unused


def test_a_pack_dependency_in_the_core_list_is_caught():
    with open(os.path.join(REPO, "requirements.txt"), encoding="utf-8") as fh:
        text = fh.read()
    assert _unused_pins(text + "sortedcontainers-t115==2.4.0\n") == ["sortedcontainers-t115"]
