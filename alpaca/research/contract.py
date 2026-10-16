"""The format 3 contract loader: read the research block, merge pack defaults with the task
override, record the diff, refuse an ungranted loosening and an overlap, freeze the contract and
verify the frozen copy (t-088's runbook-format-3.md section 14.2 says what each function does;
architecture.md section 3.5 owns the names).

The layers (runbook-format-3.md section 7.1): the engine, the pack defaults file, the runbook's own
block, then the task layer, which is the `research` part of the task's newest contract. `load_block`
reads the pack-level contract (the first three, checked by `alpaca runbook check` with the
runbook's own spec, and the tracked pack files at the commit `repo.ref` names); `merge` adds the
task layer and returns the recorded diff, each field classed as section 7.5 says; `pack_diff`
compares the pack-level contract with its baseline, the newest `research-contract` event; and
`check_override` refuses every loosening no owner loosen grant names (`owner.authorized`).

`freeze` writes the run's frozen contract once: `contract.json` (the contract object of section
14.6), copies of the protected, held-out and store reproducer files, and `files.json` over them,
and records the contract as an intent record, which the record never edits. The copies are
read-only, and freeze returns the inode and ctime of every frozen path for the run-start event.
After that no agent write changes what the run is held to: `load_frozen` refuses a frozen copy
that differs from the record, bytes or stamps (RUN-CONTRACT-CHANGED), `drift` records every later edit of the pack or the store on disk as
`research-contract-drift`, and `guard_task_contract`, which `taskcontract.record` calls, refuses and
records an edit of a live run's task override (CONTRACT-EDIT-DURING-RUN)."""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import shutil
import stat
import subprocess

from alpaca import db, intent, runbook, taskcontract, util
from alpaca.gates import verdict as vc
from alpaca.research import errors, events, owner, paths, texts
from alpaca.research import types as rtypes
from alpaca.research.types import Contract, OverrideDiff  # noqa: F401


KIND_CONTRACT = "research-contract"      # = events.CONTRACT, the pack-level baseline (t-088 7.7)


ENGINE_PATHS = ("alpaca/", "bin/", "setup/", ".alpaca/", ".claude/", "contracts/")   # t-088 14.2


STORE_ROOT = "contracts/research"        # t-088 14.2; paths.reproducer_store


FORBIDDEN_READ = ("alpaca", "bin", "setup", ".alpaca", "contracts", ".git", ".venv")

#: the actor of the events and the author of the intent record this module writes
_AUTHOR = "alpaca-research"
#: the research format; preview and freeze take a `runbook` argument that hides the module name
_FORMAT = runbook.RESEARCH_FORMAT

# The override class of every leaf of the block (runbook-format-3.md section 4, the "during a run /
# override" column; section 7.5 says what each class allows). A key is a path from the block with
# `[]` for the items of a list; `covers` and `source` are `none` everywhere.
_CLASSES = {
    "mode": "none", "defaults": "none", "repo.path": "fixed", "repo.ref": "fixed",
    "metrics[].id": "none", "metrics[].description": "free", "metrics[].command": "fixed",
    "metrics[].workdir": "fixed", "metrics[].parser": "fixed", "metrics[].path": "fixed",
    "metrics[].field": "fixed", "metrics[].unit": "fixed", "metrics[].direction": "fixed",
    "metrics[].kind": "fixed", "metrics[].repeats": "higher", "metrics[].min_ok": "higher",
    "metrics[].median": "order worse, middle", "metrics[].margin": "higher", "metrics[].seeds": "fixed",
    "metrics[].timeout": "lower", "metrics[].target": "target", "metrics[].artifact": "free",
    "accept.rule": "fixed", "accept.after_targets": "order stop, pareto",
    "paths.editable": "subset", "paths.protected": "superset", "paths.hidden": "superset",
    "paths.tests": "superset", "paths.read": "subset", "paths.keep": "subset", "paths.outputs": "subset",
    "fingerprint.tools": "superset", "fingerprint.shared": "superset", "gates": "superset",
    "stop.rounds": "lower", "stop.slots": "lower",
    "budget.usd": "lower", "budget.tokens": "lower", "budget.wall": "lower", "disk": "free",
    "proposal[].field": "none", "proposal[].type": "fixed", "proposal[].required": "fixed",
    "proposal[].values": "fixed", "proposal[].max": "fixed", "proposal[].description": "free",
    "categories[].id": "none", "categories[].text": "free",
    "lessons.file": "free", "lessons.note_chars": "free",
    "controls.noop_rounds": "higher", "controls.random": "order true, false", "controls.mutate": "fixed",
    "controls.syntax": "fixed", "controls.seed": "fixed",
    "holdout.stages": "superset", "holdout.paths": "superset",
    "repair.prepare": "fixed", "repair.pass_to_pass.command": "fixed",
    "repair.pass_to_pass.min_tests": "higher", "repair.pass_to_pass.timeout": "lower",
    "repair.cases[].id": "none", "repair.cases[].bug_report": "fixed", "repair.cases[].base": "fixed",
    "repair.cases[].fail_to_pass.origin": "fixed", "repair.cases[].fail_to_pass.command": "fixed",
    "repair.cases[].fail_to_pass.tests": "superset", "repair.cases[].fail_to_pass.timeout": "lower",
    "repair.cases[].fail_to_pass.repeats": "higher", "repair.cases[].suspect_files": "free",
    "coupled[].id": "none", "coupled[].files": "free", "coupled[].note": "free",
    "runtime.retries": "lower", "verifier.timeout": "higher", "verifier.rerun_timeout": "higher",
    "audit.hints": "fixed", "audit.assert_tokens": "superset", "audit.build_files": "superset",
    "audit.steering": "superset", "egress.allow": "subset",
}
for _role in ("proposer", "implementer", "notes"):
    _CLASSES["runtime.timeouts." + _role] = "free"
    _CLASSES["runtime.effort." + _role] = "free"
for _role in ("audit", "reproducer", "reviewer"):
    _CLASSES["runtime.timeouts." + _role] = "higher"       # a check's time may only rise (rule 9)
    _CLASSES["runtime.effort." + _role] = "order high, medium, low"
del _role

#: the keys contract_sha256 leaves out: what a task carries, the grants, the two hashes, and the
#: host realpaths of the read paths and tools, which depend on the caller's PATH, cwd and machine
#: (runbook-format-3.md section 7.8 hashes the object of section 14.6, which has neither); they
#: stay in contract.json, which load_frozen compares byte for byte with the intent record's bar
_NOT_HASHED = ("task", "ref", "grants", "layers", "override_diff", "override", "contract_sha256",
               "pack_sha256", "read_real", "tools_real")


# ------------------------------------------------------------------------------ small helpers
def _refuse(reason, detail="", loosened=()):
    """The ContractRefusal of a reason code of errors.CODES, with that code's verdict."""
    return errors.ContractRefusal(errors.CODES[reason], reason, detail, loosened=loosened)


def _canon(value):
    return util.canonical_json(value)


def _sha(data):
    return util.sha256_hex(data)


def _under(path, folder):
    """True when a path from the repository root lies under `folder` ("." is the whole tree)."""
    return folder in ("", ".") or path == folder or path.startswith(folder.rstrip("/") + "/")


