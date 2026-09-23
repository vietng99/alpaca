"""M1.4 proof - the floor canary and the git-as-execution scan.

Done-when (from the plan, Task M1.4):

  the canary test plants byte-level canaries outside the root, runs the full instrument
  set and the hooks, and asserts every canary and the outside-root content-hash manifest
  are unchanged, and git_containment.scan FAILs a fixture repo carrying core.hooksPath,
  diff.*.textconv or filter.*.clean.

The canary is the NEGATIVE control for the floor: it proves the harness writes only inside
its own root. Files are planted in a fake $HOME (a global git config, an ssh key stand-in,
an agent settings file, a shell rc) and in a sibling project directory, each stamped with a
byte-level sentinel. Everything outside the root is hashed into a manifest; then every alpaca
verb, every hook and every instrument runs against the root; then the manifest is re-hashed
and must be byte-for-byte identical, no sentinel disturbed, and no absent path conjured.
"""
import hashlib
import json
import os
import subprocess
import sys

from alpaca.tests.conftest import REPO

from alpaca import paths
from alpaca.gates import git_containment, workspace_guard

SENTINEL = b"CANARY-SENTINEL-DO-NOT-TOUCH-4f2a"


# --------------------------------------------------------------------- helpers
def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_tree(*roots):
    """A content-hash manifest of every file under each root (absolute path -> sha256)."""
    manifest = {}
    for base in roots:
        for dirpath, _dirs, files in os.walk(base):
            for fn in sorted(files):
                p = os.path.join(dirpath, fn)
                if os.path.islink(p) or not os.path.isfile(p):
                    manifest[p] = "<non-regular>"
                    continue
                manifest[p] = _sha_file(p)
    return manifest


def _plant(path, label):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = ("# %s\n" % label).encode("utf-8") + SENTINEL + b"\n"
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def _run(argv, env, cwd, stdin=None):
    return subprocess.run(argv, cwd=cwd, env=env, input=stdin,
                          capture_output=True, text=True, encoding="utf-8")


