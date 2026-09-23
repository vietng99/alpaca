"""Review fixes for the profile seam (alpaca/profile.py).

1. A named profile that does not load is never silent: the seal is BLOCKED, the hub overview
   carries the error and the cockpit shows it, and the failure is retried on the next load.
2. A profile resolves to a file inside its own project root, per root: two roots in one process
   get their own module, and a name that resolves outside the root (an installed or stdlib
   module) is refused.
3. A hook that raises or returns the wrong type never breaks a generic surface: it answers its
   empty value and the error is recorded per hook. The proof failed-run gate fails closed.
4. A profile route never shadows a generic route; the collision is ignored and reported.
"""
import http.server
import json
import os
import shutil
import subprocess
import textwrap
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alpaca import cli, db, profile

WEB = Path(__file__).resolve().parents[1] / "web"


def write_profile(root, body, package="domain", module="profile", yaml_extra=""):
    """Write <root>/<package>/<module>.py and name it in project.yaml."""
    pkg = Path(root) / package
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / (module + ".py")).write_text(textwrap.dedent(body), encoding="utf-8")
    (Path(root) / "project.yaml").write_text(
        "name: seamfix\nprofile: %s.%s\n%s" % (package, module, yaml_extra), encoding="utf-8")
    return pkg


def with_task(project):
    cli.main(["init"])
    cli.main(["op", "new", "fix the seam", "--done-when", "the seam holds"])
    cli.main(["task", "add", "--title", "task", "op-001", "harden the seam", "--phase", "build"])
    return db.connect(project)


BROKEN = '''
    raise ImportError("domain dependency missing: fake_eda_tool")
'''

RAISING = '''
    from alpaca import profile

    class Raising(profile.Profile):
        name = "raiser"

    def _boom(self, *args, **kwargs):
        raise RuntimeError("hook exploded")

    for _name in [n for n in dir(profile.Profile) if not n.startswith("_")
                  and callable(getattr(profile.Profile, n))]:
        setattr(Raising, _name, _boom)

    PROFILE = Raising
'''

ODD = '''
    from alpaca import profile

    class Odd(profile.Profile):
        name = "odd"

        def web(self):
            return ["runs"]

        def stages(self):
            return "abc"

        def events(self):
            return ["side_effect_kinds"]

        def paths(self):
            return {"tools": "domain/tools", "documents": 7}

        def runs(self, root):
            return [1, 2]

        def check(self, root):
            return {"stage": "x"}

    PROFILE = Odd
'''


# ================================================================ 1. a failed load is not silent
def test_a_named_profile_that_fails_to_load_blocks_the_seal(project, capsys):
    from alpaca import proof
    from alpaca.tests import proofkit
    conn = with_task(project)
    write_profile(project, BROKEN)
    proofkit.write_report(project, "t-001")
    with pytest.raises(proof.Refusal) as refused:
        proof.seal(conn, project, "t-001")
    assert any("fake_eda_tool" in p and "did not load" in p for p in refused.value.problems)
    capsys.readouterr()
    assert cli.main(["proof", "seal", "t-001"]) == cli.BLOCKED
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "fake_eda_tool" in out
    assert db.events(conn, kind=proof.KIND) == [], "nothing was sealed"