def _within(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _related(a, b):
    """True when a equals b, contains it or lies inside it (both realpaths)."""
    return _within(a, b) or _within(b, a)


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _events(conn, kind):
    return db.events(conn, kind=kind, limit=10 ** 9)


def _rules(block, part, key):
    node = block.get(part) if isinstance(block, dict) else None
    value = node.get(key) if isinstance(node, dict) else None
    return [r for r in value if isinstance(r, str)] if isinstance(value, list) else []


def _data_of(pack):
    """The runbook as research_findings reads it: its stages and knobs."""
    return {"runbook": runbook.RESEARCH_FORMAT, "id": pack["runbook_id"], "research": {},
            "stages": pack["stages"], "knobs": list(pack["knobs"].values())}


# ---------------------------------------------------------------------------------- git reads
def _git_env():
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES"):
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo, *args, stdin=None):
    try:
        return subprocess.run(["git", "-C", repo] + list(args), input=stdin, capture_output=True,
                              env=_git_env(), timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _refuse("CONTRACT-REPO-UNREADABLE", "git could not run in %s: %s" % (repo, exc))


def _repo_state(repo, ref):
    """(the repository root, the commit `ref` resolves to), or CONTRACT-REPO-UNREADABLE."""
    if not os.path.isdir(repo):
        raise _refuse("CONTRACT-REPO-UNREADABLE", "repo.path %s is not a folder" % repo)
    top = _git(repo, "rev-parse", "--show-toplevel")
    here = top.stdout.decode("utf-8", "surrogateescape").strip()
    if top.returncode != 0 or os.path.realpath(here) != os.path.realpath(repo):
        raise _refuse("CONTRACT-REPO-UNREADABLE", "repo.path %s is not the root of a git repository" % repo)
    if not isinstance(ref, str) or not ref.strip() or ref.startswith("-"):
        raise _refuse("CONTRACT-REPO-UNREADABLE", "repo.ref %r is not a git ref" % (ref,))
    found = _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    commit = found.stdout.decode("ascii", "replace").strip()
    if found.returncode != 0 or len(commit) != 40:
        raise _refuse("CONTRACT-REPO-UNREADABLE", "repo.ref %s does not resolve to a commit in %s" % (ref, repo))
    return os.path.realpath(repo), commit


def _tree(repo, commit):
    """{path: (git mode, object id)} of every blob tracked at `commit`."""
    proc = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    if proc.returncode != 0:
        raise _refuse("CONTRACT-REPO-UNREADABLE", "git ls-tree %s failed in %s" % (commit, repo))
    out = {}
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _tab, name = entry.partition(b"\t")
        mode, kind, oid = meta.split(b" ")
        if kind == b"blob":
            out[name.decode("utf-8", "surrogateescape")] = (mode.decode("ascii"), oid.decode("ascii"))
    return out


def _blobs(repo, oids):
    """{object id: bytes} read with one `git cat-file --batch`."""
    wanted = sorted(set(oids))
    if not wanted:
        return {}
    proc = _git(repo, "cat-file", "--batch", stdin=("\n".join(wanted) + "\n").encode("ascii"))
    out, data, at = {}, proc.stdout, 0
    for oid in wanted:
        end = data.index(b"\n", at)
        head = data[at:end].split(b" ")
        if len(head) != 3 or head[1] != b"blob":
            raise _refuse("CONTRACT-REPO-UNREADABLE", "object %s is not a readable blob in %s" % (oid, repo))
        size = int(head[2])
        out[oid] = data[end + 1:end + 1 + size]
        at = end + 1 + size + 1
    return out


def _store_files(folder):
    """{relative path: (bytes, git mode)} of the regular files of a store folder (no symlink is
    followed or kept)."""
    out = {}
    if not os.path.isdir(folder) or os.path.islink(folder):
        return out
    for base, dirs, names in os.walk(folder, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(base, name)
            info = os.lstat(full)
            if not stat.S_ISREG(info.st_mode):
                continue
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            out[rel] = (_read_bytes(full), "100755" if info.st_mode & stat.S_IXUSR else "100644")
    return out


#: a studio pack (op-062 t-653): the pack of a research runbook that lives in an attached project's studio while
#: repo.path is the attached repository. Its files are read from disk, frozen under this prefix, and never
#: reach the run tree, so no pack file can be part of an attempt commit.
_STUDIO_PREFIX = "@studio/"
_STUDIO_MAX_FILES = 2000
_STUDIO_MAX_BYTES = 64 << 20
_STUDIO_SKIP_DIRS = ("__pycache__", ".git")


def _studio_files(folder, skip=()):
    """{path relative to `folder`: (bytes, git mode)} of the regular files under the studio pack folder `folder`,
    without the names in `skip`, without `__pycache__` and `.git` folders and `.pyc` files. A link is not
    followed or kept. More than _STUDIO_MAX_FILES files or _STUDIO_MAX_BYTES bytes is refused: a pack folder
    that large is the wrong folder."""
    out, total = {}, 0
    for base, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _STUDIO_SKIP_DIRS)
        for name in sorted(names):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(base, name)
            info = os.lstat(full)
            if not stat.S_ISREG(info.st_mode):
                continue
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            if rel in skip:
                continue
            content = _read_bytes(full)
            total += len(content)
            if len(out) >= _STUDIO_MAX_FILES or total > _STUDIO_MAX_BYTES:
                raise _refuse("CONTRACT-CHECK-FAILED", "the studio pack folder %s holds more than %d files or %d "
                              "bytes; the runbook folder of a studio pack holds the pack and nothing else"
                              % (folder, _STUDIO_MAX_FILES, _STUDIO_MAX_BYTES))
            out[rel] = (content, "100755" if info.st_mode & stat.S_IXUSR else "100644")
    return out


def _studio_pack_allowed(project, repo_abs, base) -> bool:
    """Whether the attach package lets the pack folder `base` lie outside the repository `repo_abs`."""
    return runbook._pack_outside_allowed(project, repo_abs, os.path.realpath(base))


# -------------------------------------------------------------------------- the pack contract
def load_block(runbook_path: str) -> dict:
    """The pack-level contract of a `runbook: 3` file: runbook.check with its spec first
    (CONTRACT-CHECK-FAILED, CONTRACT-CHECK-BLOCKED), then runbook.expand_research for the
    engine, defaults and runbook layers; then resolve repo.path and repo.ref to the repository
    root and a commit (CONTRACT-REPO-UNREADABLE) and read with git, at that commit, the sha256 of
    every file of `files`: each tracked file under the runbook folder other than the runbook and
    its defaults file, and each tracked file a protected or holdout rule of the pack-level block
    hits (t-088 sections 7.7 and 14.2). Returns the block with the root, the commit, the stages,
    the knobs and `files`. Raises ContractRefusal RESEARCH-NOT-FORMAT-3 for a format 1 or 2
    file."""
    path = os.path.abspath(runbook_path)
    data, report = runbook.load(path)
    if data is None:
        detail = "; ".join(e["message"] for e in report.errors)
        if any(e["code"] == "FILE-UNREADABLE" for e in report.errors):
            raise _refuse("CONTRACT-CHECK-BLOCKED", detail)
        raise _refuse("CONTRACT-CHECK-FAILED", detail)
    if runbook.research_block(data) is None:
        raise _refuse("RESEARCH-NOT-FORMAT-3", "%s is `runbook: %r`%s; the contract loader reads only a "
                      "`runbook: 3` file with a `research` block" % (
                          runbook_path, data.get("runbook"), "" if "research" in data else " with no research block"))
    result = runbook.check(path)
    if result["code"] == vc.BLOCKED:
        raise _refuse("CONTRACT-CHECK-BLOCKED", "\n".join("%s %s: %s" % (e["code"], e["where"], e["message"])
                                                          for e in result["errors"]))
    if result["code"] != vc.PASS:
        codes = {e["code"] for e in result["errors"]}
        detail = "\n".join("%s %s: %s" % (e["code"], e["where"] or "-", e["message"]) for e in result["errors"])
        raise _refuse("CONTRACT-PACK-OUTSIDE-REPO" if "PACK-OUTSIDE-REPO" in codes else "CONTRACT-CHECK-FAILED", detail)
    base = os.path.dirname(path)
    block, layers, _found = runbook.expand_research(data, base)
    extra = runbook._research_pack(data, base)
    project = runbook._project_root(base)
    repo_path, ref_name = block["repo"]["path"], block["repo"]["ref"]
    if os.path.isabs(repo_path):
        repo_abs = repo_path
    elif project is None:
        raise _refuse("CONTRACT-REPO-UNREADABLE", "repo.path %s is relative, and no folder from %s up holds "
                      "project.yaml" % (repo_path, base))
    else:
        repo_abs = os.path.join(project, repo_path)
    top, commit = _repo_state(repo_abs, ref_name)
    studio = False
    if not _within(base, top):
        # only an attached project's studio may hold the pack of a run on the attached repository (R15)
        if not _studio_pack_allowed(project, repo_abs, base):
            raise _refuse("CONTRACT-PACK-OUTSIDE-REPO", "the runbook folder %s does not lie inside repo.path %s"
                          % (base, top))
        studio = True
    tree = _tree(top, commit)
    if studio:
        if any(p == _STUDIO_PREFIX.rstrip("/") or p.startswith(_STUDIO_PREFIX) for p in tree):
            raise _refuse("CONTRACT-PACK-OUTSIDE-REPO", "the repository tracks a path under %s, the name the "
                          "frozen studio pack uses" % _STUDIO_PREFIX)
        folder = _STUDIO_PREFIX + os.path.relpath(os.path.realpath(base), os.path.realpath(project)).replace(os.sep, "/")
    else:
        folder = os.path.relpath(os.path.realpath(base), top).replace(os.sep, "/")

    def from_top(rel):
        return os.path.relpath(os.path.realpath(os.path.join(base, rel)), top).replace(os.sep, "/")

    defaults_name = block.get("defaults")
    rules = _rules(block, "paths", "protected") + _rules(block, "holdout", "paths")
    studio_skip = set()
    if studio:
        # the runbook and its defaults file stay out, as for a pack in the repository; every other regular
        # file under the runbook folder is frozen from disk under @studio/<folder>/
        studio_skip = {os.path.basename(path)}
        if isinstance(defaults_name, str):
            studio_skip.add(os.path.normpath(defaults_name).replace(os.sep, "/"))
        chosen = sorted(p for p in tree if runbook.path_hit(p, rules))
    else:
        own = {from_top(os.path.basename(path))}
        if isinstance(defaults_name, str):
            own.add(from_top(defaults_name))
        # the runbook and its defaults file stay out: their fields are compared one by one (section 7.7)
        chosen = sorted(p for p in tree if p not in own and (_under(p, folder) or runbook.path_hit(p, rules)))
    blobs = _blobs(top, [tree[p][1] for p in chosen])
    files = dict((p, _sha(blobs[tree[p][1]])) for p in chosen)
    on_disk = {}
    if studio:
        on_disk = _studio_files(os.path.realpath(base), studio_skip)
        for rel, (content, _mode) in on_disk.items():
            files["%s/%s" % (folder, rel)] = _sha(content)
    missing = []
    lessons = block.get("lessons") if isinstance(block.get("lessons"), dict) else {}
    named = [("research.lessons.file", lessons.get("file"))]
    repair = block.get("repair") if isinstance(block.get("repair"), dict) else {}
    for n, case in enumerate(repair.get("cases") or []):
        named.append(("research.repair.cases[%d].bug_report" % n, case.get("bug_report")))
    for where, rel in named:
        if not isinstance(rel, str):
            continue
        if studio:
            if os.path.normpath(rel).replace(os.sep, "/") not in on_disk:
                missing.append("FILE-MISSING %s: %s is not a file of the studio pack folder %s, and a run reads a "
                               "pack file only from its frozen copy" % (where, rel, folder))
        elif from_top(rel) not in tree:
            missing.append("FILE-MISSING %s: %s is not tracked at commit %s, and a run reads a pack file only "
                           "from its frozen copy" % (where, from_top(rel), commit))
    if missing:
        raise _refuse("CONTRACT-CHECK-FAILED", "\n".join(missing))
    anchor = project or top

    def from_project(full):
        return os.path.relpath(os.path.realpath(full), os.path.realpath(anchor)).replace(os.sep, "/")

    spec_full = os.path.join(base, data["spec"])
    pack = {"format": runbook.RESEARCH_FORMAT, "runbook": from_project(path), "runbook_id": data["id"],
            "runbook_sha256": _sha(_read_bytes(path)), "pack": extra["pack"], "mode": block["mode"],
            "repo": {"path": repo_path, "ref": ref_name, "commit": commit, "root": top},
            "defaults": None, "spec": {"path": from_project(spec_full), "sha256": _sha(_read_bytes(spec_full))},
            "block": block, "layers": layers, "raw": extra["raw"],
            "stages": data["stages"], "knobs": dict((k["id"], k) for k in data.get("knobs") or []),
            "files": files, "folder": folder, "base_dir": base, "project": project, "_tree": tree}
    if isinstance(defaults_name, str):
        full = os.path.join(base, defaults_name)
        pack["defaults"] = {"path": from_project(full), "sha256": _sha(_read_bytes(full))}
    if studio:
        pack["pack_source"] = "studio"
        pack["_studio_dir"] = os.path.realpath(base)
        pack["_studio_skip"] = sorted(studio_skip)
    pack["pack_sha256"] = pack_sha256(pack)
    return pack


# ----------------------------------------------------------------------- classes and the diffs
def _tpath(field):
    """The table path of a field path: research.metrics[compares].target -> metrics[].target."""
    out, depth, i = [], 0, 0
    text = field[len("research."):] if field.startswith("research.") else field
    while i < len(text):
        c = text[i]
        if c == "[":
            depth += 1
            if depth == 1:
                out.append("[]")
        elif c == "]":
            depth -= 1
        elif depth == 0:
            out.append(c)
        i += 1
    return "".join(out)


def _class_of(tpath):
    name = tpath.rsplit(".", 1)[-1]
    if name in ("covers", "source"):
        return "none"
    return _CLASSES.get(tpath, "fixed")


def _split(field):
    """[step] of a field path: ("k", key) or ("i", key in brackets)."""
    text = field[len("research."):] if field.startswith("research.") else field
    steps, i = [], 0
    while i < len(text):
        if text[i] == ".":
            i += 1
            continue
        if text[i] == "[":
            j = text.index("]", i)
            steps.append(("i", text[i + 1:j]))
            i = j + 1
            continue
        j = i
        while j < len(text) and text[j] not in ".[":
            j += 1
        steps.append(("k", text[i:j]))
        i = j
    return steps


def _get(block, field):
    """The value at a field path of a block, or None."""
    node, tpath = block, ""
    for kind, name in _split(field):
        if kind == "k":
            if not isinstance(node, dict) or name not in node:
                return None
            node, tpath = node[name], "%s.%s" % (tpath, name) if tpath else name
        else:
            key = runbook.KEYED_LISTS.get(tpath)
            if key is None or not isinstance(node, list):
                return None
            hit = [item for item in node if runbook._item_key(item, key) == name]
            if not hit:
                return None
            node, tpath = hit[0], tpath + "[]"
    return node


def _rank(value, words):
    shown = "true" if value is True else "false" if value is False else value
    return words.index(shown) if shown in words else None


def classify(field: str, old, new, block: dict, protected=()) -> str:
    """"tightened", "loosened", "free", "added" or "same" by the class of t-088 section 7.5;
    protected is the keyword runbook-format-3.md section 14.2 defines (the tracked files a
    pack-level protected rule hits; merge passes the pack's, pack_diff the new block's). A target
    reads its metric's direction from block; a new metric, and a metric or a case a pack edit
    drops, is "loosened"; so is a paths.hidden or holdout.paths list that adds a rule which
    runbook.path_hit matches to a path of protected (built by t-096). A research.budget cap that
    is dropped is "loosened" here, as any dropped bound; pack_diff alone frees the drop of a cap
    a pre-t-297 baseline took from the pack defaults file (t-297, _dropped_default_cap)."""
    if _canon(old) == _canon(new):
        return "same"
    tpath = _tpath(field)
    if tpath.endswith("[]"):
        items = tpath[:-2]
        if old is None:
            return "loosened" if items == "metrics" else "added"
        if new is None:
            return "loosened" if items in ("metrics", "repair.cases", "proposal") else "free"
        return "loosened"
    klass = _class_of(tpath)
    if klass == "none":
        return "free" if tpath.rsplit(".", 1)[-1] in ("covers", "source", "defaults") else "loosened"
    if klass == "free":
        return "free"
    if klass == "fixed":
        return "loosened"
    if klass in ("higher", "lower"):
        if old is None:
            return "tightened"                    # a value set where none was
        if new is None or not runbook._is_number(new) or not runbook._is_number(old):
            return "loosened"
        up = new > old
        return ("tightened" if up else "loosened") if klass == "higher" else ("loosened" if up else "tightened")
    if klass == "target":
        if old is None or new is None or not runbook._is_number(new) or not runbook._is_number(old):
            return "loosened"                     # a target set where none was, or dropped
        metric = _get(block, field.rsplit(".", 1)[0]) or {}
        lower = metric.get("direction") == "lower"
        return "tightened" if (new < old if lower else new > old) else "loosened"
    if klass.startswith("order "):
        words = [w.strip() for w in klass[len("order "):].split(",")]
        after = _rank(new, words)
        if old is None:
            return "tightened" if after == 0 else "loosened"
        before = _rank(old, words)
        if after is None or before is None:
            return "loosened"
        return "tightened" if after < before else "loosened"
    if tpath == "gates":
        seq = lambda v: [g.get("stage") for g in v if isinstance(g, dict)] if isinstance(v, list) else []  # noqa: E731
        was, now = seq(old), seq(new)
        at = [now.index(s) if s in now else -1 for s in was]
        if -1 in at or at != sorted(at):
            return "loosened"
        return "tightened" if len(now) > len(was) else "free"
    was = {_canon(v) for v in old} if isinstance(old, list) else set()
    now = {_canon(v) for v in new} if isinstance(new, list) else set()
    if klass == "subset":
        return "tightened" if now <= was else "loosened"
    if not now >= was:
        return "loosened"
    if tpath in ("paths.hidden", "holdout.paths"):
        added = [json.loads(v) for v in now - was]
        added = [r for r in added if isinstance(r, str)]
        if added and any(runbook.path_hit(p, added) for p in protected):
            return "loosened"                     # it takes a protected file out of ${CONTRACT_DIR}
    return "tightened"


def _protected_files(files, block):
    """The paths of `files` a protected rule of `block` hits (classify's `protected`)."""
    rules = _rules(block, "paths", "protected")
    return sorted(p for p in files if runbook.path_hit(p, rules))


def _class_name(field):
    tpath = _tpath(field)
    return "item" if tpath.endswith("[]") else _class_of(tpath)


def _override_leaves(node, tpath, field, pack_block, tables, problems, leaves):
    """Walk a task layer: collect the field path of every leaf it sets (a new item of a keyed list
    is one entry), and a problem for each field of class none, each new metric or case, and each
    item that names no key."""
    for key, value in node.items():
        if not isinstance(key, str):
            problems.append("%s: a key must be text, found %r" % (field, key))
            continue
        sub = "%s.%s" % (tpath, key) if tpath else key
        path = "%s.%s" % (field, key)
        if _class_of(sub) == "none":
            problems.append("%s: no task layer may set it (class none)" % path)
            continue
        keyed = runbook.KEYED_LISTS.get(sub)
        if keyed and isinstance(value, list):
            for item in value:
                name = runbook._item_key(item, keyed)
                if name is None:
                    problems.append("%s: every item names its `%s`" % (path, keyed))
                    continue
                item_path = "%s[%s]" % (path, name)
                if _get(pack_block, item_path) is None:
                    if sub in ("metrics", "repair.cases"):
                        problems.append("%s: a task layer may not add a %s; each covers a spec item and no "
                                        "override sets covers" % (item_path, "metric" if sub == "metrics" else "case"))
                    else:
                        leaves.append(item_path)
                    continue
                rest = dict((k, v) for k, v in item.items() if k != keyed)
                _override_leaves(rest, sub + "[]", item_path, pack_block, tables, problems, leaves)
        elif isinstance(value, dict) and sub in tables:
            _override_leaves(value, sub, path, pack_block, tables, problems, leaves)
        else:
            leaves.append(path)


def merge(pack: dict, override: dict | None) -> tuple[dict, list[OverrideDiff]]:
    """Adds the task layer (runbook.merge_research), refuses one that adds a metric or a case or
    breaks a checker rule (CONTRACT-OVERRIDE-INVALID), and returns the merged block and the diff
    of t-088 section 7.6, one OverrideDiff per changed leaf, classed by classify (built by
    t-096)."""
    if override is None:
        return runbook._copy(pack["block"]), []
    if not isinstance(override, dict):
        raise _refuse("CONTRACT-OVERRIDE-INVALID", "the research part of a task contract is a mapping, found %s"
                      % type(override).__name__)
    problems, leaves = [], []
    tables = runbook._research_tables(pack["block"].get("mode"))
    _override_leaves(override, "", "research", pack["block"], tables, problems, leaves)
    if problems:
        raise _refuse("CONTRACT-OVERRIDE-INVALID", "\n".join(problems))
    merged = runbook._engine_fill(runbook.merge_research(pack["raw"], override))
    found, _warnings = runbook.research_findings(_data_of(pack), merged, pack["base_dir"], True,
                                                 repo_root=pack["repo"]["root"])
    if found:
        raise _refuse("CONTRACT-OVERRIDE-INVALID", "\n".join("%s %s: %s" % (e["code"], e["where"], e["message"])
                                                             for e in found))
    protected = _protected_files(pack["files"], pack["block"])
    diffs = []
    for field in sorted(set(leaves)):
        old, new = _get(pack["block"], field), _get(merged, field)
        direction = classify(field, old, new, merged, protected)
        if direction != "same":
            diffs.append(OverrideDiff(field=field, pack=old, task=new, field_class=_class_name(field),
                                      direction=direction, grant=None))
    return merged, diffs


def _leaves(block):
    """({field path: value} of every leaf, {field path: item} of every keyed-list item)."""
    leaves, items = {}, {}
    for _where, chain, tpath, value in runbook._nodes(block if isinstance(block, dict) else {}):
        if not chain:
            continue
        if chain[-1][0] == "i":
            items[runbook._keyed(chain)] = value
        elif runbook._is_leaf(tpath, value):
            leaves[runbook._keyed(chain)] = value
    return leaves, items


def _entry(field, old, new, klass, direction):
    return {"field": field, "baseline": old, "pack": new, "class": klass, "direction": direction, "grant": None}


def _dropped_default_cap(field, new, pack, baseline) -> bool:
    """True when field is a research.budget cap that the new block drops and that the baseline
    took from the pack defaults file before t-297, when `usd` and `tokens` were required, so every
    pack had to write a value nobody chose (the refpack baselines, events 65028 and 92293). Such a
    baseline has no `layers` map; the cap came from the defaults file when the baseline names one,
    the runbook's bytes are the baseline's (pack runbook_sha256 equal) and the runbook does not set
    the field now, so it did not set it then either. Its drop returns the run to the owner's uncapped default (owner
    decision dec-36f614298507, in-session-runtime.md section 21, t-297). A baseline landed since
    records `layers`; caps are optional for it, so any cap it holds was written on purpose (the
    runbook, its defaults file or the owner) and its drop stays a loosening that needs the owner's
    loosen grant, as does the drop of a cap the runbook set."""
    if new is not None or not _tpath(field).startswith("budget."):
        return False
    if isinstance(baseline.get("layers"), dict):
        return False
    return bool(baseline.get("defaults")) and bool(pack.get("runbook_sha256")) \
        and pack.get("runbook_sha256") == baseline.get("runbook_sha256") \
        and (pack.get("layers") or {}).get(field) != "runbook"


def pack_diff(pack: dict, baseline: dict | None) -> list:
    """t-088's pack-level diff (runbook-format-3.md sections 7.7 and 14.2): what load_block
    returned against the data of the newest research-contract event for this runbook id and path;
    [] when baseline is None (a first block). One entry {field, baseline, pack, class, direction,
    grant} per changed block leaf, classed by classify, and one per stage, knob and file of
    `files` that changed, is new or is gone, and for a changed spec path, each with class "fixed"
    and direction "loosened"; a path that enters or leaves `files` only through a changed
    protected or holdout rule is left to that rule's own entry (built by t-096)."""
    if not baseline:
        return []
    out = []
    old_block, new_block = baseline.get("block") or {}, pack["block"]
    protected = _protected_files(pack["files"], new_block)
    old_leaves, old_items = _leaves(old_block)
    new_leaves, new_items = _leaves(new_block)
    lone = [p for p in set(old_items) ^ set(new_items)]
    for field in sorted(set(old_items) ^ set(new_items)):
        old, new = old_items.get(field), new_items.get(field)
        out.append(_entry(field, old, new, "item", classify(field, old, new, new_block, protected)))
    for field in sorted(set(old_leaves) | set(new_leaves)):
        if any(field.startswith(item + ".") for item in lone):
            continue
        old, new = old_leaves.get(field), new_leaves.get(field)
        direction = classify(field, old, new, new_block, protected)
        if direction == "loosened" and _dropped_default_cap(field, new, pack, baseline):
            direction = "free"
        if direction != "same":
            out.append(_entry(field, old, new, _class_name(field), direction))

    def keyed(value):
        if isinstance(value, dict):
            return value
        return dict((v["id"], v) for v in value or [] if isinstance(v, dict) and isinstance(v.get("id"), str))

    for label, was, now in (("stages", keyed(baseline.get("stages")), keyed(pack["stages"])),
                            ("knobs", keyed(baseline.get("knobs")), keyed(pack["knobs"]))):
        for name in sorted(set(was) | set(now)):
            if _canon(was.get(name)) != _canon(now.get(name)):
                out.append(_entry("%s[%s]" % (label, name), was.get(name), now.get(name), "fixed", "loosened"))
    old_spec = (baseline.get("spec") or {}).get("path")
    if old_spec != pack["spec"]["path"]:
        out.append(_entry("spec", old_spec, pack["spec"]["path"], "fixed", "loosened"))
    was, now, folder = baseline.get("files") or {}, pack["files"], pack["folder"]
    old_rules = _rules(old_block, "paths", "protected") + _rules(old_block, "holdout", "paths")
    new_rules = _rules(new_block, "paths", "protected") + _rules(new_block, "holdout", "paths")
    for path in sorted(set(was) | set(now)):
        a, b = was.get(path), now.get(path)
        if a == b:
            continue
        if a is None and not (_under(path, folder) or runbook.path_hit(path, old_rules)):
            continue                              # it came in with a rule the new block added
        if b is None and not (_under(path, folder) or runbook.path_hit(path, new_rules)):
            continue                              # it left with a rule the new block dropped
        out.append(_entry("files[%s]" % path, a, b, "fixed", "loosened"))
    return out


# ------------------------------------------------------------------------------ the grants
def _entry_values(entry):
    """(field, direction, accepted, new) of a task-layer OverrideDiff, its contract.json form, or a
    pack_diff entry."""
    if isinstance(entry, OverrideDiff):
        return entry.field, entry.direction, entry.pack, entry.task
    if "baseline" in entry:
        return entry["field"], entry["direction"], entry.get("baseline"), entry.get("pack")
    return entry["field"], entry["direction"], entry.get("pack"), entry.get("task")


def _grant_terms(data):
    terms = data.get("terms")
    terms = terms.get("set") if isinstance(terms, dict) else None
    return terms if isinstance(terms, dict) else None


def _loosen_grant(conn, ref_):
    """(decision id, {field path: value}) of the loosen grant owner.authorized accepts on ref_,
    or (None, {}). The terms are those of the event authorized accepted: a research-grant event on
    ref_ with gate loosen, confirmed "terminal" and that decision. When any research-grant event
    that names the decision carries other terms (an appended event, confirmed or not), the grant
    is ambiguous and grants nothing, so every loosening stays CONTRACT-LOOSEN-UNGRANTED."""
    ok, decision = owner.authorized(conn, ref_, "loosen")
    if not ok:
        return None, {}
    accepted, seen = None, set()
    for event in _events(conn, owner.KIND_GRANT):
        data = event["data"] if isinstance(event["data"], dict) else {}
        if data.get("decision") != decision:
            continue
        seen.add(_canon(_grant_terms(data)))
        if data.get("gate") == "loosen" and data.get("confirmed") == "terminal" and \
                (event.get("ref") == ref_ or data.get("ref") == ref_):
            accepted = _grant_terms(data)
    if accepted is None or len(seen) != 1:
        return decision, {}
    return decision, accepted


def ungranted(diffs: list, *, ref: str, conn) -> list[dict]:
    """The loosenings of one layer that no term of owner.authorized(conn, ref, "loosen") grants,
    each as {"ref": ref, "field": <field path, or the stage, knob, spec or `files` item>,
    "accepted": <the value the grant is measured against: the pack value for a task override,
    the baseline's value for a pack edit, None for an item the baseline lacks>, "new": <the value
    now, None for an item that is gone>}. A term grants a field when its path is the field's and
    its value has the same util.canonical_json text. Raises nothing; [] when every loosening is
    granted (built by t-096)."""
    loose = [_entry_values(d) for d in diffs]
    loose = [d for d in loose if d[1] == "loosened"]
    if not loose:
        return []
    _decision, terms = _loosen_grant(conn, ref)
    out = []
    for field, _direction, accepted, new in loose:
        if field in terms and _canon(terms[field]) == _canon(new):
            continue
        out.append({"ref": ref, "field": field, "accepted": accepted, "new": new})
    return out


def check_override(layers: list[tuple[str, list]], *, conn) -> None:
    """Runs ungranted() for every (ref, diffs) layer first, then raises one ContractRefusal
    CONTRACT-LOOSEN-UNGRANTED (exit 3) when any entry is left, with `loosened` the entries of all
    layers in layer order and `detail` one line per entry (built by t-096)."""
    entries = []
    for ref_, diffs in layers:
        entries += ungranted(diffs, ref=ref_, conn=conn)
    if entries:
        detail = "\n".join("%s %s: accepted %s, new %s" % (e["ref"], e["field"], _canon(e["accepted"]),
                                                             _canon(e["new"])) for e in entries)
        raise _refuse("CONTRACT-LOOSEN-UNGRANTED", detail, loosened=entries)


def _mark_grants(conn, ref_, diffs):
    """Write the granting decision id into each granted loosening of `diffs` (dicts) and return
    the ids used."""
    decision, terms = _loosen_grant(conn, ref_)
    used = []
    for entry in diffs:
        field, direction, _accepted, new = _entry_values(entry)
        if direction == "loosened" and decision and field in terms and _canon(terms[field]) == _canon(new):
            entry["grant"] = decision
            if decision not in used:
                used.append(decision)
    return used


# ------------------------------------------------------------------------ overlap and paths
def _forbidden(root, repo, hidden, holdout, run=None):
    """The realpaths no read path, shared path or tool may equal, contain or lie inside (rule 2
    of check_overlap)."""
    home = os.path.expanduser("~")
    out = [os.path.join(root, name) for name in FORBIDDEN_READ] + [root]
    if run is not None:
        out += [paths.run_dir(root, run), paths.worktrees_dir(root, run)]
    out.append(paths.machine_lock_dir())
    out += [home, os.path.join(home, ".claude"), os.path.join(home, ".codex")]
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_RUNTIME_DIR"):
        if os.environ.get(name):
            out.append(os.environ[name])
    out.append("/run/user/%d" % os.getuid())
    for rule in list(hidden) + list(holdout):
        body = rule.rstrip("/")
        while body.startswith("./"):
            body = body[2:]
        if body:
            out += [os.path.join(root, body), os.path.join(repo, body)]
    seen, keep = set(), []
    for path in out:
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            keep.append(real)
    return keep


def _tool_dirs(forbidden):
    """The tool PATH: the caller's PATH without any folder that equals or lies inside a forbidden
    path."""
    out = []
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder and not any(_within(folder, f) for f in forbidden):
            out.append(folder)
    return out


def _resolve_tool(name, dirs):
    for folder in dirs:
        full = os.path.join(folder, name)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return os.path.realpath(full)
    return None


def _kind(mode):
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return None


def _walk_findings(real):
    """Rules (4) and (5) of check_overlap on one realpath."""
    out = []
    try:
        top = os.lstat(real)
    except OSError:
        return ["%s: missing" % real]
    kind = _kind(top.st_mode)
    if kind:
        return ["%s: %s %s" % (real, kind, real)]
    if not stat.S_ISDIR(top.st_mode):
        return out
    for base, dirs, names in os.walk(real, followlinks=False):
        info = os.lstat(base)
        if os.access(base, os.W_OK) or info.st_mode & stat.S_IWOTH:
            out.append("%s: writable %s" % (real, base))
        for name in sorted(dirs + names):
            entry = os.path.join(base, name)
            try:
                kind = _kind(os.lstat(entry).st_mode)
            except OSError:
                continue
            if kind:
                out.append("%s: %s %s" % (real, kind, entry))
    return out


def check_read_paths(contract: Contract) -> list[str]:
    """Rules (4) and (5) of check_overlap alone, over every realpath of contract.read_real: one
    finding per offending path, "<realpath>: socket|fifo|device <entry>" or "<realpath>:
    writable <folder>" (and "<realpath>: missing"), from one os.walk(followlinks=False) and
    os.lstat walk; [] when all are clean. It raises nothing and reads no record (built by
    t-096)."""
    out = []
    for real in contract.read_real:
        if isinstance(real, str) and real:
            out += _walk_findings(real)
    return out


def _repo_abs(root, contract):
    repo = contract.repo_path
    return os.path.realpath(repo if os.path.isabs(repo) else os.path.join(root, repo))


def _tracked(repo, commit):
    if commit:
        return sorted(_tree(repo, commit))
    proc = _git(repo, "ls-files", "-z")
    if proc.returncode != 0:
        raise _refuse("CONTRACT-REPO-UNREADABLE", "git ls-files failed in %s" % repo)
    return sorted(p.decode("utf-8", "surrogateescape") for p in proc.stdout.split(b"\0") if p)


def check_overlap(root: str, contract: Contract, *, run: str | None = None) -> None:
    """Raises CONTRACT-PACK-OUTSIDE-REPO when the runbook folder is not inside repo_path (unless the project is
    attached, repo_path is the attached repository and the folder is a plain folder of the studio: op-062
    t-653, a studio pack), and
    CONTRACT-UNPROTECTED for a tracked file under the runbook folder that no protected rule hits
    (t-088 section 14.2). Raises CONTRACT-ALLOWLIST-OVERLAP (1) when an editable rule matches
    engine code (alpaca/, bin/, setup/), .alpaca/, a protected path, a hidden or held-out path,
    or the reproducer store; (2) when the realpath of a declared read path or shared path
    equals, contains or lies inside any forbidden path: <root>/<name> for each FORBIDDEN_READ
    name, the project root itself, the run folder and the worktrees folder of `run`,
    paths.machine_lock_dir(), the operator's home, each held-out and hidden path (in the main
    checkout and in the target repository), the runtime config homes (~/.claude, ~/.codex,
    $CLAUDE_CONFIG_DIR, $CODEX_HOME), and the user runtime folders ($XDG_RUNTIME_DIR when set,
    and /run/user/<uid> always); (3) when a tool of contract.tools, resolved on the tool PATH
    (the caller's PATH without its forbidden folders), has a realpath that equals or lies inside
    a forbidden path, or does not resolve; (4) when the realpath of a declared read path or
    shared path, or any entry under it, is a socket, a FIFO or a device; or (5) when that
    realpath, or any folder under it, is writable by the runner's user or world-writable
    (architecture.md section 3.5; built by t-096)."""
    root = os.path.realpath(root)
    repo = _repo_abs(root, contract)
    folder_abs = os.path.realpath(os.path.dirname(os.path.join(root, contract.runbook)))
    studio = not _within(folder_abs, repo)
    if studio:
        # a pack outside the repository is a studio pack, frozen from disk and never in the repository, so no
        # tracked file is "loose". Whether it may be one is asked again here, from the spelling of repo.path as
        # the contract holds it, so a contract.json that puts its pack outside the repository in a project that
        # is not attached to that repository is refused, as before
        spelled = contract.repo_path if os.path.isabs(contract.repo_path) else os.path.join(root, contract.repo_path)
        if not _studio_pack_allowed(root, spelled, folder_abs):
            raise _refuse("CONTRACT-PACK-OUTSIDE-REPO", "the runbook folder %s does not lie inside repo.path %s"
                          % (folder_abs, repo))
    tracked = _tracked(repo, contract.repo_commit)
    if not studio:
        folder = os.path.relpath(folder_abs, repo).replace(os.sep, "/")
        loose = [p for p in tracked if _under(p, folder) and not runbook.path_hit(p, contract.protected)]
        if loose:
            raise _refuse("CONTRACT-UNPROTECTED", "every tracked file under the runbook folder %s must be hit by a "
                          "protected rule; no rule hits: %s" % (folder, ", ".join(loose)))
    problems = []
    hides = list(contract.hidden) + list(contract.holdout_paths)
    for rule in contract.editable:
        for engine in ENGINE_PATHS + (STORE_ROOT + "/",):
            if runbook._overlap(rule, engine) or runbook.path_admitted(engine + "x", [rule]):
                problems.append("editable rule %s reaches %s" % (rule, engine))
        for kind, others in (("protected", contract.protected), ("hidden or held-out", hides)):
            for other in others:
                how = runbook._overlap(rule, other)
                if how:
                    problems.append("editable rule %s %s %s rule %s" % (rule, how, kind, other))
        for path in tracked:
            if runbook.path_admitted(path, [rule]):
                hit = runbook.path_hit(path, contract.protected)
                if hit:
                    problems.append("editable rule %s admits %s, which protected rule %s hits" % (rule, path, hit))
                    continue
                hit = runbook.path_hit(path, hides)
                if hit:
                    problems.append("editable rule %s admits %s, which hidden or holdout rule %s hits"
                                    % (rule, path, hit))
    forbidden = _forbidden(root, repo, contract.hidden, contract.holdout_paths, run)
    reads = list(zip(contract.read_paths, contract.read_real or [None] * len(contract.read_paths)))
    shared = [(p, None) for p in contract.shared]
    walked = []
    for label, pairs in (("read path", reads), ("shared path", shared)):
        for declared, real in pairs:
            real = real or os.path.realpath(declared if os.path.isabs(declared) else os.path.join(root, declared))
            walked.append(real)
            for bad in forbidden:
                if _related(real, bad):
                    problems.append("%s %s (%s) meets forbidden path %s" % (label, declared, real, bad))
                    break
    dirs = _tool_dirs(forbidden)
    reals = list(contract.tools_real) or [_resolve_tool(t, dirs) for t in contract.tools]
    for name, real in zip(contract.tools, reals):
        if not real:
            problems.append("tool %s does not resolve on the tool PATH (the PATH without its forbidden folders)" % name)
            continue
        for bad in forbidden:
            if _within(real, bad):
                problems.append("tool %s resolves to %s, inside forbidden path %s" % (name, real, bad))
                break
    for real in walked:
        problems += _walk_findings(real)
    if problems:
        raise _refuse("CONTRACT-ALLOWLIST-OVERLAP", "\n".join(problems))


# ---------------------------------------------------------------------- the contract object
def from_block(contract_obj: dict) -> Contract:
    """The Contract of t-088's contract object (runbook-format-3.md section 14.6), every field of
    section 12.2 read from it and `block` kept whole; sha256 and files_sha256 are left empty
    until freeze hashes the frozen files and fills both with dataclasses.replace (built by
    t-096)."""
    c = contract_obj
    b = c.get("block") or {}
    engine = runbook.RESEARCH_DEFAULTS             # the fallbacks are the engine layer's, not literals
    f2p_engine = engine["repair.cases[].fail_to_pass"]
    part = lambda key: b.get(key) if isinstance(b.get(key), dict) else {}  # noqa: E731
    timeouts = dict((s["id"], s.get("timeout")) for s in c.get("stages") or []
                    if isinstance(s, dict) and isinstance(s.get("id"), str))
    metrics = []
    for m in b.get("metrics") or []:
        repeats = int(m.get("repeats", 1))
        metrics.append(rtypes.Metric(
            id=m["id"], command=m.get("command", ""), parser=m.get("parser", ""), unit=m.get("unit", ""),
            direction=m.get("direction", ""), kind=m.get("kind", ""), repeats=repeats,
            min_ok=int(m.get("min_ok", repeats)), margin=float(m.get("margin", 0)),
            timeout_s=int(m.get("timeout", 0)), median=m.get("median", engine["metrics[]"]["median"]), seeds=tuple(m.get("seeds") or ()),
            workdir=m.get("workdir"), path=m.get("path"), field=m.get("field"),
            target=float(m["target"]) if runbook._is_number(m.get("target")) else None,
            artifact=m.get("artifact"), description=m.get("description")))
    accept, paths_, finger = part("accept"), part("paths"), part("fingerprint")
    stop, budget, lessons, controls = part("stop"), part("budget"), part("lessons"), part("controls")
    holdout, repair, runtime = part("holdout"), part("repair"), part("runtime")
    verifier, audit, egress = part("verifier"), part("audit"), part("egress")
    items = []
    for n, case in enumerate(repair.get("cases") or []):
        f2p = case.get("fail_to_pass") or {}
        items.append(rtypes.RepairItem(
            index=n, case_id=case["id"], bug_report=case.get("bug_report", ""),
            fail_to_pass=rtypes.FailToPassSpec(
                origin=f2p.get("origin", ""), command=f2p.get("command", f2p_engine["command"]),
                tests=tuple(f2p.get("tests") or ()), timeout_s=int(f2p.get("timeout", f2p_engine["timeout"])),
                repeats=int(f2p.get("repeats", f2p_engine["repeats"]))),
            base=case.get("base"), suspect_files=tuple(case.get("suspect_files") or ())))
    p2p = repair.get("pass_to_pass")
    t_out = runtime.get("timeouts") if isinstance(runtime.get("timeouts"), dict) else {}
    effort = runtime.get("effort") if isinstance(runtime.get("effort"), dict) else {}
    repo = c.get("repo") or {}
    return Contract(
        runbook=c.get("runbook", ""), runbook_id=c.get("runbook_id", ""), runbook_sha256=c.get("runbook_sha256", ""),
        pack=c.get("pack"), task=c.get("task"), ref=c.get("ref") or ref(c.get("runbook", ""), c.get("task")),
        mode=b.get("mode", ""), repo_path=repo.get("path", "."), repo_ref=repo.get("ref", "HEAD"),
        repo_commit=repo.get("commit") or "", stages=tuple(c.get("stages") or ()), knobs=dict(c.get("knobs") or {}),
        metrics=tuple(metrics), targets=tuple(rtypes.Target(m.id, m.target) for m in metrics if m.target is not None),
        accept_rule=accept.get("rule", ""), after_targets=accept.get("after_targets"),
        editable=tuple(paths_.get("editable") or ()), protected=tuple(paths_.get("protected") or ()),
        hidden=tuple(paths_.get("hidden") or ()), tests=tuple(paths_.get("tests") or ()),
        read_paths=tuple(paths_.get("read") or ()), read_real=tuple(c.get("read_real") or ()),
        keep=tuple(paths_.get("keep") or ()), outputs=tuple(paths_.get("outputs") or ()),
        tools=tuple(finger.get("tools") or ()), tools_real=tuple(c.get("tools_real") or ()),
        shared=tuple(finger.get("shared") or ()),
        gates=tuple(rtypes.GateStage(stage=g["stage"], timeout_s=int(timeouts.get(g["stage"]) or 0),
                                     heavy=bool(g.get("heavy", False)),
                                     lock_timeout_s=int(g.get("lock_timeout", engine["gates[]"]["lock_timeout"])))
                    for g in b.get("gates") or []),
        slots=int(stop.get("slots", 1)), rounds=int(stop.get("rounds", 1)),
        # a cap the contract leaves out is no cap (None), never 0 (t-297, in-session-runtime.md
        # section 21): a run with no budget set on purpose is uncapped
        budget_usd=float(budget["usd"]) if runbook._is_number(budget.get("usd")) else None,
        budget_tokens=int(budget["tokens"]) if runbook._is_number(budget.get("tokens")) else None,
        budget_wall_s=int(budget["wall"]) if runbook._is_number(budget.get("wall")) else None,
        disk_gib=float(b.get("disk", 0)),
        proposal=tuple(rtypes.ProposalField(field=p["field"], type=p.get("type", "str"),
                                            required=bool(p.get("required", True)), values=tuple(p.get("values") or ()),
                                            max=p.get("max"), description=p.get("description"))
                       for p in b.get("proposal") or []),
        categories=tuple(rtypes.Category(id=x["id"], text=x.get("text", "")) for x in b.get("categories") or []),
        lessons_file=lessons.get("file"), note_chars=int(lessons.get("note_chars", texts.NOTE_MAX_CHARS)),
        noop_rounds=int(controls.get("noop_rounds", 0)), controls_random=bool(controls.get("random", False)),
        controls_mutate=controls.get("mutate"), controls_syntax=controls.get("syntax"),
        controls_seed=int(controls.get("seed", engine["controls"]["seed"])),
        holdout_stages=tuple(holdout.get("stages") or ()), holdout_paths=tuple(holdout.get("paths") or ()),
        repair=tuple(items),
        pass_to_pass=rtypes.PassToPassSpec(command=p2p.get("command", ""), min_tests=int(p2p.get("min_tests", 0)),
                                           timeout_s=int(p2p.get("timeout", 0))) if isinstance(p2p, dict) else None,
        repair_prepare=repair.get("prepare"),
        coupled=tuple(rtypes.CoupledGroup(id=g["id"], files=tuple(g.get("files") or ()), note=g.get("note"))
                      for g in b.get("coupled") or []),
        agents=tuple(rtypes.AgentSettings(role=label, timeout_s=t_out.get(key), effort=effort.get(key))
                     for key, label in rtypes.AGENT_ROLE_KEYS.items()),
        infra_retries=int(runtime.get("retries", engine["runtime"]["retries"])),
        verifier_timeout_s=int(verifier.get("timeout", engine["verifier"]["timeout"])),
        verifier_rerun_timeout_s=int(verifier.get("rerun_timeout", engine["verifier"]["rerun_timeout"])),
        audit_hints=audit.get("hints"), audit_assert_tokens=tuple(audit.get("assert_tokens") or ()),
        audit_build_files=tuple(audit.get("build_files") or ()), audit_steering=tuple(audit.get("steering") or ()),
        egress_extra=tuple(egress.get("allow") or ()), block=b,
        override_diff=tuple(OverrideDiff(field=d["field"], pack=d.get("pack"), task=d.get("task"),
                                         field_class=d.get("class", ""), direction=d.get("direction", ""),
                                         grant=d.get("grant")) for d in c.get("override_diff") or []),
        grants=tuple(c.get("grants") or ()), reproducers=dict(c.get("reproducers") or {}),
        pack_sha256=c.get("pack_sha256", ""), sha256="", files_sha256="")


def ref(runbook: str, task: str | None) -> str:
    """The contract ref of operator-surface section 4.2: contract:<runbook path>#<task id or ->
    (built by t-096)."""
    return "contract:%s#%s" % (runbook, task or "-")


def contract_sha256(contract_obj: dict) -> str:
    """t-088's run contract hash (runbook-format-3.md section 7.8): sha256 of util.canonical_json
    (alpaca/util.py:22-23) of the contract object without task, ref, grants, layers,
    override_diff and repo.commit (and the task's raw `override`, the two hash keys and the host
    realpaths read_real and tools_real, which section 14.6 does not list and which depend on the
    caller's PATH, cwd and machine); it covers
    the merged block, the stages, the knobs, the pack file hashes and files_sha256. The no-op
    burn-in keys on it (FR-044; built by t-096)."""
    obj = dict((k, v) for k, v in contract_obj.items() if k not in _NOT_HASHED)
    if isinstance(obj.get("repo"), dict):
        obj["repo"] = dict((k, v) for k, v in obj["repo"].items() if k != "commit")
    return _sha(_canon(obj))


def pack_sha256(pack: dict) -> str:
    """t-088's pack-level hash (section 7.8) over what load_block returns: the runbook id and
    path, the pack-level block, the stages, the knobs, the spec, the defaults file's sha256 and
    `files`; no base commit and no task layer, so a new commit that changes none of those files
    leaves it the same (built by t-096)."""
    part = {"runbook_id": pack["runbook_id"], "runbook": pack["runbook"], "block": pack["block"],
            "stages": pack["stages"], "knobs": pack["knobs"], "spec": pack["spec"],
            "defaults": (pack.get("defaults") or {}).get("sha256"), "files": pack["files"]}
    return _sha(_canon(part))


def _baseline_event(conn, runbook_id, runbook_path):
    """The newest research-contract event of this runbook id and path, or None."""
    for event in reversed(_events(conn, KIND_CONTRACT)):
        data = event["data"] if isinstance(event["data"], dict) else {}
        if event.get("ref") == runbook_id and data.get("runbook") == runbook_path:
            return event
    return None


def land(conn, contract, *, op: str | None, via: str, session: str, actor: str) -> dict | None:
    """One research-contract event when pack_sha256 differs from the newest one for this runbook
    id and path (t-088 section 14.3); None when it is the same. Intake calls it with via
    "intake", freeze with via "loader" (built by t-096)."""
    newest = _baseline_event(conn, contract["runbook_id"], contract["runbook"])
    if newest is not None and newest["data"].get("pack_sha256") == contract["pack_sha256"]:
        return None
    data = {"runbook": contract["runbook"], "runbook_id": contract["runbook_id"],
            "runbook_sha256": contract["runbook_sha256"], "defaults": contract.get("defaults"),
            "spec": contract["spec"], "mode": contract.get("mode") or contract["block"].get("mode"),
            "block": contract["block"], "stages": contract["stages"], "knobs": contract["knobs"],
            "files": contract["files"], "pack_sha256": contract["pack_sha256"], "via": via,
            "grants": list(contract.get("grants") or [])}
    if isinstance(contract.get("layers"), dict):
        data["layers"] = contract["layers"]       # the layer of each leaf; marks a baseline landed since t-297
    return events.emit(conn, events.CONTRACT, run=None, ref=contract["runbook_id"], op=op, session=session,
                       actor=actor, data=data)


# ----------------------------------------------------------------------------- live runs
def _live_runs(conn):
    """[(run id, research-run-start event)] of every run with no research-finalize event."""
    ended = set()
    for event in _events(conn, events.FINALIZE):
        data = event["data"] if isinstance(event["data"], dict) else {}
        ended.add(data.get("run") or event.get("ref"))
    out, seen = [], set()
    for event in _events(conn, events.RUN_START):
        data = event["data"] if isinstance(event["data"], dict) else {}
        run = data.get("run") or event.get("ref")
        if run not in ended and run not in seen:
            seen.add(run)                         # a run is bound to its first run-start
            out.append((run, event))
    return out


def _intent_contract(conn, start):
    """The frozen contract object of a run, read from the intent record its run-start names."""
    data = start["data"] if isinstance(start["data"], dict) else {}
    rec = intent.get(conn, data.get("intent")) if data.get("intent") else None
    if not rec:
        return None
    try:
        obj = json.loads(rec.get("done_when") or "")
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def guard_task_contract(conn, task: str, data: dict, *, session: str) -> None:
    """Called by taskcontract.record: a research part that differs from the frozen override of a
    live run is recorded as research-contract-drift (code CONTRACT-EDIT-DURING-RUN) and raises
    ValueError (t-088 section 14.2). A live run is one with a research-run-start event and no
    research-finalize; it is bound to the task its frozen contract names. For a task no live run
    names this reads the record and writes nothing (built by t-096)."""
    now = data.get("research") if isinstance(data, dict) else None
    for run, start in _live_runs(conn):
        frozen = _intent_contract(conn, start)
        if not frozen or frozen.get("task") != task:
            continue
        before = frozen.get("override")
        if _canon(before) == _canon(now):
            continue
        events.emit(conn, events.CONTRACT_DRIFT, run=run, ref=run, op=start.get("op"), session=session,
                    actor=_AUTHOR, code=vc.FAIL, reason="CONTRACT-EDIT-DURING-RUN",
                    data={"what": "override", "field": "research", "frozen": before, "now": now})
        raise ValueError("task %s is bound to live research run %s, which froze its research part; an edit "
                         "is refused and recorded (CONTRACT-EDIT-DURING-RUN)" % (task, run))


# --------------------------------------------------------------------- preview and freeze
def _task_override(conn, task):
    if conn is None:
        raise ValueError("preview needs conn to read the task contract of %s" % task)
    if not db.rows(conn, "tasks", "id=?", (task,)):
        raise _refuse("CONTRACT-TASK-UNKNOWN", "no task %s" % task)
    return (taskcontract.latest(conn).get(task) or {}).get("research")


def _hidden_key(root, case_id):
    """The manifest key of a store case's frozen folder, `hidden/<stage slug>`, read from
    paths.hidden_dir relative to paths.contract_dir so the two spellings cannot drift (any
    valid run id gives the same relative path)."""
    probe = "rr-000"
    return os.path.relpath(paths.hidden_dir(root, probe, case_id),
                           paths.contract_dir(root, probe)).replace(os.sep, "/")


def _manifest(root, pack, block):
    """(files.json manifest, {manifest key: (source, locator)}, reproducers) of what a freeze
    copies: protected files into files/, held-out files into holdout/, store reproducers into
    hidden/<case>/ (CONTRACT-REPRODUCER-MISSING without run.sh)."""
    tree, top = pack["_tree"], pack["repo"]["root"]
    protected, hidden = _rules(block, "paths", "protected"), _rules(block, "paths", "hidden")
    holdout = _rules(block, "holdout", "paths")
    chosen = {}
    for path in sorted(tree):
        if runbook.path_hit(path, hidden):
            continue
        if runbook.path_hit(path, holdout):
            chosen["holdout/" + path] = path
        elif runbook.path_hit(path, protected):
            chosen["files/" + path] = path
    blobs = _blobs(top, [tree[p][1] for p in chosen.values()])
    manifest, sources, reproducers = {}, {}, {}
    for key, path in sorted(chosen.items()):
        mode, oid = tree[path]
        content = blobs[oid]
        manifest[key] = {"sha256": _sha(content), "mode": mode, "size": len(content)}
        sources[key] = ("git", oid)
    if pack.get("pack_source") == "studio":
        # the studio pack, read from disk again (freeze compares this manifest with the preview's): the
        # frozen copy sits at files/@studio/<folder>/<file>, where gates._frozen_runbook_dir looks
        folder = pack["_studio_dir"]
        for rel, (content, mode) in sorted(_studio_files(folder, set(pack["_studio_skip"])).items()):
            key = "files/%s/%s" % (pack["folder"], rel)
            manifest[key] = {"sha256": _sha(content), "mode": mode, "size": len(content)}
            sources[key] = ("studio", os.path.join(folder, rel))
    repair = block.get("repair") if isinstance(block.get("repair"), dict) else {}
    for case in repair.get("cases") or []:
        if (case.get("fail_to_pass") or {}).get("origin") != "store":
            continue
        store = paths.reproducer_store(root, pack["runbook_id"], case["id"])
        found = _store_files(store)
        if "run.sh" not in found:
            raise _refuse("CONTRACT-REPRODUCER-MISSING", "case %s is origin store, and its store folder %s holds "
                          "no run.sh" % (case["id"], os.path.relpath(store, root)))
        folder = _hidden_key(root, case["id"])
        for rel, (content, mode) in sorted(found.items()):
            key = "%s/%s" % (folder, rel)
            manifest[key] = {"sha256": _sha(content), "mode": mode, "size": len(content)}
            sources[key] = ("store", os.path.join(store, rel))
        reproducers[case["id"]] = "contract/" + folder
    return manifest, sources, reproducers


def _task_layers(pack, override, block):
    """The layer map of the merged block: the pack's, with every leaf the task layer sets named
    `task`."""
    out = dict(pack["layers"])
    if isinstance(override, dict):
        for _where, chain, tpath, value in runbook._nodes(override):
            if not chain or not runbook._is_leaf(tpath, value) or chain[-1][0] == "i":
                continue
            if len(chain) > 1 and chain[-2][0] == "i" and chain[-1][1] == chain[-2][1]:
                continue                          # an item's key names the item and sets nothing
            out[runbook._keyed(chain)] = "task"
    known = _leaves(block)[0]
    for field in known:
        out.setdefault(field, "engine")
    return dict((k, v) for k, v in out.items() if k in known)


def preview(root: str, runbook: str, *, task: str | None = None, conn=None) -> dict:
    """Read-only: it reads the runbook, the task contract's `research` part (conn is required
    when task is given, and only read), the target repository with git at the commit load_block
    resolves, and the store folders; it writes no file, no folder and no event. It computes what
    freeze would freeze at this moment: load_block, merge with the task layer (None for no
    override), the files.json manifest of the three frozen folders, files_sha256 over that
    manifest, and the contract object of t-088 section 14.6. Returns {"contract", "contract_sha256",
    "pack_sha256", "files_sha256", "files": the manifest, "base_commit", "override_diff"} and the
    pack-level contract under "pack". Raises the loader's ContractRefusal codes load_block and
    merge raise; it checks no grant and no overlap (built by t-096)."""
    root = os.path.realpath(root)
    rel = os.path.relpath(runbook, root) if os.path.isabs(runbook) else os.path.normpath(runbook)
    rel = rel.replace(os.sep, "/")
    pack = load_block(os.path.join(root, rel))
    anchor = os.path.realpath(pack["project"] or pack["repo"]["root"])
    if anchor != root:
        for part in ("defaults", "spec"):
            if pack[part]:
                full = os.path.join(anchor, pack[part]["path"])
                pack[part]["path"] = os.path.relpath(full, root).replace(os.sep, "/")
    # the runbook path is load_block's realpath-based one, never the caller's spelling: a runbook
    # reached through a folder link keys the same baseline and the same refs as its real path
    rel = os.path.relpath(os.path.join(anchor, pack["runbook"]), root).replace(os.sep, "/")
    pack["runbook"] = rel
    pack["pack_sha256"] = pack_sha256(pack)
    override = _task_override(conn, task) if task is not None else None
    block, diffs = merge(pack, override)
    manifest, _sources, reproducers = _manifest(root, pack, block)
    files_sha256 = _sha(_canon(manifest))
    repo = pack["repo"]
    forbidden = _forbidden(root, repo["root"], _rules(block, "paths", "hidden"), _rules(block, "holdout", "paths"))
    reads = [os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p)) for p in _rules(block, "paths", "read")]
    dirs = _tool_dirs(forbidden)
    tools = [_resolve_tool(t, dirs) for t in (block.get("fingerprint") or {}).get("tools") or []]
    override_diff = [{"field": d.field, "pack": d.pack, "task": d.task, "class": d.field_class,
                      "direction": d.direction, "grant": d.grant} for d in diffs]
    contract = {"format": _FORMAT, "runbook": rel, "runbook_id": pack["runbook_id"],
                "runbook_sha256": pack["runbook_sha256"], "pack": pack["pack"],
                "repo": {"path": repo["path"], "ref": repo["ref"], "commit": repo["commit"]},
                "defaults": pack["defaults"], "spec": pack["spec"], "files": pack["files"],
                "block": block, "layers": _task_layers(pack, override, block),
                "stages": pack["stages"], "knobs": pack["knobs"], "files_sha256": files_sha256,
                "reproducers": reproducers, "read_real": reads, "tools_real": tools,
                "task": task, "ref": ref(rel, task), "override": override, "override_diff": override_diff,
                "grants": [], "pack_sha256": pack["pack_sha256"]}
    if pack.get("pack_source"):
        contract["pack_source"] = pack["pack_source"]      # only a studio pack says so: other hashes do not move
    contract["contract_sha256"] = contract_sha256(contract)
    return {"contract": contract, "contract_sha256": contract["contract_sha256"],
            "pack_sha256": pack["pack_sha256"], "files_sha256": files_sha256, "files": manifest,
            "base_commit": repo["commit"], "override_diff": override_diff, "pack": pack}


