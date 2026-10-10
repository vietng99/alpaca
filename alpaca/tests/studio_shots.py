"""The Studio look baseline (t-474): build the fixture, serve it, capture every page, compare.

One command (re)captures the stored baseline, from the repository root:

    .venv/bin/python alpaca/tests/studio_shots.py update

It builds the fixture project (studio_fixture.py) in a fresh temporary folder, starts `alpaca serve`
on it under the fixed clock (studio/fixed_clock.py) on a free loopback port, drives headless Chrome
over the DevTools protocol (studio/capture.mjs) through every page and view in light and dark at 1440
and 390 px wide, writes the PNGs and manifest.json to alpaca/tests/baseline/studio/, then stops the
server and deletes the folder. Take it on `main` before any change to alpaca/web (the t-474 done bar).

    .venv/bin/python alpaca/tests/studio_shots.py compare [--keep DIR]
    .venv/bin/python alpaca/tests/studio_shots.py capture --out DIR
    .venv/bin/python alpaca/tests/studio_shots.py serve --work DIR      # build and serve, print the URL, wait

`compare` captures the current tree and compares it with the baseline (studio/compare.mjs: 0 differing
pixels allowed per image outside the masks, exit 1 otherwise), leaving the summary and diff images in
--keep DIR (default: a temporary folder that is deleted). `capture` writes one capture to DIR. Needs
node and google-chrome (CHROME names another binary); the work folder goes under $STUDIO_WORK when set.
"""
import argparse
import contextlib
import http.cookiejar
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
STUDIO = os.path.join(HERE, "studio")
BASELINE = os.path.join(HERE, "baseline", "studio")
sys.path.insert(0, HERE)
import studio_fixture  # noqa: E402

AT = studio_fixture.AT


def tools():
    """(node, chrome) paths, or a reason they are missing."""
    node = shutil.which("node")
    chrome = os.environ.get("CHROME") or shutil.which("google-chrome")
    if not node or not chrome:
        return None, None, "node and google-chrome are required for the Studio capture"
    return node, chrome, ""


def build_fixture(work):
    """Build the fixture project under `work`; return its root. Not under a home folder: the burn
    board names a transcript's account from a /home/<person>/ path, which would print a real name.
    The pooled accounts folder is refused too; its path comes from alpaca/ownerword.py ACCOUNTS_ROOT,
    so this shipped file does not name it (test_no_host_paths_shipped.py)."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from alpaca import ownerword
    real = os.path.realpath(work)
    if real.startswith(("/home/", "/Users/", ownerword.ACCOUNTS_ROOT.rstrip("/") + "/")):
        raise RuntimeError("build the Studio fixture outside a home folder (STUDIO_WORK), not in %s" % real)
    root = os.path.join(work, studio_fixture.FOLDER)
    os.makedirs(work, exist_ok=True)
    run = subprocess.run([sys.executable, os.path.join(HERE, "studio_fixture.py"), root, "--at", AT],
                         capture_output=True, text=True, timeout=300, cwd=work,
                         env=studio_fixture.env_for(root))
    if run.returncode != 0:
        raise RuntimeError("fixture build failed:\n" + run.stdout[-3000:] + run.stderr[-3000:])
    return root


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


@contextlib.contextmanager
def serve(root, at=AT, wait=90, running=False):
    """`alpaca serve` on the fixture under the fixed clock; yields the base URL. Stopped on exit.
    The capture serves a still record, so its clock stands still; the motion checks write to the
    record while serving, so theirs runs (`running`): the server's watcher throttles its board and
    fold refresh by wall time and would never push a change under a clock that stands still."""
    port = free_port()
    log = open(os.path.join(root, ".studio-fixture", "serve.log"), "ab")
    proc = subprocess.Popen([sys.executable, os.path.join(STUDIO, "fixed_clock.py"), "--at", at,
                             *(["--running"] if running else []), "--",
                             "serve", "--port", str(port), "--keep"], cwd=root, env=studio_fixture.env_for(root),
                            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    base = "http://127.0.0.1:%d" % port
    try:
        ready(base, root, proc, wait)
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


def sign_in(base, root):
    user, _, code = open(os.path.join(root, ".studio-fixture", "auth"), encoding="utf-8").read().strip().partition(":")
    opener = _opener()
    req = urllib.request.Request(base + "/auth/login", data=json.dumps({"user": user, "code": code}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    opener.open(req, timeout=10).read()
    return opener


def ready(base, root, proc, wait):
    """Wait until the server answers and its first analytics fold is done, so no revision moves
    while pages are captured."""
    end = time.monotonic() + wait
    opener = None
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError("alpaca serve exited %s; see %s" % (proc.returncode, os.path.join(root, ".studio-fixture", "serve.log")))
        try:
            opener = opener or sign_in(base, root)
            health = json.loads(opener.open(base + "/health.json", timeout=10).read())
            index = opener.open(base + "/hub/analytics-index.json", timeout=30)
            if (health.get("projection") or {}).get("state") == "fresh" and index.status == 200:
                first = opener.open(base + "/hub/overview.json", timeout=30).read()
                time.sleep(1.5)
                if opener.open(base + "/hub/overview.json", timeout=30).read() == first:
                    return
        except Exception:  # noqa: BLE001 - starting up: refused, 401 before the login, 202 while folding
            opener = None if opener is None else opener
        time.sleep(0.5)
    raise RuntimeError("the fixture server was not ready in %d s" % wait)


def capture(out, *, views=None, work=None):
    """Build, serve and capture into `out`. Returns the capture's exit code and output."""
    node, chrome, why = tools()
    if not node:
        raise RuntimeError(why)
    own = work is None
    work = work or tempfile.mkdtemp(prefix="studio-", dir=os.environ.get("STUDIO_WORK") or None)
    try:
        root = build_fixture(work)
        with serve(root) as base:
            cmd = [node, os.path.join(STUDIO, "capture.mjs"), "--base", base, "--out", out,
                   "--auth", os.path.join(root, ".studio-fixture", "auth"),
                   "--stubs", os.path.join(root, ".studio-fixture", "stubs"), "--at", AT]
            if views:
                cmd += ["--views", ",".join(views)]
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=dict(os.environ, CHROME=chrome))
        return run.returncode, run.stdout + run.stderr
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


