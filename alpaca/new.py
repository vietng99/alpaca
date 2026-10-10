"""alpaca new: start a project as a full copy of this Alpaca installation.

    alpaca new [--name NAME] [--dir DIR] [--who NAME:ROLE] [--what TEXT]
               [--no-git] [--no-bootstrap] [--no-serve] [--python PY]

Asks for the project name and the working directory when they are not given (a relative DIR is
read from the folder the command was run in), then:

  1. refuses a DIR that exists and is not empty, before anything is written;
  2. copies the shipped file list, the mechanism class of ALPACA-MANIFEST as setup/gen_manifest.py
     walks it, so runtime state (.alpaca/, RESUME.md, analytics/, .venv) never travels;
  3. when this installation is itself onboarded (a development tree), puts the release templates
     named by project.yaml `release.overlays` in place, so the copy starts from the template;
  4. writes MANIFEST.json for the copy, then fills the project-name block of README.md;
  5. runs `git init`, `bash setup/bootstrap.sh`, `bin/alpaca onboard` and `bin/alpaca doctor` in the
     copy, each with the copy's own interpreter and root;
  6. starts the copy's hub (`bin/alpaca serve --detach`) and prints its localhost URL.

Every copy is its own installation, pinned to the Alpaca version it was made from.
"""
import getpass
import importlib.util
import os
import re
import shutil
import subprocess
import sys

from alpaca import cli

README_START = "<!-- alpaca:project-name -->"
README_END = "<!-- /alpaca:project-name -->"
_URL = re.compile(r"running at (http://\S+)")


def _source_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gen_manifest(root):
    """setup/gen_manifest.py of an installation, loaded by path (setup/ is not a package)."""
    path = os.path.join(root, "setup", "gen_manifest.py")
    spec = importlib.util.spec_from_file_location("_alpaca_new_gen_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "project"


def _ask(prompt, default=None):
    shown = "%s [%s]: " % (prompt, default) if default else "%s: " % prompt
    try:
        answer = input(shown).strip()
    except EOFError:
        answer = ""
    return answer or (default or "")


def _default_who():
    try:
        out = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=5)
        name = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    if not name:
        try:
            name = getpass.getuser()
        except Exception:
            name = "owner"
    return "%s:owner" % name.replace(",", " ").replace(":", " ").strip()


def _caller_dir():
    return os.environ.get("ALPACA_CALLER_CWD") or os.getcwd()


def _child_env():
    """The copy's commands run as that copy: nothing inherited may point them back at this one."""
    env = dict(os.environ)
    for key in ("ALPACA_ROOT", "ALPACA_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR",
                "PYTHONPATH", "PYTHONHOME", "ALPACA_CALLER_CWD", "ALPACA_REGRESSION_SUITE_ACTIVE",
                "VIRTUAL_ENV"):
        env.pop(key, None)
    return env


def copy_tree(src, dst):
    """Copy the mechanism class of `src` into `dst` (absent or empty). Returns the file count."""
    gm = _gen_manifest(src)
    manifest, walk_errors, read_errors = gm.build(src)
    blocked = gm._blocked_reason(walk_errors, read_errors)
    if blocked:
        raise RuntimeError(blocked)
    os.makedirs(dst, exist_ok=True)
    for rel, marker in manifest["file_modes"].items():
        s = os.path.join(src, rel.replace("/", os.sep))
        d = os.path.join(dst, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(d), exist_ok=True)
        if marker == "l":
            os.symlink(os.readlink(s), d)
            continue
        shutil.copyfile(s, d)
        os.chmod(d, 0o755 if marker == "f0755" else 0o644)
    return manifest["file_count"]


#: the release templates every release carries; an onboarded tree with no `release.overlays` of its
#: own (a project made by alpaca new) puts the ones it has in place, so its copy starts unonboarded
DEFAULT_OVERLAYS = {"project.yaml": "templates/release/project.yaml",
                    "intents/queue.md": "templates/release/queue.md",
                    ".gitignore": "templates/release/gitignore"}


