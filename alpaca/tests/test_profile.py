"""The domain seam (alpaca/profile.py): the empty profile hides every domain surface and returns empty
values; a profile named in project.yaml shows its stages, cards and routes; and no generic module
reaches a domain package except through alpaca.profile."""
import ast
import base64
import http.server
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from alpaca import db, profile

ALPACA_DIR = Path(__file__).resolve().parents[1]
WEB = ALPACA_DIR / "web"


# ------------------------------------------------------------------------------------ helpers
def _hook_args(name, root, conn):
    """Plausible arguments for every hook, so the empty profile is called the way the call
    sites call it."""
    return {
        "stages": (), "verbs": (), "events": (), "summarize": ({"kind": "x", "data": {}},),
        "next_action": (root,), "boot_lines": (root,), "doctor_checks": (root, conn),
        "refresh": (root, "s-1"), "evidence_file": (root, "receipt", "a" * 32),
        "proof_lines": ([],), "failed_runs": (root, None, None), "paths": (),
        "stage_rows": (conn,), "metrics": (conn,), "check": (root,), "runs": (root,),
        "acceptance": (root, conn, [], "now"), "capture": (root, conn), "routes": (),
        "live_revision": (root,), "web": (),
    }[name]


def _hooks():
    return sorted(name for name, fn in inspect.getmembers(profile.Profile, inspect.isfunction)
                  if not name.startswith("_"))


def _is_empty(value):
    if value is None or value == "" or value == () or value == [] or value == {}:
        return True
    if isinstance(value, dict):            # runs(): the empty shape of its payload
        return all(_is_empty(v) or v == 0 for v in value.values())
    return False


FAKE_PROFILE = '''
from alpaca import profile

HERE = __file__.rsplit("/", 1)[0]


class Fake(profile.Profile):
    name = "fakedom"

    def stages(self):
        return ("alpha", "beta")

    def next_action(self, root):
        return "fakedom: run stage alpha"

    def doctor_checks(self, root, conn):
        return [("fakedom-check", "ok", "fake domain checked")]

    def check(self, root):
        return [{"stage": "alpha", "verdict": "PASS", "reason": "ok", "receipt_id": "a" * 32,
                 "recorded_status": "PASS"},
                {"stage": "beta", "verdict": "BLOCKED", "reason": "no run yet", "receipt_id": None}]

    def runs(self, root):
        return {"items": [{"id": "b" * 32, "job_id": "b" * 32, "verdict": "PASS", "stage": "alpha",
                           "stages": [{"stage": "alpha", "receipt_id": "a" * 32}],
                           "started_at": 1.0}], "total": 1, "errors": []}

    def routes(self):
        return {"/hub/fake.json": lambda root, query: ({"fake": True}, 200)}

    def web(self):
        return {"pages": ["runs"], "stage_names": {"alpha": "Alpha stage"},
                "cards": [{"id": "demo", "title": "Demo", "module": "demo.js"}],
                "assets": {"demo.js": HERE + "/demo.js"}}


PROFILE = Fake
'''

DEMO_CARD = ("export function render(o){return '<section class=\"panel\" id=\"fake-card\">'"
             "+(o.profile.name)+' card</section>';}\n")


def _write_profile(root: Path) -> str:
    """A fake domain package under the project root, named uniquely so no other test's import
    shadows it. Returns the dotted module path."""
    pkg = "fakedom_%s" % uuid.uuid4().hex[:10]
    (root / pkg).mkdir()
    (root / pkg / "__init__.py").write_text("", encoding="utf-8")
    (root / pkg / "profile.py").write_text(FAKE_PROFILE, encoding="utf-8")
    (root / pkg / "demo.js").write_text(DEMO_CARD, encoding="utf-8")
    return pkg + ".profile"


@pytest.fixture
def fake(project):
    """A project whose project.yaml names the fake profile."""
    root = Path(project)
    dotted = _write_profile(root)
    (root / "project.yaml").write_text("name: fake\nprofile: %s\n" % dotted, encoding="utf-8")
    yield root
    sys.modules.pop(dotted, None)
    sys.modules.pop(dotted.split(".")[0], None)
    while str(root) in sys.path:
        sys.path.remove(str(root))


# -------------------------------------------------------------- (a) the empty profile is empty
def test_no_profile_key_loads_the_empty_profile(project):
    assert profile.load(project) is profile.EMPTY
    assert profile.error(project) is None
    Path(project, "project.yaml").write_text("name: plain\n", encoding="utf-8")
    assert profile.load(project) is profile.EMPTY
    assert profile.load(None) is profile.EMPTY


def test_every_hook_of_the_empty_profile_returns_an_empty_value(project):
    conn = db.connect(project)
    hooks = _hooks()
    assert len(hooks) >= 20
    for name in hooks:
        value = getattr(profile.EMPTY, name)(*_hook_args(name, project, conn))
        assert _is_empty(value), (name, value)
    assert profile.EMPTY.name == ""


def test_every_hook_names_its_call_site():
    for name in _hooks():
        doc = inspect.getdoc(getattr(profile.Profile, name)) or ""
        assert re.search(r"alpaca/[a-z_/]+\.py", doc), name