def _layers(conn, root: str, runbook: str, task: str | None):
    """(preview view, pack ref, baseline event or None, pack diff, layers) for check_override: the
    task layer's (ref, override_diff) when task is given, then the pack layer's (pack ref, pack
    diff against the newest research-contract baseline). freeze and loosen_recheck share it, so
    the re-check of a loosen hold reads exactly what the next freeze would check."""
    view = preview(root, runbook, task=task, conn=conn)
    pack, contract = view["pack"], view["contract"]
    pack_ref = ref(contract["runbook"], None)
    baseline = _baseline_event(conn, pack["runbook_id"], pack["runbook"])
    pdiff = pack_diff(pack, baseline["data"] if baseline else None)
    layers = ([(contract["ref"], contract["override_diff"])] if task is not None else []) + [(pack_ref, pdiff)]
    return view, pack_ref, baseline, pdiff, layers


def loosen_recheck(conn, root: str, runbook: str, task: str | None = None) -> dict:
    """Read-only: what the next freeze of (runbook, task) would find loosened now, for the start
    verb's moot check of an open loosen hold (in-session-runtime.md section 22, t-306). Returns
    {"refs": {contract ref: {"loosened": [field, ...], "changed": [{field, direction}, ...]}},
    "pack_sha256", "contract_sha256", "files_sha256", "baseline": the baseline event id or None}.
    `loosened` lists every field whose direction is "loosened", granted or not: a grant never
    makes a field moot here. Writes nothing; raises what preview raises."""
    view, _pack_ref, baseline, _pdiff, layers = _layers(conn, os.path.realpath(root), runbook, task)
    refs = {}
    for ref_, diffs in layers:
        values = [_entry_values(d) for d in diffs]
        refs[ref_] = {"loosened": sorted({v[0] for v in values if v[1] == "loosened"}),
                      "changed": [{"field": v[0], "direction": v[1]} for v in values]}
    return {"refs": refs, "pack_sha256": view["pack_sha256"],
            "contract_sha256": view["contract_sha256"], "files_sha256": view["files_sha256"],
            "baseline": baseline["id"] if baseline else None}


