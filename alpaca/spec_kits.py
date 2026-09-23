"""alpaca spec: install spec-kit or OpenSpec into this project from the vendored copies, offline.

Both tools are used as their upstreams ship them, at the versions pinned in `vendor/VENDOR.json`:

- spec-kit (https://github.com/github/spec-kit, MIT) is for a new thing: a raw idea to a first
  spec, with the clarify questions and answers. `vendor/spec-kit/` holds the output of its
  `specify init` for Claude Code, generated once from the pinned release (the skills under
  `.claude/skills/speckit-*`, the `.specify/` templates, scripts and memory). The install writes
  those files and puts `vendor/spec-kit/constitution.md` in place of the upstream constitution, so
  the constitution points at `CLAUDE.md` and `doctrine/` instead of becoming a second rulebook.
- OpenSpec (https://github.com/Fission-AI/OpenSpec, MIT) is for changes to something that exists:
  living specs plus change deltas. `vendor/openspec/npm/` holds the registry tarballs of the pinned
  CLI and its production dependencies. The first use unpacks them into
  `.alpaca/tools/openspec-<version>/` after checking every sha256; `bin/openspec` runs the CLI with
  the host `node`. The install runs the vendored `openspec init --tools claude`, which writes
  `openspec/` and the Claude Code skills and `/opsx:*` commands.

A project uses one kit, so the two never overlap: `alpaca spec init` refuses the second kit when the
project already has the other one (by its files or by the `spec:` block in `project.yaml`), unless
`--force`. For either kit the install also refuses to replace a file that differs from what the
kit would write (an edited skill, command or template), unless `--force`; OpenSpec's output is
first generated in a scratch directory and compared, so a refusal writes nothing.

`openspec init` keeps a global config (profile, delivery, workflows) under XDG_CONFIG_HOME. The
install gives it a scratch config dir that is removed afterwards, so it never writes the user's
own OpenSpec config and the user's settings do not change what the install generates.

    alpaca spec init --kit spec-kit|openspec [--force]   install and record the kit
    alpaca spec status                                   the recorded kit and the kits found on disk
    alpaca spec verify                                   check every vendored archive against its pin

Nothing here uses the network. OpenSpec runs with OPENSPEC_TELEMETRY=0 and DO_NOT_TRACK=1.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

KITS = ("spec-kit", "openspec")
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(PACKAGE_ROOT, "vendor")
PINS_FILE = "VENDOR.json"
RUNTIME = os.path.join(".alpaca", "tools")
MARKER = ".alpaca-vendor.json"
QUIET_ENV = {"OPENSPEC_TELEMETRY": "0", "DO_NOT_TRACK": "1"}
INIT_TIMEOUT = 180

#: files and directories that show a kit is present in a project, beyond the project.yaml record.
MARKERS = {
    "spec-kit": (".specify", ".claude/skills/speckit-*", ".claude/commands/speckit.*"),
    "openspec": ("openspec/config.yaml", "openspec/specs", "openspec/changes", "openspec/project.md",
                 ".claude/skills/openspec-*", ".claude/commands/opsx"),
}


class KitError(Exception):
    """An install that cannot go ahead. `blocked` separates a refusal (exit 2) from a fault (1)."""

    def __init__(self, message, blocked=False, detail=None):
        super().__init__(message)
        self.blocked = blocked
        self.detail = detail or []


# ------------------------------------------------------------------------------------ pins
def load_pins(vendor_dir=None) -> dict:
    path = os.path.join(vendor_dir or VENDOR_DIR, PINS_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise KitError("cannot read the vendored pins %s (%s)" % (path, exc))


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _vendored(vendor_dir, rel) -> str:
    rel = str(rel)
    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        raise KitError("vendored path escapes vendor/: %r" % rel)
    return os.path.join(vendor_dir, rel)


def _check_archive(vendor_dir, rel, want) -> str:
    path = _vendored(vendor_dir, rel)
    if not os.path.isfile(path):
        raise KitError("vendored archive missing: vendor/%s" % rel)
    got = sha256_file(path)
    if got != want:
        raise KitError("vendored archive vendor/%s does not match its pin (sha256 %s, pinned %s)"
                       % (rel, got, want))
    return path


def _tar_bytes(path, want=None) -> bytes:
    """The tar inside a gzip archive, checked against its pinned sha256 when one is given (the
    registry tarball's tar for an OpenSpec package, so a recompressed copy is still that package)."""
    try:
        with gzip.open(path, "rb") as fh:
            data = fh.read()
    except (OSError, EOFError) as exc:
        raise KitError("cannot read vendored archive %s (%s)" % (path, exc))
    if want and hashlib.sha256(data).hexdigest() != want:
        raise KitError("the tar inside %s does not match its pin" % path)
    return data


def verify(vendor_dir=None) -> list:
    """Every problem with the vendored copies: an archive whose sha256 differs from its pin, a
    missing archive, license file or constitution. An empty list means everything matches."""
    vendor_dir = vendor_dir or VENDOR_DIR
    problems = []
    try:
        pins = load_pins(vendor_dir)
    except KitError as exc:
        return [str(exc)]
    checks = []
    sk = pins.get("spec-kit") or {}
    op = pins.get("openspec") or {}
    if sk.get("archive"):
        checks.append((sk["archive"]["path"], sk["archive"]["sha256"],
                       sk["archive"].get("tar_sha256")))
    else:
        problems.append("spec-kit: no archive pinned")
    packages = op.get("packages") or []
    if not packages:
        problems.append("openspec: no packages pinned")
    for pkg in packages:
        checks.append((pkg["file"], pkg["sha256"], pkg.get("tar_sha256")))
    for rel, want, want_tar in checks:
        try:
            _tar_bytes(_check_archive(vendor_dir, rel, want), want_tar)
        except KitError as exc:
            problems.append(str(exc))
    for kit, doc in (("spec-kit", sk), ("openspec", op)):
        lic = doc.get("license_file")
        if not lic or not os.path.isfile(os.path.join(vendor_dir, lic)):
            problems.append("%s: upstream license file missing (%s)" % (kit, lic))
    const = (sk.get("constitution") or {}).get("replaced_by")
    if not const or not os.path.isfile(os.path.join(vendor_dir, const)):
        problems.append("spec-kit: constitution missing (%s)" % const)
    return problems


# ------------------------------------------------------------------------- project state
def detect(root) -> dict:
    """kit -> the marker paths found in the project (project-relative). A kit absent maps to []."""
    import glob
    found = {}
    for kit, patterns in MARKERS.items():
        hits = []
        for pat in patterns:
            for p in sorted(glob.glob(os.path.join(root, pat))):
                hits.append(os.path.relpath(p, root).replace(os.sep, "/"))
        found[kit] = hits
    return found


def _parse_yaml(text) -> dict:
    """project.yaml text as a mapping; a file that does not parse, or is not a mapping, is a
    KitError (a FAIL verdict), not a traceback."""
    import yaml
    try:
        doc = yaml.safe_load(text) if text else None
    except yaml.YAMLError as exc:
        raise KitError("project.yaml does not parse; fix it and rerun (%s)"
                       % " ".join(str(exc).split()))
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise KitError("project.yaml is not a mapping of keys; fix it and rerun")
    return doc


def _read_yaml(root) -> dict:
    path = os.path.join(root, "project.yaml")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return _parse_yaml(fh.read())


def recorded(root) -> dict:
    """The `spec:` block of project.yaml, or {} when the project has recorded no kit."""
    try:
        block = _read_yaml(root).get("spec")
    except Exception:
        return {}
    return block if isinstance(block, dict) else {}


def _replace_top_block(text, key, block_text, keep_comments=False) -> str:
    """Put `block_text` (a dumped top-level mapping entry) in place of the top-level `key:` block,
    or append it. Comments and the order of every other key are kept. With `keep_comments`, the
    replaced key's own comments are kept too: a trailing `# ...` on its line (when the new entry
    is one line) and the comment lines inside its old block, which follow the new entry."""
    lines = text.splitlines(keepends=True)
    out, i, done = [], 0, False
    head = re.compile(r"^%s:(\s|$)" % re.escape(key))
    trailing = re.compile(r"^%s:[^#'\"\r\n]*?(\s+#[^\r\n]*)" % re.escape(key))
    while i < len(lines):
        if not done and head.match(lines[i]):
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j][0] in " \t-"):
                j += 1
            keep = j
            while keep > i + 1 and not lines[keep - 1].strip():
                keep -= 1                      # the blank lines after the old block stay
            if keep_comments:
                m = trailing.match(lines[i])
                if m and block_text.count("\n") == 1 and block_text.endswith("\n"):
                    block_text = block_text[:-1] + m.group(1) + "\n"
                out.append(block_text)
                out.extend(ln for ln in lines[i + 1:keep] if ln.lstrip().startswith("#"))
            else:
                out.append(block_text)
            out.extend(lines[keep:j])
            i = j
            done = True
            continue
        out.append(lines[i])
        i += 1
    if not done:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(block_text)
    return "".join(out)