def test_a_profile_that_fails_to_import_falls_back_and_is_reported(project):
    from alpaca import doctor
    Path(project, "project.yaml").write_text("name: x\nprofile: no_such_domain.profile\n",
                                             encoding="utf-8")
    assert profile.load(project) is profile.EMPTY
    assert "no_such_domain.profile" in profile.error(project)
    db.connect(project)
    findings = {f["name"]: f for f in doctor.checks(project)}
    assert findings["profile"]["level"] == "error"


def test_the_empty_profile_hides_domain_data_on_the_hub(project):
    from alpaca import hub
    db.connect(project)
    view = hub.overview(project)
    assert view["profile"] == {"name": "", "pages": [], "cards": [], "stages": [],
                               "stage_names": {}}
    assert view["stages"] == [] and view["items"] == [] and view["active_jobs"] == []
    assert view["capture"]["status"] == "ok" and view["capture"]["profile"] == {}
    assert hub.runs(project) == {"items": [], "total": 0, "errors": []}
    assert hub.acceptance_health(project)["status"] == "unavailable"


def test_the_empty_profile_leaves_the_payload_metrics_and_stages_empty(project):
    from alpaca import export
    conn = db.connect(project)
    data = export.payload(conn, root=project)
    assert data["metrics"] == {}
    assert data["pulse"]["now"]["stages"] == []


def test_without_a_profile_no_task_contract_can_name_a_stage(project):
    from alpaca import taskcontract
    with pytest.raises(ValueError, match="no profile declares stages"):
        taskcontract.check({"done_bar": ["x"], "stage": "alpha"})
    assert taskcontract.check({"done_bar": ["x"]})["stage"] is None


def test_doctor_runs_on_a_fresh_project_without_a_profile(project):
    from alpaca import doctor
    names = {f["name"] for f in doctor.checks(project)}
    assert "profile" not in names and "profile-checks" not in names


# ------------------------------------------------------- (b) a profile named in project.yaml
def test_project_yaml_names_the_profile_and_its_hooks_reach_the_call_sites(fake):
    from alpaca import doctor, hub, pad, taskcontract
    root = str(fake)
    prof = profile.load(root)
    assert prof.name == "fakedom" and profile.error(root) is None
    conn = db.connect(root)
    # stages reach the --stage flag
    task_check = taskcontract.check({"done_bar": ["x"], "stage": "alpha"}, prof.stages())
    assert task_check["stage"] == "alpha"
    with pytest.raises(ValueError, match="alpha, beta"):
        taskcontract.check({"done_bar": ["x"], "stage": "gamma"}, prof.stages())
    # the pad, the doctor and the hub read the profile
    assert pad.next_action(root, conn=conn) == "fakedom: run stage alpha"
    findings = {f["name"]: f for f in doctor.checks(root)}
    assert findings["fakedom-check"]["level"] == "ok"
    view = hub.overview(root)
    assert view["profile"]["name"] == "fakedom"
    assert view["profile"]["pages"] == ["runs"]
    assert view["profile"]["stages"] == ["alpha", "beta"]
    assert view["profile"]["stage_names"] == {"alpha": "Alpha stage"}
    assert view["profile"]["cards"] == [{"id": "demo", "title": "Demo",
                                         "module": "/hub/assets/profile/demo.js"}]
    assert [s["stage"] for s in view["stages"]] == ["alpha", "beta"]
    assert view["stages"][0]["recorded_status"] == "PASS"
    assert view["acceptance"]["status"] == "BLOCKED"
    assert hub.runs(root)["total"] == 1


def test_a_profile_check_that_raises_blocks_every_stage(fake, monkeypatch):
    from alpaca import hub
    root = str(fake)
    prof = profile.load(root)

    def broken(_root):
        raise OSError("tool missing")
    monkeypatch.setattr(prof._raw, "check", broken)
    rows, error, _ = hub._checked(Path(root))
    assert [r["stage"] for r in rows] == ["alpha", "beta"]
    assert all(r["verdict"] == "BLOCKED" for r in rows) and "tool missing" in error


def test_a_task_contract_records_a_profile_stage(fake):
    from alpaca import taskcontract
    root = str(fake)
    conn = db.connect(root)
    db.append_event(conn, session="s", actor="t", kind="op-open", op="op-1", data={})
    conn.execute("INSERT INTO ops(id,intent,status,opened) VALUES('op-1','x','open','now')")
    conn.execute("INSERT INTO tasks(id,op,statement,status) VALUES('t-1','op-1','x','open')")
    conn.commit()
    taskcontract.record(conn, "t-1", {"done_bar": ["x"], "stage": "beta"}, session="s", actor="t")
    assert taskcontract.latest(conn)["t-1"]["stage"] == "beta"
    with pytest.raises(ValueError):
        taskcontract.record(conn, "t-1", {"done_bar": ["x"], "stage": "gamma"}, session="s", actor="t")


@pytest.fixture
def served(fake):
    from alpaca import serve
    root = str(fake)
    live = serve.Live(root)
    live.refresh(fold=False)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, root))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    try:
        r = urllib.request.urlopen(url, timeout=15)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.read()