def test_the_hub_overview_and_cockpit_carry_the_profile_error(project, tmp_path):
    from alpaca import hub
    db.connect(project)
    write_profile(project, BROKEN)
    block = hub.overview(project)["profile"]
    assert "fake_eda_tool" in block["error"]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    runner = tmp_path / "cockpit.mjs"
    runner.write_text(textwrap.dedent('''
        globalThis.document = {getElementById: () => null, head: {appendChild() {}},
                               createElement: () => ({}), addEventListener() {}};
        globalThis.fetch = () => new Promise(() => {});
        const cockpit = await import(process.argv[2]);
        const o = JSON.parse(process.argv[3]);
        process.stdout.write(cockpit.renderCockpit(o, {items: []}, {items: []}, {}));
        process.exit(0);
    '''), encoding="utf-8")
    overview = {"profile": block, "tasks": [], "ops": [], "items": [], "stages": [],
                "active_jobs": [], "counts": {"items_pass": 0},
                "capture": {"status": "ok", "errors": []}}
    out = subprocess.run([node, str(runner), (WEB / "cockpit.js").as_uri(), json.dumps(overview)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert 'id="profile-error"' in out.stdout and "fake_eda_tool" in out.stdout


def test_a_failed_load_is_retried_on_the_next_load(project):
    pkg = write_profile(project, '''
        import os
        from alpaca import profile
        if os.path.exists(os.path.join(os.path.dirname(__file__), "broken")):
            raise ImportError("tool environment not ready")

        class Ready(profile.Profile):
            name = "ready"

        PROFILE = Ready
    ''')
    (pkg / "broken").write_text("x", encoding="utf-8")
    assert profile.load(project) is profile.EMPTY
    assert "tool environment not ready" in profile.error(project)
    (pkg / "broken").unlink()          # project.yaml and the module file are unchanged
    assert profile.load(project).name == "ready"
    assert profile.error(project) is None


# ============================================================ 2. resolution is per project root
def test_two_roots_in_one_process_each_get_their_own_profile(tmp_path):
    roots = []
    for label in ("alpha", "beta"):
        root = tmp_path / label
        root.mkdir()
        write_profile(root, '''
            from alpaca import profile

            class Mine(profile.Profile):
                name = "%s"

            PROFILE = Mine
        ''' % label)
        roots.append(str(root))
    assert profile.load(roots[0]).name == "alpha"
    assert profile.load(roots[1]).name == "beta"
    assert profile.load(roots[0]).name == "alpha"


@pytest.mark.parametrize("name", ["this", "profile", "os.path", "alpaca.profile", "domain..x", "../x"])
def test_a_name_that_does_not_resolve_inside_the_root_is_refused(project, name):
    Path(project, "project.yaml").write_text("name: x\nprofile: %s\n" % name, encoding="utf-8")
    assert profile.load(project) is profile.EMPTY
    assert "inside the project root" in profile.error(project)


def test_a_symlink_out_of_the_root_is_refused(project, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "profile.py").write_text("from alpaca import profile\nPROFILE = profile.Profile\n",
                                        encoding="utf-8")
    os.symlink(outside, Path(project, "domain"))
    Path(project, "project.yaml").write_text("name: x\nprofile: domain.profile\n", encoding="utf-8")
    assert profile.load(project) is profile.EMPTY
    assert "inside the project root" in profile.error(project)


# ======================================================== 3. a raising or odd hook is contained
def test_a_profile_whose_every_hook_raises_leaves_the_core_working(project):
    from alpaca import doctor, export, hub, pad
    from alpaca.hooks import session_start
    from alpaca.tests import proofkit
    conn = with_task(project)
    write_profile(project, RAISING)
    assert profile.load(project).name == "raiser"

    view = hub.overview(project)
    assert view["tasks"] and view["stages"] == [] and view["items"] == []
    errors = view["profile"]["hook_errors"]
    for hook in ("check", "runs", "acceptance", "capture", "web", "stages"):
        assert "hook exploded" in errors[hook], hook
    assert hub.history(project, kind="work")["total"] > 0
    assert hub.runs(project)["items"] == []
    payload = export.payload(conn, root=project)
    assert payload["metrics"] == {} and payload["pulse"]["now"]["stages"] == []
    assert os.path.isfile(pad.write(project))
    context = session_start.handle({"session_id": "s-raiser", "cwd": project})["context"]
    assert "Alpaca BOOT" in context and "hook exploded" in context
    findings = {f["name"]: f for f in doctor.checks(project)}
    assert findings["profile-hooks"]["level"] == "warn"
    assert "hook exploded" in findings["profile-hooks"]["detail"]

    # the failed-run gate fails closed: a raising failed_runs hook refuses the seal
    proofkit.write_report(project, "t-001")
    assert cli.main(["proof", "seal", "t-001"]) == cli.BLOCKED


def test_hook_return_values_of_the_wrong_type_read_as_empty(project):
    from alpaca import hub
    db.connect(project)
    write_profile(project, ODD)
    meta = profile.web_meta(project)
    assert meta["pages"] == [] and meta["stages"] == [] and meta["cards"] == []
    view = hub.overview(project)
    assert view["stages"] == [] and view["active_jobs"] == []
    assert "list" in view["profile"]["hook_errors"]["web"]
    assert hub.runs(project) == {"items": [], "total": 0, "errors": []}
    assert hub.documents(project, category="tools")["total"] == 0
    assert hub.history(project, kind="work")["total"] == 0


# ============================================================ 4. generic routes come first
def test_a_profile_route_cannot_shadow_a_generic_route(project):
    from alpaca import serve
    write_profile(project, '''
        from alpaca import profile

        def shadow(root, query):
            return {"shadow": True}, 200

        class Shadow(profile.Profile):
            def routes(self):
                return {"/data.json": shadow, "/files/list": shadow, "/health.json": shadow,
                        "/hub/overview.json": shadow, "/events": shadow, "/live/ok.json": shadow}

        PROFILE = Shadow
    ''')
    db.connect(project)
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % httpd.server_address[1]

    def get(path):
        try:
            r = urllib.request.urlopen(url + path, timeout=15)
        except urllib.error.HTTPError as exc:
            r = exc
        return r.status, r.read()
    try:
        for path in ("/data.json", "/health.json", "/hub/overview.json"):
            status, body = get(path)
            assert status == 200 and b'"shadow"' not in body, path
        status, body = get("/files/list")
        assert status == 403 and b"shadow" not in body, "the file browser stays closed"
        assert json.loads(get("/live/ok.json")[1]) == {"shadow": True}
    finally:
        httpd.shutdown()
        httpd.server_close()
    reported = profile.errors(project).get("routes", "")
    for path in ("/data.json", "/files/list", "/health.json", "/hub/overview.json", "/events"):
        assert path in reported, path
    assert "/live/ok.json" not in reported