def freeze(conn, root: str, run: str, *, runbook: str, task: str | None,
           session: str) -> tuple[Contract, dict]:
    """t-088 section 7.8: take the commit load_block resolved as the base commit; write
    paths.contract_dir(root, run): contract.json (the contract object), files/ (every tracked
    file a protected rule hits), holdout/ (every tracked file a holdout rule hits, which only the
    holdout jail mounts), hidden/<case id>/ for each origin store case copied from
    paths.reproducer_store(root, runbook_id, case_id) (CONTRACT-REPRODUCER-MISSING without
    run.sh), and files.json over the three; freeze the realpaths of the read paths and tools
    (Contract.read_real, Contract.tools_real, check_overlap); record the intent; land the
    pack-level block; return the contract with the run-start fields {contract_sha256,
    pack_sha256, files_sha256, intent, override_diff, runbook_sha256, base_commit, block,
    stamps}. The copies are written read-only (0444, 0555 for an executable), and `stamps` maps
    each path of the frozen copy (`.` for the contract folder, contract.json, files.json and every
    folder and file under files/, holdout/ and hidden/) to [st_ino, st_ctime_ns] taken after the
    last write; load_frozen, read_frozen and drift compare them with the first run-start's.
    Before it writes anything it calls preview(root, runbook, task=task, conn=conn) and
    check_override with both layers in one call, so an ungranted loosening of either layer or
    both raises one refusal that names every entry. It writes contract.json as preview's
    `contract` object and files.json as preview's `files` manifest, then copies the bytes and
    re-hashes each copy against that manifest (a file that changed between the two reads raises
    ResearchRefusal RUN-CONTRACT-CHANGED), so its contract_sha256, pack_sha256 and files_sha256
    are preview's (built by t-096)."""
    root = os.path.realpath(root)
    view, pack_ref, baseline, pdiff, layers = _layers(conn, root, runbook, task)
    pack, contract = view["pack"], view["contract"]
    try:
        check_override(layers, conn=conn)
    except errors.ContractRefusal as exc:
        # the baseline and pack this loosening was measured against: the start verb records
        # them on the loosen hold, and holds.moot_reason lifts that hold only on a re-check
        # of the same baseline (in-session-runtime.md section 22, t-306)
        exc.measured = {"baseline": baseline["id"] if baseline else None,
                        "pack_sha256": view["pack_sha256"]}
        raise
    task_grants = _mark_grants(conn, contract["ref"], contract["override_diff"]) if task is not None else []
    pack_grants = _mark_grants(conn, pack_ref, pdiff)
    contract["grants"] = sorted(set(task_grants + pack_grants))
    check_overlap(root, from_block(contract), run=run)
    folder = paths.contract_dir(root, run)
    if os.path.lexists(folder):
        raise errors.refusal("RUN-CONTRACT-CHANGED", "%s already holds a frozen contract" % folder)
    manifest, sources, _reproducers = _manifest(root, pack, contract["block"])
    if _canon(manifest) != _canon(view["files"]):
        raise errors.refusal("RUN-CONTRACT-CHANGED", "the files to freeze changed after the preview")
    partial = folder + ".partial"
    if os.path.lexists(partial):
        shutil.rmtree(partial)
    blobs = _blobs(pack["repo"]["root"], [loc for kind, loc in sources.values() if kind == "git"])
    try:
        os.makedirs(partial)
        for key, meta in sorted(manifest.items()):
            kind, loc = sources[key]
            content = blobs[loc] if kind == "git" else _read_bytes(loc)
            if _sha(content) != meta["sha256"]:
                raise errors.refusal("RUN-CONTRACT-CHANGED", "%s changed between the preview and the copy" % key)
            target = os.path.join(partial, key)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(content)
            os.chmod(target, 0o555 if meta["mode"] == "100755" else 0o444)
            if _sha(_read_bytes(target)) != meta["sha256"]:
                raise errors.refusal("RUN-CONTRACT-CHANGED", "the copy of %s does not match its manifest" % key)
        text = _canon(contract)
        for name, body in (("files.json", _canon(manifest)), ("contract.json", text)):
            with open(os.path.join(partial, name), "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(os.path.join(partial, name), 0o444)
        os.rename(partial, folder)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    # taken after the rename and the last chmod: any later write, chmod, rename or new hard link
    # of a frozen path moves its ctime, which no same-user writer can set back
    stamps = _stamps(folder)
    pointer = " ".join(["local:%s@%s" % (contract["runbook"], contract["runbook_sha256"])]
                       + ["decision:%s" % d for d in contract["grants"]])
    label = "research contract %s (%s, task %s)" % (contract["runbook_id"], contract["runbook"], task or "-")
    record = intent.record(conn, label, text, _AUTHOR, pointer, author_role=intent.AGENT_ROLE, session=session)
    land(conn, dict(pack, grants=pack_grants), op=None, via="loader", session=session, actor=_AUTHOR)
    fields = {"contract_sha256": contract["contract_sha256"], "pack_sha256": contract["pack_sha256"],
              "files_sha256": contract["files_sha256"], "intent": record["id"],
              "override_diff": contract["override_diff"], "runbook_sha256": contract["runbook_sha256"],
              "base_commit": contract["repo"]["commit"], "block": contract["block"], "stamps": stamps}
    return _filled(contract, folder), fields


# ------------------------------------------------------------------ reading the frozen copy
#: the `now` of a frozen-copy difference whose path is a link or not a regular file or folder
_NOT_PLAIN = "not a plain file or folder"


class _NotPlain(Exception):
    """A path of the frozen copy that is a symbolic link, or not a regular file or real folder."""


def _plain_dir(path):
    """True when `path` is a real folder (not a link), False when it does not exist; _NotPlain
    for anything else."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        raise _NotPlain(path)
    return True


def _plain_read(folder, rel):
    """The bytes of folder/rel read with no link followed: each folder under `folder` on the way
    must be a real folder and the file a regular file (lstat, then open with O_NOFOLLOW and
    fstat). FileNotFoundError when a part is missing, _NotPlain for a link or a special file."""
    parts = rel.split("/")
    here = folder
    for part in parts[:-1]:
        here = os.path.join(here, part)
        if not _plain_dir(here):
            raise FileNotFoundError(here)
    path = os.path.join(here, parts[-1])
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise _NotPlain(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _NotPlain(path)
        raise
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise _NotPlain(path)
        return fh.read()


def _fix_terms(contract, folder):
    """The Contract with each store case's fix terms read from its frozen fix-terms.txt (read with
    no link followed; a link there raises RUN-CONTRACT-CHANGED)."""
    items = []
    for item in contract.repair:
        terms = ()
        where = contract.reproducers.get(item.case_id)
        if where:
            rel = os.path.relpath(where, "contract").replace(os.sep, "/") + "/fix-terms.txt"
            try:
                text = _plain_read(folder, rel).decode("utf-8")
                terms = tuple(line.strip() for line in text.splitlines() if line.strip())
            except _NotPlain:
                raise _changed("the frozen file %s is a link or not a regular file" % rel)
            except (OSError, ValueError):
                terms = ()
        items.append(dataclasses.replace(item, fix_terms=terms))
    return dataclasses.replace(contract, repair=tuple(items))


def _filled(obj, folder):
    frozen = from_block(obj)
    frozen = dataclasses.replace(frozen, sha256=obj.get("contract_sha256", ""),
                                 files_sha256=obj.get("files_sha256", ""))
    return _fix_terms(frozen, folder)


def _run_starts(conn, run):
    """Every research-run-start event of `run`, oldest first."""
    out = []
    for event in _events(conn, events.RUN_START):
        data = event["data"] if isinstance(event["data"], dict) else {}
        if (data.get("run") or event.get("ref")) == run:
            out.append(event)
    return out


def _run_start(conn, run):
    """The first research-run-start event of `run`, the one the run is bound to, or None."""
    starts = _run_starts(conn, run)
    return starts[0] if starts else None


def _changed(detail):
    return errors.refusal("RUN-CONTRACT-CHANGED", detail)


#: the run-start keys that bind a run to its frozen copy; a later run-start of the run may not
#: change any of them
_BINDING = ("intent", "contract_sha256", "files_sha256", "stamps")


def _binding(data):
    """The binding keys of a run-start's data, with the stamps as their hash (for a short event)."""
    out = dict((k, data.get(k)) for k in _BINDING if k != "stamps")
    out["stamps_sha256"] = _sha(_canon(data["stamps"])) if data.get("stamps") is not None else None
    return out


