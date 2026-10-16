"""alpaca new: start a project as a full copy of this Alpaca installation.

    alpaca new [--name NAME] [--dir DIR] [--who NAME:ROLE] [--what TEXT]
               [--no-git] [--no-bootstrap] [--no-serve] [--python PY]
               [--attach SOURCE [--attach-gates-file PATH] [--attach-base BRANCH]]

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

With `--attach SOURCE` (a git URL or a local path) the studio runs beside an existing repository:
after onboarding, and before doctor, the copy runs `alpaca attach init SOURCE`, which clones the
repository into app/ (never into the studio's own files), senses its gates and writes the `attach:`
block of project.yaml. The refusal of a non-empty DIR stays: the clone goes into a fresh studio.

Every copy is its own installation, pinned to the Alpaca version it was made from.
"""
import getpass
import importlib.util
import os
import re
import shutil
import signal
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
    from alpaca.watchdog.gitw import LOCAL_ENV
    env = dict(os.environ)
    # LOCAL_ENV: variables that point git at another repository (a git hook exports GIT_DIR); with one
    # set, `git init` in the copy would act on that repository and leave the copy with no .git of its own
    for key in ("ALPACA_ROOT", "ALPACA_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR",
                "PYTHONPATH", "PYTHONHOME", "ALPACA_CALLER_CWD", "ALPACA_REGRESSION_SUITE_ACTIVE",
                "VIRTUAL_ENV", *LOCAL_ENV):
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


# How long a child gets to finish after a Ctrl-C before it is killed. subprocess.run waits 0.25 s, which cut
# `alpaca attach init` off in the middle of its rollback (S8 of the t-675 report: 5 of 5 probe runs): the rollback
# deletes the clone tree and then a handful of small files, and a clone of a large repository takes seconds to
# delete on a slow disk. 60 s is far above that and still bounded: a child that ignores the Ctrl-C does not hold
# the terminal for more than a minute, and a second Ctrl-C ends the wait at once.
INTERRUPT_GRACE = 60.0