def record(root, block: dict) -> bool:
    """Write `spec: <block>` into project.yaml, keeping the rest of the file as it is. The first
    `installed` time is kept while the kit and version stay the same, and the file is not written
    when nothing changed. Returns whether the file was written."""
    import yaml
    from alpaca import util
    path = os.path.join(root, "project.yaml")
    text = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    before = _parse_yaml(text)
    old = before.get("spec")
    block = dict(block)
    if isinstance(old, dict) and "installed" in block and old.get("installed") \
            and (old.get("kit"), old.get("version")) == (block.get("kit"), block.get("version")):
        block["installed"] = old["installed"]
    if old == block:
        return False
    new = _replace_top_block(text, "spec", yaml.safe_dump({"spec": block}, sort_keys=False))
    after = yaml.safe_load(new) or {}
    want = dict(before)
    want["spec"] = block
    if after != want:
        before["spec"] = block
        new = yaml.safe_dump(before, sort_keys=False, allow_unicode=True)
    util.write_text(path, new)
    return True


# ------------------------------------------------------------------------------ file writes
def _inside(root, rel) -> str:
    """The absolute target for a project-relative path, refused if it leaves the project root
    (an absolute path, a `..` part, or a symlinked directory pointing outside)."""
    norm = rel.replace("\\", "/")
    parts = norm.split("/")
    if not norm or norm.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise KitError("unsafe path in a vendored archive: %r" % rel)
    real_root = os.path.realpath(root)
    target = os.path.join(real_root, *parts)
    parent = os.path.realpath(os.path.dirname(target))
    if parent != real_root and not parent.startswith(real_root + os.sep):
        raise KitError("path leaves the project root: %s" % rel)
    if os.path.islink(target):
        raise KitError("refusing to write through a symlink: %s" % rel)
    return target