def _freeze_label(obj):
    """The intent label freeze gives the contract object `obj`."""
    return "research contract %s (%s, task %s)" % (obj.get("runbook_id"), obj.get("runbook"), obj.get("task") or "-")


def _is_freeze_record(rec):
    """True when an intent record has the author and the label freeze writes for the contract its
    own bar holds."""
    if not isinstance(rec, dict) or rec.get("author") != _AUTHOR:
        return False
    try:
        obj = json.loads(rec.get("done_when") or "")
    except ValueError:
        return False
    return isinstance(obj, dict) and rec.get("label") == _freeze_label(obj)


def _record_bar(conn, run, text):
    """(the first run-start's data or None, the intent record's bar or None, findings). The run is
    bound to its first run-start event: a later run-start of the run that names another intent,
    contract_sha256, files_sha256 or stamps is a finding. The bar is that of the intent the first
    run-start names, which must be a freeze record (author alpaca-research and freeze's label);
    with no run-start yet, it is the bar of a freeze record whose bar is `text`."""
    starts = _run_starts(conn, run)
    first = (starts[0]["data"] if isinstance(starts[0]["data"], dict) else {}) if starts else None
    found = []
    if starts:
        bound = _binding(first)
        for later in starts[1:]:
            now = _binding(later["data"] if isinstance(later["data"], dict) else {})
            if _canon(now) != _canon(bound):
                found.append(("run-start", bound, now, "a later research-run-start event of %s (event %s) names "
                              "another frozen contract than the first one" % (run, later.get("id"))))
        iid = first.get("intent")
        rec = intent.get(conn, iid) if iid else None
        if rec is not None and not _is_freeze_record(rec):
            found.append(("intent", iid, rec.get("label"), "the intent record %s that the run-start of %s names is "
                          "not one freeze wrote" % (iid, run)))
            rec = None
    else:
        rec = None
        if text is not None:
            for data in intent.all_intents(conn):
                if data.get("done_when") == text and _is_freeze_record(data):
                    rec = data
    return first, (rec.get("done_when") if rec else None), found