def _run(args, cwd, env, label, check=True):
    print("alpaca new: %s" % label, flush=True)
    proc = subprocess.Popen(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Popen.communicate waits 0.25 s on its own before a Ctrl-C reaches this code, which delays the forward and
    # can swallow a quick second Ctrl-C (t-681 confirm-r1, M1); zero hands the interrupt over at once.
    proc._sigint_wait_secs = 0
    try:
        try:
            stdout, stderr = proc.communicate()
        except KeyboardInterrupt:
            # the terminal sent the Ctrl-C to the child as well; a Ctrl-C sent to this process alone has not
            # reached it, so it is forwarded, then the child is given time to undo what it did (INTERRUPT_GRACE)
            _forward_interrupt(proc)
            try:
                proc.communicate(timeout=INTERRUPT_GRACE)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                pass                                  # out of time, or a second Ctrl-C: the finally below kills it
            raise
    finally:
        if proc.poll() is None:
            proc.kill()                               # whatever ended this call, no child is left running
            proc.wait()
    if proc.returncode and check:
        tail = (stdout + stderr).strip().splitlines()[-15:]
        print("\n".join("  " + line for line in tail))
        raise RuntimeError("%s failed (exit %d)" % (label, proc.returncode))
    return stdout


def _forward_interrupt(proc):
    try:
        proc.send_signal(signal.SIGINT)
    except OSError:
        pass                                          # it has already ended


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


def _caller_path(raw):
    """A path the caller typed, made absolute against the folder `alpaca new` was run in: the copy's
    commands run in the copy, where a relative path would mean something else."""
    return os.path.abspath(os.path.join(_caller_dir(), os.path.expanduser(raw)))


def _attach_source(raw):
    """The `--attach` source for the copy: a local folder becomes an absolute path, a URL stays as typed."""
    path = _caller_path(raw)
    return path if os.path.isdir(path) else raw


def _attach_refusal(args):
    """Why the --attach options cannot be used, or None. Each one is tested by `is not None`: a value that
    is empty or blank is refused here, never dropped and never reported ok with no attach."""
    given = (("the --attach source", args.attach), ("--attach-gates-file", args.attach_gates_file),
             ("--attach-base", args.attach_base))
    for label, value in given:
        if value is not None and not value.strip():
            return "%s is empty; give it a value or leave it out (an unset shell variable gives an empty one)" % label
    if args.attach is None and (args.attach_gates_file is not None or args.attach_base is not None):
        return "--attach-gates-file and --attach-base need --attach SOURCE; they do nothing without it"
    if args.attach is not None and args.attach.strip().startswith("-"):
        return "the --attach source starts with '-' and would be read as a git option"
    return None


@cli.command("new")
def cmd_new(args):
    interactive = sys.stdin.isatty() and not args.yes
    name = (args.name or (_ask("Project name") if interactive else "")).strip()
    if not name:
        print("GATE alpaca-new: FAIL (a project name is required: --name NAME)")
        return cli.USAGE
    # "--attach" is asked for when it is given at all: an empty value (an unset shell variable) is a request
    # too, so every test below is `is not None`, never a truth test
    attach = args.attach is not None
    refusal = _attach_refusal(args)
    if refusal:
        # refused before the copy is made, so no partial studio is left behind (attach init refuses it too)
        print("GATE alpaca-new: FAIL (%s)" % refusal)
        return cli.FAIL
    if attach:
        from alpaca import onboard_flow
        refused = onboard_flow.setup_write_refusal(None, None, "new-attach")
        if refused:
            # the owner makes the choice in the first chat, after a setup card and a typed yes; a caller that is not the
            # owner's own terminal does not attach on its own, and refusing here leaves no half-made studio behind
            print("GATE alpaca-new: FAIL (%s) [%s]" % (refused[1], refused[0]))
            return cli.FAIL
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
        if attach:
            # the clone is kept out of the studio's commits by the studio's own .git/info/exclude (P2)
            from alpaca.attach import gitops
            if gitops.studio_kind(target) != "own":
                raise RuntimeError("--attach needs a studio with a git repository of its own; %s has none "
                                   "(do not combine --attach with --no-git or with git not installed)" % target)
        if not args.no_bootstrap:
            boot = ["bash", "setup/bootstrap.sh"]
            if args.python:
                boot += ["--python", args.python]
            _run(boot, target, env, "bootstrap (local .venv, pinned requirements)")
        alpaca = _alpaca(target, not args.no_bootstrap)
        # provisional: the project is set up now, and the first chat (look at the owner's repo, one setup card,
        # the owner's yes) is still due; `onboard apply` revises this setup after the yes
        _run(alpaca + ["onboard", "--provisional", "--name", name, "--who", args.who or _default_who(),
              "--what", args.what or "%s, built with Alpaca." % name], target, env, "onboard")
        if attach:
            # options first, the source last behind "--": a source such as -h would otherwise be read by the
            # child's argument parser as its own help option, which prints help and exits 0
            _run(alpaca + ["attach", "init"]
                 + (["--gates-file=" + _caller_path(args.attach_gates_file)]
                    if args.attach_gates_file is not None else [])
                 + (["--base=" + args.attach_base] if args.attach_base is not None else [])
                 + ["--", _attach_source(args.attach)],
                 target, env, "attach (clone the repository into the studio, sense its gates)")
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
    print("      Then tell it about your project: paste a git URL or a folder path, or say \"start empty\".")
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
    p.add_argument("--attach", metavar="SOURCE",
                   help="attach an existing repository (git URL or local path): clone it into app/ and "
                        "write the attach: block; Alpaca files never go into it")
    p.add_argument("--attach-gates-file", metavar="PATH",
                   help="JSON list of {name, command (argv list), timeout} gates, instead of sensing them")
    p.add_argument("--attach-base", metavar="BRANCH",
                   help="integration branch of the attached repository (default: its HEAD branch)")


cli.register_parser("new", _parser)