def _write_bytes(path, data, mode) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _regular_members(tar, strip_first=False):
    """(relative path, member) for every regular file; refuses links, devices and unsafe names."""
    for m in tar.getmembers():
        name = m.name
        if strip_first:
            name = name.split("/", 1)[1] if "/" in name else ""
        if m.isdir():
            continue
        if not m.isreg():
            raise KitError("vendored archive holds a non-regular member: %s" % m.name)
        norm = name.replace("\\", "/")
        if not norm or norm.startswith("/") or ".." in norm.split("/"):
            raise KitError("vendored archive holds an unsafe member: %s" % m.name)
        yield norm, m


# --------------------------------------------------------------------------------- spec-kit
def install_spec_kit(root, pins, vendor_dir, force=False) -> dict:
    sk = pins["spec-kit"]
    archive = _check_archive(vendor_dir, sk["archive"]["path"], sk["archive"]["sha256"])
    const_rel = sk["constitution"]["path"]
    const_src = _vendored(vendor_dir, sk["constitution"]["replaced_by"])
    with open(const_src, "rb") as fh:
        constitution = fh.read()
    plan = {}
    raw = _tar_bytes(archive, sk["archive"].get("tar_sha256"))
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        for rel, m in _regular_members(tar):
            data = tar.extractfile(m).read()
            plan[rel] = (data, 0o755 if m.mode & 0o111 else 0o644)
    if const_rel not in plan:
        raise KitError("the vendored spec-kit output has no %s" % const_rel)
    plan[const_rel] = (constitution, 0o644)
    written, unchanged, conflicts = [], [], []
    targets = {rel: _inside(root, rel) for rel in plan}
    for rel in sorted(plan):
        target = targets[rel]
        if os.path.isfile(target):
            with open(target, "rb") as fh:
                same = fh.read() == plan[rel][0]
            (unchanged if same else conflicts).append(rel)
        elif os.path.exists(target):
            conflicts.append(rel)
    if conflicts and not force:
        raise KitError("%d file(s) differ from the vendored spec-kit copy; rerun with --force to "
                       "replace them" % len(conflicts), blocked=True, detail=conflicts)
    for rel in sorted(plan):
        if rel in unchanged:
            continue
        target = targets[rel]
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        _write_bytes(target, plan[rel][0], plan[rel][1])
        written.append(rel)
    return {"written": written, "unchanged": unchanged,
            "replaced": [r for r in conflicts if r in written], "constitution": const_rel}