def _stamps(folder):
    """{path: [st_ino, st_ctime_ns]} of the frozen copy in `folder` (`.` for the folder itself),
    read with lstat, links never followed."""
    out = {}

    def put(rel, full):
        st = os.lstat(full)
        out[rel] = [st.st_ino, st.st_ctime_ns]

    put(".", folder)
    for name in ("contract.json", "files.json"):
        put(name, os.path.join(folder, name))
    for top in ("files", "holdout", "hidden"):
        full_top = os.path.join(folder, top)
        if not _plain_dir(full_top):
            continue
        put(top, full_top)
        for base, dirs, names in os.walk(full_top, followlinks=False):
            dirs.sort()
            for name in dirs + sorted(names):
                full = os.path.join(base, name)
                put(os.path.relpath(full, folder).replace(os.sep, "/"), full)
    return out


def _is_frozen_path(field):
    return field in ("contract", "contract.json", "files.json") or field.split("/")[0] in ("files", "holdout", "hidden")


def _stamp_findings(folder, want, run, others=()):
    """One finding per stamped path whose inode or ctime moved since freeze, or which is gone; a
    path already named by another finding of the frozen copy (itself, a path under it or a folder
    above it) is left to that finding. `.` is reported as the field `contract`."""
    if not isinstance(want, dict):
        return [("stamps", None, None, "the run-start of %s carries no stamps of its frozen copy" % run)]
    taken = [f for f in others if _is_frozen_path(f)]
    out = []
    for rel in sorted(want):
        field = "contract" if rel == "." else rel
        if any(field == "contract" or t == "contract" or t == field or t.startswith(field + "/")
               or field.startswith(t + "/") for t in taken):
            continue
        try:
            st = os.lstat(folder if rel == "." else os.path.join(folder, rel))
            now = [st.st_ino, st.st_ctime_ns]
        except OSError:
            now = None
        if now != list(want[rel] or []):
            out.append((field, want[rel], now, "the frozen path %s of %s was written, moved or relinked after "
                        "freeze (inode and ctime %s, now %s)" % (field, run, _canon(want[rel]), _canon(now))))
    return out


