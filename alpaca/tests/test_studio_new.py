"""t-479: new projects and upgrades carry the Studio.

`alpaca new` copies the mechanism class of ALPACA-MANIFEST, alpaca/web included, so a fresh project
has the full Studio. Here a project made by `alpaca new` (in the system temp folder, never under a
home folder: the burn board would print an account name from a /home/<person>/ path) is served with
`alpaca serve` from its own copy, and headless Chrome (studio/pages.mjs over studio/cdp.mjs) opens
every generic page of its registry on the empty record: no console error, no uncaught exception, no
error view, no loader left, and each page shows its content or an empty state that says so.

`alpaca upgrade` replaces alpaca/web with the source's copy and leaves a project's profile package
alone (it sits outside every mechanism path; project.yaml is preserved). The upgrade test starts from
a new project whose Studio is made older (a changed file, a missing file, a file the release dropped),
adds the sample profile with its own page, upgrades from a release-shaped copy of this tree and checks
both the refreshed bytes and the page in the browser.

te/web, the Studio of the harness Alpaca grew out of, is no mechanism path: it never ships."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from contextlib import redirect_stdout

import pytest

from alpaca import new
from alpaca.tests import studio_fixture, studio_shots

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PAGES = os.path.join(HERE, "studio", "pages.mjs")
NAME = "Fresh Studio"
# the empty state (or the content) each page must show on a record with no work in it
EMPTY = {
    "overview": "No open operations",
    "roadmap": "No tasks have been recorded yet",
    "cockpit": "No unfinished task is recorded",
    "inflight": "Nothing is in flight",
    "research": "No research run is recorded",
    "work": "0 tasks",
    "activity": "Timeline",
    "messages": "Agent reports and handoffs appear here when they are recorded",
    "library": "README.md",
    "sessions": "No model response measurements are available",
    "vcs": "No commits in this window",
    "roadmap-mission": "Starter map",
}


def _work(tmp_path_factory, name):
    work = str(tmp_path_factory.mktemp(name))
    if os.path.realpath(work).startswith(("/home/", "/Users/")):
        pytest.skip("run with --basetemp outside a home folder: the burn board names an account from its path")
    return work


def _tools():
    node, chrome, why = studio_shots.tools()
    if not node:
        pytest.skip(why)
    return node, chrome


def _env(work):
    """The host's registry, tunnel config, Claude home and live session never reach the project."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ALPACA_", "CLAUDE_", "CODEX_")) and k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    home = os.path.join(work, "home")
    os.makedirs(home, exist_ok=True)
    env.update({"HOME": home, "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"),
                "ALPACA_WORKSPACES": os.path.join(work, "workspaces.json"),
                "ALPACA_CLOUDFLARED_CONFIG": os.path.join(work, "no-cloudflared.yml"),
                "ALPACA_SERVE_RELOAD": "0", "ALPACA_NO_AUTOSERVE": "1", "ALPACA_DISK_SWEEP": "0",
                "ALPACA_RECORD_SKIP_ROOT": REPO, "TZ": "UTC", "LANG": "C.UTF-8",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    return env


def _new(work, folder, *extra):
    """`alpaca new` from this tree into <work>/<folder>, as a person runs it (minus the .venv and the
    detached hub, which the test starts itself)."""
    root = os.path.join(work, folder)
    run = subprocess.run([sys.executable, "-m", "alpaca", "new", "--name", NAME, "--dir", root,
                          "--who", "Pat Doe:owner", "--yes", "--no-bootstrap", "--no-serve", *extra],
                         cwd=REPO, env=_env(work), capture_output=True, text=True, timeout=300)
    assert run.returncode == 0 and "doctor 0 ERROR line(s)" in run.stdout, run.stdout + run.stderr
    return root


@contextlib.contextmanager
def _serve(root, work, wait=90):
    """`alpaca serve` run by the project's own copy (python -m alpaca from its root) on a free port.
    A fresh project has no web login, so nothing signs in. Ready once the first analytics fold is done
    and two overview reads agree."""
    port = studio_shots.free_port()
    log_path = os.path.join(work, "serve-%s.log" % os.path.basename(root))
    log = open(log_path, "ab")
    proc = subprocess.Popen([sys.executable, "-m", "alpaca", "serve", "--port", str(port), "--keep"], cwd=root,
                            env=_env(work), stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    base = "http://127.0.0.1:%d" % port
    try:
        end = time.monotonic() + wait
        while True:
            if proc.poll() is not None:
                raise RuntimeError("alpaca serve exited %s; see %s" % (proc.returncode, log_path))
            if time.monotonic() > end:
                raise RuntimeError("the project server was not ready in %d s; see %s" % (wait, log_path))
            try:
                health = json.loads(urllib.request.urlopen(base + "/health.json", timeout=10).read())
                if (health.get("projection") or {}).get("state") == "fresh":
                    first = urllib.request.urlopen(base + "/hub/overview.json", timeout=30).read()
                    time.sleep(1.0)
                    if urllib.request.urlopen(base + "/hub/overview.json", timeout=30).read() == first:
                        break
            except OSError:          # starting up: refused, or 202 while the first fold runs
                pass
            time.sleep(0.5)
        yield base
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(10)
        log.close()


def _visit(base, views, work):
    """studio/pages.mjs over `views`; one dict per view."""
    node, chrome = _tools()
    run = subprocess.run([node, PAGES, "--base", base, "--views", json.dumps(views),
                          "--profile", os.path.join(work, "chrome")],
                         capture_output=True, text=True, timeout=600, env=dict(os.environ, CHROME=chrome))
    rows = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    for row in rows:
        print(json.dumps(row))
    assert not [r for r in rows if r["view"] == "driver"], run.stdout + run.stderr
    assert [r["view"] for r in rows] == [v[0] for v in views], run.stdout + run.stderr
    return {r["view"]: r for r in rows}


def _generic_views(root):
    """Every page of the project's own registry that needs no profile, in nav order, plus the
    roadmap's mission view."""
    with open(os.path.join(root, "alpaca", "web", "pages.json"), encoding="utf-8") as fh:
        pages = json.load(fh)["pages"]
    ids = [p["id"] for p in sorted(pages, key=lambda p: p["nav"]) if p.get("gate") != "profile"]
    return ids, [[pid, "/hub/#" + pid] for pid in ids] + [["roadmap-mission", "/hub/#roadmap?view=mission"]]


def _clean(row):
    """The page settled with no error view, no loader, no console error and something in its main area."""
    assert row["settled"], row
    assert not row["loader"], row
    assert not row["error"], row
    assert row["problems"] == [], row
    assert row["heading"] and row["text"] > 0, row


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    _tools()
    work = _work(tmp_path_factory, "studio-new")
    return work, _new(work, "fresh-studio")


# ------------------------------------------------------------------ alpaca new, empty record
def test_a_new_project_opens_every_generic_page_on_its_empty_record(fresh):
    work, root = fresh
    ids, views = _generic_views(root)
    assert set(EMPTY) == set(ids) | {"roadmap-mission"}, "a page joined the registry: name its empty state here"
    views.append(["hub", "/", "document.querySelectorAll('.tile').length>0 && !document.querySelector('.tile .skeleton, .tile-live.checking')"])
    with _serve(root, work) as base:
        overview = json.loads(urllib.request.urlopen(base + "/hub/overview.json", timeout=30).read())
        seen = _visit(base, views, work)
    assert overview["project"]["name"] == NAME
    for pid in ids + ["roadmap-mission"]:
        row = seen[pid]
        _clean(row)
        assert EMPTY[pid] in row["main"], (pid, row["main"])
        # the nav lists every generic page and no profile-gated one
        assert row["nav"].split() == ["#" + i for i in ids], row["nav"]
    hub = seen["hub"]
    assert hub["settled"] and hub["problems"] == [] and NAME in hub["main"], hub


def test_a_project_with_no_git_repository_says_so_on_version_control(tmp_path_factory):
    _tools()
    work = _work(tmp_path_factory, "studio-nogit")
    root = _new(work, "no-git", "--no-git")
    assert not os.path.exists(os.path.join(root, ".git"))
    with _serve(root, work) as base:
        data = json.loads(urllib.request.urlopen(base + "/hub/vcs.json?days=7", timeout=30).read())
        row = _visit(base, [["vcs", "/hub/#vcs"]], work)["vcs"]
    assert data["repository"] is False
    _clean(row)
    assert "No git repository" in row["main"], row


# ------------------------------------------------------------------ alpaca upgrade
def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _tree(root):
    out = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            path = os.path.join(base, name)
            out[os.path.relpath(path, root)] = _sha(path)
    return out


def _release(path):
    """A release-shaped copy of this tree, built the way alpaca new builds a copy: the shipped file
    list, the release templates and a MANIFEST.json of its own."""
    new.copy_tree(REPO, path)
    new.apply_release_overlays(REPO, path)
    with redirect_stdout(io.StringIO()):
        assert new._gen_manifest(path).write(path) == 0
    return path


def test_upgrade_refreshes_the_studio_and_keeps_the_project_page(tmp_path_factory):
    _tools()
    work = _work(tmp_path_factory, "studio-upgrade")
    live = _new(work, "upgraded")
    web = os.path.join(live, "alpaca", "web")
    # an older Studio: one file with other bytes, one file it did not have yet, one the release dropped
    with open(os.path.join(web, "hub.css"), "a", encoding="utf-8") as fh:
        fh.write("\n/* an older Studio */\n")
    os.remove(os.path.join(web, "inflight.js"))
    with open(os.path.join(web, "retired.js"), "w", encoding="utf-8") as fh:
        fh.write("// a page an older Studio had\n")
    dotted = studio_fixture.add_sample_profile(live)
    pkg = os.path.join(live, studio_fixture.SAMPLE_PACKAGE)
    kept = _tree(pkg)
    yaml_before = open(os.path.join(live, "project.yaml"), encoding="utf-8").read()
    record = os.path.join(live, ".alpaca", "alpaca.db")
    assert os.path.isfile(record)

    source = _release(os.path.join(work, "release"))
    run = subprocess.run([sys.executable, "-m", "alpaca", "upgrade", "--source", source], cwd=live, env=_env(work),
                         capture_output=True, text=True, timeout=300)
    print(run.stdout + run.stderr)
    assert run.returncode == 0 and "PASS" in run.stdout, run.stdout + run.stderr

    # the Studio is the release's, byte for byte
    assert _tree(web) == _tree(os.path.join(source, "alpaca", "web"))
    assert _sha(os.path.join(web, "hub.css")) == _sha(os.path.join(REPO, "alpaca", "web", "hub.css"))
    assert os.path.isfile(os.path.join(web, "inflight.js")) and not os.path.exists(os.path.join(web, "retired.js"))
    # the project's own profile package, its project.yaml and its record stay
    assert _tree(pkg) == kept
    assert open(os.path.join(live, "project.yaml"), encoding="utf-8").read() == yaml_before
    assert ("profile: %s" % dotted) in yaml_before and os.path.isfile(record)

    with _serve(live, work) as base:
        seen = _visit(base, [["bench", "/hub/#bench", "!!document.querySelector('#main .bench-page li')"],
                             ["cockpit", "/hub/#cockpit"]], work)
    bench = seen["bench"]
    _clean(bench)
    assert "Bench A: 21.5 C" in bench["main"], bench
    assert "#bench" in bench["nav"].split() and bench["title"].startswith("Bench |"), bench
    _clean(seen["cockpit"])


# ------------------------------------------------------------------ what ships
def test_te_web_is_never_shipped_and_the_baseline_ships_only_with_the_self_tests():
    """alpaca new copies, and setup/ship.py stages, the mechanism class of ALPACA-MANIFEST as
    setup/gen_manifest.py walks it; the release overlays only replace files inside it. te/ (and so
    te/web) is not in it. The Studio baseline images live under alpaca/tests/, which ships because
    the manifest ships the self-tests (a new project can rerun the baseline); without them it would
    not ship either."""
    gm = new._gen_manifest(REPO)
    manifest, walk_errors, read_errors = gm.build(REPO)
    assert not walk_errors and not read_errors
    shipped = set(manifest["files"])
    assert shipped and "alpaca/web/hub.html" in shipped
    assert not [rel for rel in shipped if rel == "te" or rel.startswith("te/")]
    mechanism = [m.rstrip("/") for m in gm.read_classes(REPO)["mechanism"]]
    assert not [m for m in mechanism if m == "te" or m.startswith("te/")]
    import yaml
    with open(os.path.join(REPO, "project.yaml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    overlays = ((doc.get("release") or {}).get("overlays") or {}) if isinstance(doc, dict) else {}
    assert not [t for t in overlays if str(t).startswith("te/")]
    baseline = [rel for rel in shipped if rel.startswith("alpaca/tests/baseline/")]
    if not any(rel.startswith("alpaca/tests/test_") for rel in shipped):
        assert not baseline