# --------------------------------------------------------------------------------- OpenSpec
def _packages_digest(op) -> str:
    rows = sorted("%s %s %s %s" % (p["sha256"], p.get("tar_sha256"), p["file"],
                                   ",".join(sorted(p["paths"])))
                  for p in op["packages"])
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def runtime_dir(root, pins) -> str:
    return os.path.join(root, RUNTIME, "openspec-%s" % pins["openspec"]["version"])


def ensure_runtime(root, pins=None, vendor_dir=None) -> str:
    """Unpack the vendored OpenSpec packages into .alpaca/tools/openspec-<version>/ (once) and
    return that directory. Every tarball is checked against its pinned sha256 first."""
    vendor_dir = vendor_dir or VENDOR_DIR
    pins = pins or load_pins(vendor_dir)
    op = pins["openspec"]
    dest = runtime_dir(root, pins)
    digest = _packages_digest(op)
    if _runtime_ready(dest, digest):
        return dest
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    with _unpack_lock(os.path.join(parent, ".%s.lock" % os.path.basename(dest))):
        if _runtime_ready(dest, digest):       # another caller finished while this one waited
            return dest
        return _unpack_runtime(op, vendor_dir, dest, digest)


def _runtime_ready(dest, digest) -> bool:
    try:
        with open(os.path.join(dest, MARKER), encoding="utf-8") as fh:
            return json.load(fh).get("packages_digest") == digest
    except (OSError, ValueError):
        return False


class _unpack_lock:
    """An exclusive lock file around the first-run unpack, so two first runs at once do not both
    rename into the same directory. Where fcntl is missing the lock is a no-op; the rename below
    still treats a runtime completed by another caller as success."""

    def __init__(self, path):
        self.path, self.fh = path, None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            self.fh.close()                    # closing the file releases the lock
        return False