def _make_root(base):
    """A minimal but real Alpaca project root: manifest, shim, hooks settings, style."""
    root = os.path.join(base, "proj")
    os.makedirs(root)
    import shutil
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), os.path.join(root, "ALPACA-MANIFEST"))
    os.makedirs(os.path.join(root, "bin"))
    shutil.copy(os.path.join(REPO, "bin", "alpaca"), os.path.join(root, "bin", "alpaca"))
    os.chmod(os.path.join(root, "bin", "alpaca"), 0o755)
    os.makedirs(os.path.join(root, ".claude"))
    shutil.copy(os.path.join(REPO, ".claude", "settings.json"),
                os.path.join(root, ".claude", "settings.json"))
    os.makedirs(os.path.join(root, "style"))
    with open(os.path.join(root, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("")
    return root


# --------------------------------------------------------------------- the canary
def test_floor_canary_nothing_outside_the_root_is_touched(tmp_path):
    base = str(tmp_path)
    root = _make_root(base)

    # A fake HOME with the surfaces the floor forbids, plus a sibling project.
    home = os.path.join(base, "home")
    cfg = os.path.join(home, ".claude")            # agent config dir
    sibling = os.path.join(base, "sibling")
    canaries = [
        _plant(os.path.join(home, ".gitconfig"), "global git config"),
        _plant(os.path.join(home, ".ssh", "id_rsa"), "ssh key stand-in"),
        _plant(os.path.join(home, ".bashrc"), "shell rc"),
        _plant(os.path.join(cfg, "settings.json"), "agent settings"),
        _plant(os.path.join(sibling, "their_project.py"), "sibling project file"),
    ]
    # A path that does not exist: the run must not conjure it into being.
    phantom = os.path.join(sibling, "phantom_that_must_stay_absent")
    phantom_root = os.path.join(base, "an_absent_root")
    assert not os.path.exists(phantom)
    assert not os.path.exists(phantom_root)

    # The transcript dir the hooks read lives under the (outside-root) config dir; create
    # it and a transcript BEFORE hashing, so a read leaves it byte-identical.
    tdir = os.path.join(cfg, "projects", paths.escape_cwd(root))
    os.makedirs(tdir)
    for sid in ("s-canary-1", "s-canary-2"):
        with open(os.path.join(tdir, sid + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user",
                     "content": "hi"}}) + "\n")

    before = _hash_tree(home, sibling)

    env = {**os.environ, "HOME": home, "CLAUDE_CONFIG_DIR": cfg,
           "CLAUDE_PROJECT_DIR": root, "ALPACA_NO_AUTOSERVE": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": REPO}
    alpaca = [sys.executable, "-m", "alpaca"]

    # --- every alpaca verb reachable in M1
    _run(alpaca + ["init"], env, root)
    _run(alpaca + ["onboard", "--name", "canary", "--who", "a:owner", "--what", "floor probe"],
         env, root)
    _run(alpaca + ["op", "new", "probe the floor", "--done-when", "canaries intact"], env, root)
    _run(alpaca + ["task", "add", "--title", "task", "op-001", "run the canary", "--phase", "build"], env, root)
    _run(alpaca + ["status"], env, root)
    _run(alpaca + ["status", "--json"], env, root)
    _run(alpaca + ["verify"], env, root)
    _run(alpaca + ["doctor"], env, root)

    # --- every hook, driven the way Claude Code drives it (payload on stdin)
    s1 = {"session_id": "s-canary-1", "cwd": root,
          "transcript_path": os.path.join(tdir, "s-canary-1.jsonl"), "source": "startup"}
    _run([sys.executable, "-m", "alpaca.hooks.session_start"], env, root, json.dumps(s1))
    _run([sys.executable, "-m", "alpaca.hooks.user_prompt"], env, root,
         json.dumps({"session_id": "s-canary-1", "cwd": root, "prompt": "do a thing"}))
    _run([sys.executable, "-m", "alpaca.hooks.post_tool"], env, root,
         json.dumps({"session_id": "s-canary-1", "tool_name": "Edit",
                     "tool_input": {"file_path": "x.py"}}))
    _run([sys.executable, "-m", "alpaca.hooks.stop"], env, root,
         json.dumps({"session_id": "s-canary-1"}))
    _run([sys.executable, "-m", "alpaca.hooks.session_end"], env, root,
         json.dumps({"session_id": "s-canary-1", "reason": "exit"}))

    # --- every instrument under alpaca/gates/ with a --selftest, best effort
    gates_dir = os.path.join(REPO, "alpaca", "gates")
    for fn in sorted(os.listdir(gates_dir)):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        mod = "alpaca.gates.%s" % fn[:-3]
        _run([sys.executable, "-m", mod, "--selftest"], env, root)

    # --- instruments called in-process against the root and the sibling
    workspace_guard.check(root, [root, sibling, os.path.join(root, "project.yaml")])
    git_containment.scan(root)
    from alpaca.gates import rc_conformance
    rc_conformance.scan_tree(gates_dir)
    from alpaca import doctor
    doctor.checks(root)

    after = _hash_tree(home, sibling)

    # 1) not one byte outside the root changed, nothing appeared, nothing vanished
    assert after == before, {
        "changed": {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
                    if before.get(k) != after.get(k)}}
    # 2) every byte-level sentinel is still intact
    for c in canaries:
        with open(c, "rb") as fh:
            assert SENTINEL in fh.read(), c
    # 3) an absent path was never conjured into existence
    assert not os.path.exists(phantom)
    assert not os.path.exists(phantom_root)


# ------------------------------------------------ git as execution (the FAIL half)
def _git_repo(base, body):
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, ".git"))
    with open(os.path.join(repo, ".git", "config"), "w", encoding="utf-8") as fh:
        fh.write(body)
    return repo


def test_git_containment_fails_on_hookspath_textconv_and_filter(tmp_path):
    body = (
        "[core]\n"
        "\thooksPath = /tmp/evil-hooks\n"
        "[diff \"x\"]\n"
        "\ttextconv = cat\n"
        "[filter \"y\"]\n"
        "\tclean = ./scrub.sh\n"
    )
    repo = _git_repo(str(tmp_path), body)
    findings = git_containment.scan(repo)
    tokens = {tok for tok, _f, _ln, _d in findings}
    assert git_containment.R_HOOKSPATH in tokens, findings
    assert git_containment.R_TEXTCONV in tokens, findings
    assert git_containment.R_FILTER_CLEAN in tokens, findings
    assert git_containment.verdict_of(findings) == git_containment.verdict.BLOCKED
    # each finding pins a real file and line
    for _tok, fpath, line, _detail in findings:
        assert os.path.isfile(fpath) and isinstance(line, int) and line >= 1


def test_git_containment_passes_on_a_clean_repo(tmp_path):
    repo = _git_repo(str(tmp_path), "[core]\n\tbare = false\n")
    findings = git_containment.scan(repo)
    assert findings == [], findings
    assert git_containment.verdict_of(findings) == git_containment.verdict.PASS