def compare(capture_dir, out, baseline=BASELINE, max_pixels=None):
    node, _chrome, why = tools()
    if not node:
        raise RuntimeError(why)
    cmd = [node, os.path.join(STUDIO, "compare.mjs"), baseline, capture_dir, out]
    if max_pixels is not None:
        cmd += ["--max-pixels", str(max_pixels)]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return run.returncode, run.stdout + run.stderr


def main(argv=None):
    p = argparse.ArgumentParser(description="Studio look baseline: capture and compare")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update", help="recapture the stored baseline (alpaca/tests/baseline/studio)")
    c = sub.add_parser("capture", help="capture into a directory")
    c.add_argument("--out", required=True)
    c.add_argument("--views", default="")
    k = sub.add_parser("compare", help="capture this tree and compare it with the baseline")
    k.add_argument("--keep", default="", help="keep the capture, summary and diffs here")
    k.add_argument("--views", default="")
    s = sub.add_parser("serve", help="build and serve the fixture until interrupted")
    s.add_argument("--work", required=True)
    s.add_argument("--running", action="store_true", help="let the server clock run (for the motion checks)")
    a = p.parse_args(argv)
    views = [v for v in getattr(a, "views", "").split(",") if v] or None
    if a.cmd == "serve":
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        root = build_fixture(os.path.abspath(a.work))
        with serve(root, running=a.running) as base:
            print("serving %s at %s (sign in with %s/.studio-fixture/auth); Ctrl-C stops" % (root, base, root), flush=True)
            with contextlib.suppress(KeyboardInterrupt):
                while True:
                    time.sleep(3600)
        return 0
    if a.cmd == "update":
        fresh = tempfile.mkdtemp(prefix="studio-baseline-", dir=os.environ.get("STUDIO_WORK") or None)
        try:
            code, text = capture(fresh)
            print(text)
            if code != 0:
                print("capture failed; the stored baseline is unchanged", file=sys.stderr)
                return code
            shutil.rmtree(BASELINE, ignore_errors=True)
            os.makedirs(BASELINE)
            for name in sorted(os.listdir(fresh)):
                if name.endswith(".png") or name == "manifest.json":
                    shutil.copy2(os.path.join(fresh, name), os.path.join(BASELINE, name))
            size = sum(os.path.getsize(os.path.join(BASELINE, n)) for n in os.listdir(BASELINE))
            print("baseline written: %s (%d files, %d bytes)" % (BASELINE, len(os.listdir(BASELINE)), size))
            return 0
        finally:
            shutil.rmtree(fresh, ignore_errors=True)
    if a.cmd == "capture":
        code, text = capture(os.path.abspath(a.out), views=views)
        print(text)
        return code
    keep = os.path.abspath(a.keep) if a.keep else tempfile.mkdtemp(prefix="studio-compare-", dir=os.environ.get("STUDIO_WORK") or None)
    try:
        shots = os.path.join(keep, "capture")
        code, text = capture(shots, views=views)
        print(text)
        if code != 0:
            return code
        code, text = compare(shots, os.path.join(keep, "compare"))
        print(text)
        return code
    finally:
        if not a.keep:
            shutil.rmtree(keep, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