def _unpack_runtime(op, vendor_dir, dest, digest) -> str:
    parent = os.path.dirname(dest)
    tmp = tempfile.mkdtemp(dir=parent, prefix=".unpack-openspec-")
    try:
        for pkg in op["packages"]:
            path = _check_archive(vendor_dir, pkg["file"], pkg["sha256"])
            raw = _tar_bytes(path, pkg.get("tar_sha256"))
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
                members = list(_regular_members(tar, strip_first=True))
                for install_path in pkg["paths"]:
                    norm = install_path.replace("\\", "/")
                    if not norm.startswith("node_modules/") or ".." in norm.split("/"):
                        raise KitError("unsafe install path in the pins: %r" % install_path)
                    base = os.path.join(tmp, *norm.split("/"))
                    for rel, m in members:
                        data = tar.extractfile(m).read()
                        _write_bytes(os.path.join(base, *rel.split("/")), data,
                                     0o755 if m.mode & 0o111 else 0o644)
        with open(os.path.join(tmp, MARKER), "w", encoding="utf-8") as fh:
            json.dump({"version": op["version"], "packages_digest": digest}, fh)
        old = None
        if os.path.exists(dest):
            old = dest + ".old-%d" % os.getpid()
            os.replace(dest, old)
        try:
            os.replace(tmp, dest)
        except OSError:
            if not _runtime_ready(dest, digest):
                raise
            shutil.rmtree(tmp, ignore_errors=True)   # another caller put a complete runtime there
        if old:
            shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def _version_tuple(text):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def find_node(minimum=None) -> str:
    """The node binary ($ALPACA_NODE, else `node` on PATH), checked against the pinned engines."""
    node = os.environ.get("ALPACA_NODE") or shutil.which("node")
    if not node:
        raise KitError("OpenSpec needs node on PATH (or ALPACA_NODE); none found", blocked=True)
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise KitError("cannot run %s --version (%s)" % (node, exc), blocked=True)
    have, need = _version_tuple(out), _version_tuple(minimum)
    if have is None or (need and have < need):
        raise KitError("node %s is older than OpenSpec needs (%s)" % (out.strip() or "?", minimum),
                       blocked=True)
    return node


def openspec_command(root, pins=None, vendor_dir=None) -> list:
    """[node, entry script] for the vendored OpenSpec CLI, unpacking it on first use."""
    pins = pins or load_pins(vendor_dir)
    op = pins["openspec"]
    node = find_node(op.get("engines_node"))
    entry = os.path.join(ensure_runtime(root, pins, vendor_dir), *op["entry"].split("/"))
    if not os.path.isfile(entry):
        raise KitError("the unpacked OpenSpec runtime has no %s" % op["entry"])
    return [node, entry]


def openspec_env(base=None) -> dict:
    env = dict(os.environ if base is None else base)
    env.update(QUIET_ENV)
    env.setdefault("NO_COLOR", "1")
    return env