def _moved(root, run):
    """None when the contract folder of `run` resolves to its own spelling under the real project
    root; else the detail: some folder on the way (.alpaca, .alpaca/research, the run folder, the
    contract folder) is a link."""
    folder = paths.contract_dir(root, run)
    want = os.path.normpath(os.path.join(os.path.realpath(root), os.path.relpath(folder, root)))
    got = os.path.realpath(folder)
    if got == want:
        return None
    return "the frozen contract folder of %s resolves to %s, not %s: a folder on the way is a link" % (run, got, want)


def _frozen_check(conn, root, run):
    """Check the frozen copy of `run` against the record without following any link: the contract
    folder resolves to its own spelling under the real project root (no link at .alpaca or any
    folder below it), the run and contract folders, contract.json byte for byte against the bar
    of the freeze intent record the first run-start names, a later run-start that names another
    contract, the contract hash and the run-start hashes, files.json against files_sha256, every
    file under files/, holdout/ and hidden/ against files.json (a file it does not list, a link
    or a special file included), and the inode and ctime of every frozen path against the first
    run-start's stamps, so an edit undone before this check is still found. Returns (the frozen
    contract object, the manifest, findings): the object is the intent record's bar
    (contract.json only when no record names one), and each finding is (field, frozen, now,
    detail), empty when the copy is intact."""
    folder = paths.contract_dir(root, run)
    out, text, problem = [], None, None
    moved = _moved(root, run)
    try:
        folder_ok = moved is None and all(_plain_dir(where) for where in (paths.run_dir(root, run), folder))
    except _NotPlain as exc:
        folder_ok = False
        problem = ("contract", _NOT_PLAIN, "the frozen contract folder of %s is a link or not a folder: %s" % (run, exc))
    if moved is not None:
        problem = ("contract", _NOT_PLAIN, moved)
    if folder_ok:
        try:
            text = _plain_read(folder, "contract.json").decode("utf-8")
        except _NotPlain as exc:
            problem = ("contract.json", _NOT_PLAIN, "contract.json of %s is a link or not a regular file: %s" % (run, exc))
        except (OSError, ValueError) as exc:
            problem = ("contract.json", None, "the frozen contract of %s cannot be read: %s" % (run, exc))
    elif problem is None:
        problem = ("contract", None, "the frozen contract of %s is missing" % run)
    first, bar, bound = _record_bar(conn, run, text)
    start_data = first or {}
    if problem is not None:
        out.append((problem[0], _sha(bar) if bar else None, problem[1], problem[2]))
    elif bar != text:
        out.append(("contract.json", _sha(bar) if bar else None, _sha(text),
                    "contract.json of %s differs from the intent record's bar" % run))
    out += bound
    obj = None
    for source in (bar, text):
        try:
            obj = json.loads(source) if source is not None else None
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            break
        obj = None
    if obj is None:
        if not out:
            out.append(("contract.json", None, None, "the frozen contract of %s is not an object" % run))
        return None, {}, out
    if contract_sha256(obj) != obj.get("contract_sha256"):
        out.append(("contract_sha256", obj.get("contract_sha256"), contract_sha256(obj),
                    "contract.json of %s does not hash to its contract_sha256" % run))
    for key in ("contract_sha256", "files_sha256"):
        if start_data.get(key) and start_data[key] != obj.get(key):
            out.append(("run-start " + key, start_data[key], obj.get(key),
                        "the run-start event of %s names another %s" % (run, key)))
    if not folder_ok:
        return obj, {}, out                   # nothing under a linked or missing folder is read
    manifest, want = {}, obj.get("files_sha256")
    try:
        listing = _plain_read(folder, "files.json").decode("utf-8")
    except _NotPlain:
        listing = None
        out.append(("files.json", want, _NOT_PLAIN, "files.json of %s is a link or not a regular file" % run))
    except (OSError, ValueError) as exc:
        listing = None
        out.append(("files.json", want, None, "files.json of %s cannot be read: %s" % (run, exc)))
    if listing is not None:
        if _sha(listing) != want:
            out.append(("files.json", want, _sha(listing), "files.json of %s does not hash to files_sha256" % run))
        try:
            manifest = json.loads(listing)
        except ValueError:
            manifest = {}
        manifest = manifest if isinstance(manifest, dict) else {}
    sha_of = lambda key: manifest[key].get("sha256") if isinstance(manifest.get(key), dict) else None  # noqa: E731
    bad, found = [], set()
    for top in ("files", "holdout", "hidden"):
        try:
            if not _plain_dir(os.path.join(folder, top)):
                continue
        except _NotPlain:
            bad.append(top)
            out.append((top, None, _NOT_PLAIN, "%s/ of %s is a link or not a folder" % (top, run)))
            continue
        for base, dirs, names in os.walk(os.path.join(folder, top), followlinks=False):
            for name, want_mode in [(d, stat.S_ISDIR) for d in sorted(dirs)] + [(n, stat.S_ISREG) for n in sorted(names)]:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, folder).replace(os.sep, "/")
                try:
                    mode = os.lstat(full).st_mode
                except OSError:
                    continue                  # gone since the listing; the manifest check sees it
                if want_mode(mode):
                    if want_mode is stat.S_ISREG:
                        found.add(rel)
                    continue
                bad.append(rel)
                out.append((rel, sha_of(rel), _NOT_PLAIN, "%s of %s is a link or not a regular file or folder" % (rel, run)))
            dirs[:] = [d for d in dirs if os.path.relpath(os.path.join(base, d), folder).replace(os.sep, "/") not in bad]
    for rel in sorted(found - set(manifest)):
        try:
            now = _sha(_plain_read(folder, rel))
        except (_NotPlain, OSError):
            now = _NOT_PLAIN
        out.append((rel, None, now, "the frozen folders of %s hold a file the manifest does not list: %s" % (run, rel)))
    for key in sorted(manifest):
        if any(key == b or key.startswith(b + "/") for b in bad):
            continue                          # reported once, at the link
        parts = key.split("/")
        if parts[0] not in ("files", "holdout", "hidden") or len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
            out.append((key, sha_of(key), None, "files.json of %s names a path outside the frozen folders: %s" % (run, key)))
            continue
        try:
            now = _sha(_plain_read(folder, key))
        except _NotPlain:
            out.append((key, sha_of(key), _NOT_PLAIN, "the frozen file %s of %s is a link or not a regular file" % (key, run)))
            continue
        except OSError:
            out.append((key, sha_of(key), None, "the frozen file %s of %s is missing" % (key, run)))
            continue
        if now != sha_of(key):
            out.append((key, sha_of(key), now, "the frozen file %s of %s changed" % (key, run)))
    if first is not None:
        out += _stamp_findings(folder, first.get("stamps"), run, [f[0] for f in out])
    return obj, manifest, out