def test_a_profile_serves_its_routes_and_card_assets(served):
    status, body = _get(served + "/hub/fake.json")
    assert status == 200 and json.loads(body) == {"fake": True}
    status, body = _get(served + "/hub/assets/profile/demo.js")
    assert status == 200 and b"fake-card" in body
    assert _get(served + "/hub/assets/profile/other.js")[0] == 404
    assert _get(served + "/hub/assets/profile/../profile.py")[0] == 404
    assert _get(served + "/hub/other.json")[0] == 404


def test_without_a_profile_domain_routes_do_not_exist(project):
    from alpaca import serve
    live = serve.Live(project)
    live.refresh(fold=False)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        for path in ("/hub/logscan.json?receipt=" + "a" * 32, "/hub/contract.json",
                     "/hub/log.json?receipt=" + "a" * 32, "/live/job.json", "/flow/job.json",
                     "/hub/assets/mission.js"):
            assert _get(url + path)[0] == 404, path
        status, body = _get(url + "/hub/runs.json")
        assert status == 200 and json.loads(body)["items"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------ the web pages hide or show the panels
NODE = shutil.which("node")

RUNNER = textwrap.dedent('''
    globalThis.document = {getElementById: () => null, head: {appendChild() {}},
                           createElement: () => ({}), addEventListener() {}};
    globalThis.fetch = () => new Promise(() => {});
    const cockpit = await import(process.argv[2]);
    const o = JSON.parse(process.argv[3]);
    const cards = [];
    if (process.argv[4]) { const m = await import(process.argv[4]); cards.push({id: 'demo', render: m.render}); }
    cockpit.setStageNames(o.profile.stage_names);
    const html = cockpit.renderCockpit(o, {items: o.runs || []}, {items: []}, {cards});
    process.stdout.write(html);
    process.exit(0);
''')


def _cockpit_html(tmp_path, overview, card=None):
    runner = tmp_path / "run-cockpit.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    args = [NODE, str(runner), (WEB / "cockpit.js").as_uri(), json.dumps(overview)]
    if card:
        args.append(Path(card).as_uri())
    out = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _overview(prof, **extra):
    base = {"profile": prof, "tasks": [], "ops": [], "items": [], "stages": [], "active_jobs": [],
            "counts": {"items_pass": 0}, "capture": {"status": "ok", "errors": []}}
    base.update(extra)
    return base


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_cockpit_hides_domain_cards_without_a_profile(tmp_path):
    html = _cockpit_html(tmp_path, _overview({"name": "", "pages": [], "cards": [], "stages": [],
                                              "stage_names": {}}))
    assert "Cockpit" in html
    for hidden in ("active runs", "Execution", "outcomes need attention", "Active run",
                   "Inspect logs", 'href="#runs"'):
        assert hidden not in html, hidden


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_cockpit_shows_a_profiles_pages_and_cards(fake, tmp_path):
    prof = profile.web_meta(str(fake))
    card = Path(profile.asset(str(fake), "demo.js"))
    items = [{"id": "o-1", "status": "BLOCKED", "stage": "beta"}]
    html = _cockpit_html(tmp_path, _overview(prof, items=items,
                                             stages=[{"stage": "beta", "status": "BLOCKED",
                                                      "reason": "no run yet"}]), card)
    assert "active runs" in html and "Execution" in html and 'href="#runs"' in html
    assert "outcomes need attention" in html
    assert 'id="fake-card"' in html and "fakedom card" in html


def test_the_hub_gates_profile_pages_on_the_profile():
    text = (WEB / "hub.js").read_text(encoding="utf-8")
    assert "const PROFILE_PAGES = ['runs','live'];" in text
    assert "!PROFILE_PAGES.includes(id)||profileOn(id)" in text
    assert "const pages = [" not in text        # every page list goes through visiblePages()
    assert not (WEB / "mission.js").exists()


# ------------------------------------------ (c) generic code never reaches a domain directly
def _generic_python():
    for path in sorted(ALPACA_DIR.rglob("*.py")):
        rel = path.relative_to(ALPACA_DIR)
        if rel.parts[0] == "tests":
            continue
        yield rel, path


def test_no_generic_module_imports_a_flow_package():
    offenders = []
    for rel, path in _generic_python():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names = [base] + ["%s.%s" % (base, alias.name) for alias in node.names]
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__":
                names = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            for name in names:
                if "flow" in name.split("."):
                    offenders.append("%s:%d %s" % (rel, node.lineno, name))
    assert offenders == []


def test_no_generic_file_names_the_chip_flow():
    pattern = re.compile(r"orfs|sky130|openroad|alpaca\.flow|alpaca/flow", re.I)
    offenders = []
    for path in sorted(ALPACA_DIR.rglob("*")):
        rel = path.relative_to(ALPACA_DIR)
        if not path.is_file() or rel.parts[0] == "tests" or "__pycache__" in rel.parts:
            continue
        if path.suffix not in (".py", ".js", ".html", ".css", ".json", ".sql", ".md", ".txt"):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                offenders.append("%s:%d" % (rel, n))
    assert offenders == []