def _walk_files(root, tops) -> list:
    out = []
    for base in tops:
        for dp, dn, fn in os.walk(base):
            dn.sort()
            out += [os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/") for f in sorted(fn)]
    return out


def _openspec_files(root) -> list:
    """Every file the OpenSpec install owns or reads in the project: openspec/, the /opsx
    commands and the openspec-* skills."""
    import glob
    tops = [os.path.join(root, "openspec"), os.path.join(root, ".claude", "commands", "opsx")]
    tops += sorted(glob.glob(os.path.join(root, ".claude", "skills", "openspec-*")))
    return sorted(_walk_files(root, tops))


def _digests(root, rels) -> dict:
    out = {}
    for rel in rels:
        path = os.path.join(root, *rel.split("/"))
        if os.path.isfile(path) and not os.path.islink(path):
            out[rel] = sha256_file(path)
    return out


#: the scratch global config `openspec init` reads: the profile and delivery the install uses,
#: stated so no migration step runs and the user's own settings play no part.
INIT_GLOBAL_CONFIG = {"featureFlags": {}, "profile": "core", "delivery": "both"}


def _run_openspec_init(cmd, target, work, force) -> subprocess.CompletedProcess:
    """`openspec init --tools claude --profile core` on `target`, with XDG_CONFIG_HOME and
    XDG_DATA_HOME pointed at `work` (a scratch dir the caller removes)."""
    cfg = os.path.join(work, "config")
    os.makedirs(os.path.join(cfg, "openspec"), exist_ok=True)
    os.makedirs(os.path.join(work, "data"), exist_ok=True)
    with open(os.path.join(cfg, "openspec", "config.json"), "w", encoding="utf-8") as fh:
        json.dump(INIT_GLOBAL_CONFIG, fh)
    env = openspec_env()
    env.update({"XDG_CONFIG_HOME": cfg, "XDG_DATA_HOME": os.path.join(work, "data")})
    args = cmd + ["init", "--tools", "claude", "--profile", "core", "--no-animation"]
    if force:
        args.append("--force")
    args.append(os.path.realpath(target))
    p = subprocess.run(args, cwd=target, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=INIT_TIMEOUT)
    if p.returncode != 0:
        raise KitError("openspec init failed (exit %d): %s" % (
            p.returncode, (p.stderr or p.stdout).strip()[-800:]))
    return p


def install_openspec(root, pins, vendor_dir, force=False) -> dict:
    """Run the vendored `openspec init` in a scratch project first and compare what it writes
    under .claude/ with the project's files. A project file that differs is a refusal (nothing
    written) unless `force`. openspec/ itself is the project's own data: init keeps an existing
    openspec/config.yaml, so it is not compared."""
    cmd = openspec_command(root, pins, vendor_dir)
    work = tempfile.mkdtemp(prefix="alpaca-openspec-init-")
    try:
        stage = os.path.join(work, "stage")
        os.makedirs(stage)
        _run_openspec_init(cmd, stage, os.path.join(work, "stage-home"), force=False)
        conflicts = []
        for rel in _walk_files(stage, [os.path.join(stage, ".claude")]):
            target = os.path.join(root, *rel.split("/"))
            if os.path.islink(target) or (os.path.exists(target) and not os.path.isfile(target)):
                conflicts.append(rel)
            elif os.path.isfile(target) and \
                    sha256_file(target) != sha256_file(os.path.join(stage, *rel.split("/"))):
                conflicts.append(rel)
        conflicts.sort()
        if conflicts and not force:
            raise KitError("%d file(s) differ from what the vendored OpenSpec writes; rerun with "
                           "--force to replace them" % len(conflicts), blocked=True, detail=conflicts)
        before = _digests(root, _openspec_files(root))
        p = _run_openspec_init(cmd, root, os.path.join(work, "home"), force=force)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    after_list = _openspec_files(root)
    after = _digests(root, after_list)
    return {"written": [f for f in after_list if f not in before],
            "replaced": sorted(f for f in before if f in after and after[f] != before[f]),
            "removed": sorted(f for f in before if f not in after),
            "unchanged": sorted(f for f in before if after.get(f) == before[f]),
            "present": after_list, "cli": "bin/openspec",
            "init_output": p.stdout.strip().splitlines()[-12:]}


# ------------------------------------------------------------------------------------ verbs
def init(root, kit, force=False, vendor_dir=None) -> dict:
    """Install `kit` into the project at `root` and record it in project.yaml."""
    from alpaca import util
    if kit not in KITS:
        raise KitError("unknown kit %r; use one of %s" % (kit, ", ".join(KITS)), blocked=True)
    vendor_dir = vendor_dir or VENDOR_DIR
    pins = load_pins(vendor_dir)
    other = [k for k in KITS if k != kit][0]
    present = detect(root)
    rec = _read_yaml(root).get("spec")          # a project.yaml that does not parse stops here
    rec = rec if isinstance(rec, dict) else {}
    clash = list(present[other])
    if rec.get("kit") == other:
        clash.insert(0, "project.yaml spec.kit: %s" % other)
    if clash and not force:
        raise KitError("this project already uses %s; one kit per project (spec-kit for a new "
                       "thing, OpenSpec for changes to something that exists). Rerun with --force "
                       "to install %s anyway" % (other, kit), blocked=True, detail=clash)
    if kit == "spec-kit":
        files = install_spec_kit(root, pins, vendor_dir, force=force)
    else:
        files = install_openspec(root, pins, vendor_dir, force=force)
    block = {"kit": kit, "version": pins[kit]["version"], "upstream": pins[kit]["upstream"],
             "commit": pins[kit]["commit"], "installed": util.now_iso()}
    if kit == "spec-kit":
        block["constitution"] = files["constitution"]
    else:
        block["cli"] = "bin/openspec"
    if clash:
        block["also_present"] = other
    record(root, block)
    return {"verdict": "PASS", "kit": kit, "version": pins[kit]["version"], "files": files,
            "recorded": "project.yaml spec"}


def status(root, vendor_dir=None) -> dict:
    vendor_dir = vendor_dir or VENDOR_DIR
    out = {"verdict": "PASS", "kit": recorded(root).get("kit"), "recorded": recorded(root),
           "detected": {k: v for k, v in detect(root).items() if v}}
    try:
        pins = load_pins(vendor_dir)
        out["pinned"] = {k: {"version": pins[k]["version"], "tag": pins[k]["tag"],
                             "commit": pins[k]["commit"]} for k in KITS}
    except KitError as exc:
        out["pinned"] = {"error": str(exc)}
    return out


def env_lines(root) -> list:
    """Lines for Claude Code's CLAUDE_ENV_FILE: with OpenSpec recorded, put this project's bin/ on
    PATH so the `/opsx:*` commands and skills find `openspec`, and keep its telemetry off."""
    if recorded(root).get("kit") != "openspec":
        return []
    bindir = os.path.join(os.path.realpath(root), "bin")
    if not os.path.isfile(os.path.join(bindir, "openspec")):
        return []
    return ['export PATH=%s:"$PATH"\n' % shlex.quote(bindir),
            "export OPENSPEC_TELEMETRY=0 DO_NOT_TRACK=1\n"]


from alpaca import cli  # noqa: E402  (registers the verb; cli imports this module lazily)


@cli.command("spec")
def cmd_spec(args):
    root = cli._root()
    verb = getattr(args, "spec_verb", None)
    try:
        if verb == "init":
            result = init(root, args.kit, force=args.force)
        elif verb == "status":
            result = status(root)
        elif verb == "verify":
            problems = verify()
            result = {"verdict": "FAIL" if problems else "PASS", "problems": problems,
                      "vendor": os.path.relpath(VENDOR_DIR, root) if VENDOR_DIR.startswith(root)
                      else VENDOR_DIR}
            print(json.dumps(result, indent=1))
            return cli.FAIL if problems else cli.PASS
        else:
            print("GATE alpaca-spec: BLOCKED (unknown spec verb; use init, status or verify)")
            return cli.BLOCKED
    except KitError as exc:
        verdict = "BLOCKED" if exc.blocked else "FAIL"
        print(json.dumps({"verdict": verdict, "reason": str(exc), "detail": exc.detail}, indent=1))
        return cli.BLOCKED if exc.blocked else cli.FAIL
    print(json.dumps(result, indent=1))
    return cli.PASS


def _parser(sub):
    p = sub.add_parser("spec", help="install spec-kit or OpenSpec from the vendored copies (offline)")
    v = p.add_subparsers(dest="spec_verb")
    i = v.add_parser("init", help="install one kit into this project and record it in project.yaml")
    i.add_argument("--kit", required=True, choices=KITS)
    i.add_argument("--force", action="store_true",
                   help="install even when the other kit is present; replace differing files")
    v.add_parser("status", help="the recorded kit, the kits found on disk, the pinned versions")
    v.add_parser("verify", help="check every vendored archive against its pinned sha256")


cli.register_parser("spec", _parser)


def main(argv=None) -> int:
    """`python -m alpaca.spec_kits entry openspec --root R`: print the node binary and the entry
    script of the vendored OpenSpec CLI, one per line (bin/openspec reads them)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 4 or argv[:2] != ["entry", "openspec"] or argv[2] != "--root":
        print("usage: python -m alpaca.spec_kits entry openspec --root <project root>", file=sys.stderr)
        return cli.USAGE
    try:
        node, entry = openspec_command(argv[3])
    except KitError as exc:
        print("openspec: %s" % exc, file=sys.stderr)
        return cli.BLOCKED if exc.blocked else cli.FAIL
    print(node)
    print(entry)
    return cli.PASS


if __name__ == "__main__":
    sys.exit(main())