def apply_release_overlays(src, dst):
    """A development tree (onboarded) ships its release templates in place of its own identity."""
    gm = _gen_manifest(src)
    if not gm.onboarded(src):
        return []
    import yaml
    with open(os.path.join(src, "project.yaml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    overlays = ((doc.get("release") or {}).get("overlays") or {}) if isinstance(doc, dict) else {}
    if not overlays:
        overlays = {t: s for t, s in DEFAULT_OVERLAYS.items() if os.path.isfile(os.path.join(src, s))}
    done = []
    for target, source in sorted(overlays.items()):
        s = os.path.join(src, str(source))
        if not os.path.isfile(s):
            raise RuntimeError("release overlay %s names a missing file %s" % (target, source))
        d = os.path.join(dst, str(target))
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copyfile(s, d)
        os.chmod(d, 0o644)
        done.append(str(target))
    return done


def fill_readme(root, name):
    """Write the project's name into the README block between the two markers. False when absent."""
    path = os.path.join(root, "README.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    start, end = text.find(README_START), text.find(README_END)
    if start < 0 or end < start:
        return False
    block = ("%s\n# %s\n\n%s is built with Alpaca, the engineering studio around your coding agents. "
             "The Alpaca guide follows.\n%s" % (README_START, name, name, README_END))
    text = text[:start] + block + text[end + len(README_END):]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return True


def _run(args, cwd, env, label, check=True):
    print("alpaca new: %s" % label, flush=True)
    res = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    if res.returncode and check:
        tail = (res.stdout + res.stderr).strip().splitlines()[-15:]
        print("\n".join("  " + line for line in tail))
        raise RuntimeError("%s failed (exit %d)" % (label, res.returncode))
    return res.stdout


def _alpaca(root, bootstrapped):
    """The copy's own CLI: its bin/alpaca once bootstrap made its .venv, else this interpreter
    running the copy's package from the copy's root."""
    if bootstrapped:
        return [os.path.join(root, "bin", "alpaca")]
    return [sys.executable, "-m", "alpaca"]


def hub_url(root, env, alpaca):
    """Start the copy's hub in its own session and return its localhost URL, or None."""
    try:
        subprocess.run(alpaca + ["serve", "--detach"], cwd=root, env=env, text=True,
                       capture_output=True, timeout=30)
        out = subprocess.run(alpaca + ["serve", "--status"], cwd=root, env=env, text=True,
                             capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = _URL.search(out)
    return m.group(1) if m else None


@cli.command("new")
def cmd_new(args):
    interactive = sys.stdin.isatty() and not args.yes
    name = (args.name or (_ask("Project name") if interactive else "")).strip()
    if not name:
        print("GATE alpaca-new: FAIL (a project name is required: --name NAME)")
        return cli.USAGE
    default_dir = os.path.join(_caller_dir(), slug(name))
    raw_dir = args.dir or (_ask("Working directory", default_dir) if interactive else default_dir)
    target = os.path.abspath(os.path.join(_caller_dir(), os.path.expanduser(raw_dir)))
    if os.path.lexists(target) and (not os.path.isdir(target) or os.listdir(target)):
        print("GATE alpaca-new: FAIL (%s exists and is not empty; choose another directory)" % target)
        return cli.FAIL
    src = _source_root()
    if os.path.realpath(target).startswith(os.path.realpath(src) + os.sep):
        print("GATE alpaca-new: FAIL (%s is inside this installation; choose a directory outside it)"
              % target)
        return cli.FAIL
    env = _child_env()
    try:
        count = copy_tree(src, target)
        print("alpaca new: copied %d files into %s" % (count, target))
        overlays = apply_release_overlays(src, target)
        if overlays:
            print("alpaca new: release templates in place: %s" % ", ".join(overlays))
        gm = _gen_manifest(target)
        if gm.write(target) != 0:
            raise RuntimeError("MANIFEST.json could not be written for the copy")
        if not fill_readme(target, name):
            print("alpaca new: README.md has no project-name block; left as shipped")
        if not args.no_git and shutil.which("git"):
            _run(["git", "init", "-q"], target, env, "git init")
        if not args.no_bootstrap:
            boot = ["bash", "setup/bootstrap.sh"]
            if args.python:
                boot += ["--python", args.python]
            _run(boot, target, env, "bootstrap (local .venv, pinned requirements)")
        alpaca = _alpaca(target, not args.no_bootstrap)
        _run(alpaca + ["onboard", "--name", name, "--who", args.who or _default_who(),
              "--what", args.what or "%s, built with Alpaca." % name], target, env, "onboard")
        # doctor exits non-zero on a WARN too (a fresh copy has no transcript dir until its first
        # session), so the result is its ERROR lines, not its exit status
        doctor = _run(alpaca + ["doctor"], target, env, "doctor", check=False)
    except (OSError, RuntimeError) as e:
        print("GATE alpaca-new: FAIL (%s; the partial copy stays at %s for inspection)" % (e, target))
        return cli.FAIL
    errors = [line.strip() for line in doctor.splitlines() if line.lstrip().startswith("ERROR")]
    for line in errors:
        print("  " + line)
    url = None if args.no_serve else hub_url(target, env, alpaca)
    print("")
    print("alpaca new: ok (%s at %s; doctor %d ERROR line(s))" % (name, target, len(errors)))
    if url:
        print("Hub (live status, this machine only): %s" % url)
    elif not args.no_serve:
        print("Hub: start it with `bin/alpaca serve --detach`, then `bin/alpaca serve --status` prints its URL")
    print("Next: cd %s && claude   (or codex)" % target)
    return cli.FAIL if errors else cli.PASS


def _parser(sub):
    p = sub.add_parser("new", help="start a project as a full copy of this Alpaca installation")
    p.add_argument("--name", help="project name (asked when omitted)")
    p.add_argument("--dir", help="working directory for the project (asked when omitted; "
                                 "default ./<name as a slug>)")
    p.add_argument("--who", help="name:role pairs for onboarding (default: your git user.name as owner)")
    p.add_argument("--what", help="one line on what the project builds")
    p.add_argument("--python", help="interpreter for bootstrap (default: python3)")
    p.add_argument("--yes", action="store_true", help="never prompt; use the defaults")
    p.add_argument("--no-git", action="store_true", help="skip git init")
    p.add_argument("--no-bootstrap", action="store_true", help="skip creating the local .venv")
    p.add_argument("--no-serve", action="store_true", help="do not start the hub")


cli.register_parser("new", _parser)