def load_frozen(conn, root: str, run: str) -> Contract:
    """Compare contract.json with the intent record's bar and re-hash every file of files.json
    (contract/files/, contract/holdout/ and contract/hidden/) against it and against the
    run-start event's files_sha256; raise ResearchRefusal RUN-CONTRACT-CHANGED on any
    difference, a file the manifest does not list included. No link is followed: a link or a
    special file anywhere from the project root down to the frozen files is a difference too, as
    is a later run-start that names another contract, or a frozen path whose inode or ctime moved
    since freeze. The run's reproducers/ folder is not part of it (section 9.1; built by t-096)."""
    obj, _manifest_, findings = _frozen_check(conn, root, run)
    if findings:
        raise _changed("; ".join(f[3] for f in findings))
    return _filled(obj, paths.contract_dir(root, run))


def read_frozen(root: str, run: str) -> Contract:
    """The Contract of paths.contract_dir(root, run)/contract.json, read with no link followed and
    parsed, with no re-hash of the frozen files: load_frozen has checked them at the runner's
    start (section 10.3), and the frozen files are a fingerprint source at every point (section
    9.1). launcher.launch calls it once per launch. It reads the record read-only
    (db.connect_readonly): contract.json must hash to the first run-start's contract_sha256 (with
    no run-start yet, to its own) and equal the bar of the freeze intent record the first
    run-start names (with no run-start yet, a freeze record whose bar it is); no later run-start
    may name another contract; and with a run-start, the inode and ctime of every frozen path
    (an lstat each, no read) must be the run-start's stamps, so a frozen file written after the
    runner start is refused at the next launch. The contract folder must resolve to its own
    spelling under the real project root. Raises ResearchRefusal RUN-CONTRACT-CHANGED when
    contract.json is missing, a link, does not parse or differs from the record, or a stamp
    moved (built by t-096)."""
    folder = paths.contract_dir(root, run)
    moved = _moved(root, run)
    if moved is not None:
        raise _changed(moved)
    try:
        if not all(_plain_dir(where) for where in (paths.run_dir(root, run), folder)):
            raise FileNotFoundError(folder)
        text = _plain_read(folder, "contract.json").decode("utf-8")
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError("not an object")
    except _NotPlain as exc:
        raise _changed("the frozen contract of %s is a link or not a regular file: %s" % (run, exc))
    except (OSError, ValueError) as exc:
        raise _changed("the frozen contract of %s cannot be read: %s" % (run, exc))
    try:
        conn = db.connect_readonly(root)
    except Exception as exc:  # noqa: BLE001 - a record that cannot be read fails closed
        raise _changed("the record of %s cannot be read: %s" % (run, exc))
    try:
        first, bar, found = _record_bar(conn, run, text)
    except Exception as exc:  # noqa: BLE001
        raise _changed("the record of %s cannot be read: %s" % (run, exc))
    finally:
        conn.close()
    if found:
        raise _changed("; ".join(f[3] for f in found))
    start_data = first or {}
    want = start_data.get("contract_sha256") or obj.get("contract_sha256")
    if contract_sha256(obj) != want or obj.get("contract_sha256") != want:
        raise _changed("contract.json of %s does not hash to the contract_sha256 its run started with" % run)
    if bar != text:
        raise _changed("contract.json of %s differs from the intent record's bar" % run)
    if first is not None:
        found = _stamp_findings(folder, first.get("stamps"), run)
        if found:
            raise _changed("; ".join(f[3] for f in found))
    try:
        return _filled(obj, folder)
    except (KeyError, TypeError, ValueError) as exc:
        raise _changed("the frozen contract of %s cannot be read: %s" % (run, exc))


def drift(conn, root: str, run: str) -> list[str]:
    """Differences between the frozen contract and the runbook, defaults file, spec, pack files,
    store reproducers and task override on disk now, and between the frozen copy itself and the
    record (the checks of load_frozen, what "frozen"); each one is recorded once as
    research-contract-drift (code CONTRACT-EDIT-DURING-RUN) and never used (FR-037). The frozen
    contract compared with is the intent record's bar, not contract.json. Returns one line per
    difference found now; when the frozen copy itself changed, it records every difference and
    then raises ResearchRefusal RUN-CONTRACT-CHANGED, since the run can no longer read its
    contract (built by t-096)."""
    root = os.path.realpath(root)
    obj, manifest, frozen_found = _frozen_check(conn, root, run)
    if obj is None:
        _record_drift(conn, run, [("frozen", f[0], f[1], f[2]) for f in frozen_found])
        raise _changed("; ".join(f[3] for f in frozen_found))
    repo_path = (obj.get("repo") or {}).get("path", ".")
    repo = os.path.realpath(repo_path if os.path.isabs(repo_path) else os.path.join(root, repo_path))

    def disk(full):
        try:
            return _sha(_read_bytes(full))
        except OSError:
            return None

    found = [("frozen", field, frozen, now) for field, frozen, now, _detail in frozen_found]
    found.append(("runbook", "runbook", obj.get("runbook_sha256"), disk(os.path.join(root, obj.get("runbook", "")))))
    if obj.get("defaults"):
        found.append(("runbook", "defaults", obj["defaults"].get("sha256"),
                      disk(os.path.join(root, obj["defaults"]["path"]))))
    found.append(("runbook", "spec", (obj.get("spec") or {}).get("sha256"),
                  disk(os.path.join(root, (obj.get("spec") or {}).get("path", "")))))
    for key, meta in sorted(manifest.items()):
        top, _sep, rest = key.partition("/")
        if top in ("files", "holdout") and isinstance(meta, dict):
            found.append(("runbook", "files[%s]" % rest, meta.get("sha256"), disk(os.path.join(repo, rest))))
    for case, where in sorted((obj.get("reproducers") or {}).items()):
        prefix = os.path.relpath(where, "contract").replace(os.sep, "/") + "/"
        store = paths.reproducer_store(root, obj.get("runbook_id"), case)
        now = _store_files(store)
        frozen = dict((k[len(prefix):], m.get("sha256")) for k, m in manifest.items()
                      if k.startswith(prefix) and isinstance(m, dict))
        for rel in sorted(set(frozen) | set(now)):
            found.append(("runbook", "reproducers[%s]/%s" % (case, rel), frozen.get(rel),
                          _sha(now[rel][0]) if rel in now else None))
    if obj.get("task"):
        latest = taskcontract.latest(conn).get(obj["task"]) or {}
        found.append(("override", "research", obj.get("override"), latest.get("research")))
    diffs = [(what, field, frozen, now) for what, field, frozen, now in found
             if what == "frozen" or _canon(frozen) != _canon(now)]
    lines = _record_drift(conn, run, diffs)
    if frozen_found:
        raise _changed("; ".join(f[3] for f in frozen_found))
    return lines


def _record_drift(conn, run, diffs):
    """One research-contract-drift event per (what, field, now) not recorded before for `run`;
    returns one line per difference."""
    if not diffs:
        return []
    start = _run_start(conn, run)
    seen = set()
    for event in _events(conn, events.CONTRACT_DRIFT):
        data = event["data"] if isinstance(event["data"], dict) else {}
        if data.get("run") == run:
            seen.add((data.get("what"), data.get("field"), _canon(data.get("now"))))
    lines = []
    for what, field, frozen, now in diffs:
        lines.append("%s %s: frozen %s, now %s" % (what, field, _canon(frozen), _canon(now)))
        if (what, field, _canon(now)) in seen:
            continue
        seen.add((what, field, _canon(now)))
        events.emit(conn, events.CONTRACT_DRIFT, run=run, ref=run, op=start.get("op") if start else None,
                    session=events.runner_session(run), actor=_AUTHOR, code=vc.FAIL,
                    reason="CONTRACT-EDIT-DURING-RUN",
                    data={"what": what, "field": field, "frozen": frozen, "now": now})
    return lines
