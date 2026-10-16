"""Repair mode: the per-repair-task plan. The prepare stage committed as the case base, the
reproducer (a frozen store copy, or one an agent writes and a second agent reviews) shown failing
on the base live before any fix, the base pass-to-pass measurement, the fix rounds, the result
the verifiers read, and the localization and coupled-file hints of the fix prompts.

The repair task's plan, id the repair task (architecture.md sections 3.9 and 7.4):
  claim, repo, prepare, reproducer, base-measure, rounds, result, seal.

What it keeps to:

- No fix attempt starts before its reproducer is proved (FR-005, EC-018): the `rounds` step runs
  only after a proved `research-reproducer` and an `ok` `research-baseline`, and a fix attempt
  is judged only against a reproducer proved before the attempt task was added
  (fix_problem, called by the attempt's `judge` step), so a reproducer written or changed after
  a fix attempt started and tuned to it never accepts that attempt.
- Proved means: base FAIL shown live by the engine (gates.run_item on a fresh tree at the case
  base, side "base"; every TEST it names FAILs and at least one is named) and, when an agent
  wrote it, a second agent's review approved it with no open condition. A conditional approve
  counts as not approved (gap map H11).
- A changed reproducer needs the same proof (gap map H02, H11): when the frozen copy no longer
  hashes to its newest proof, the `rounds` step calls prove_reproducer again before the next
  fix round. A store reproducer is shown failing on the base live again, and its TEST names may
  only grow. A generated reproducer's proof is a writer's launch, a second agent's review and
  base FAIL live; a change no writer launch of the engine made has no such proof, so it is
  recorded unproved and the task ends reproducer-unproved. gates.fail_to_pass re-hashes the
  copy against the newest proof before every use and refuses a changed one.
- The writer works in its own tree of the repair repository at the case base (no commit after
  the base, FR-014); its draft is copied out by bytes, never through a link, and a link, a
  special file or an oversized draft is refused. No agent jail mounts the frozen reproducer, and
  the byte copies under repair/<task>/ (draft/ and review/draft/) are removed once the proof is
  recorded.
- Early ends (a prepare stage that does not PASS, an unproved reproducer, a base pass set below
  pass_to_pass.min_tests) skip every step up to `result`, which records the outcome; the task
  then moves to blocked with the code as its reason and no seal.

Design: design/research-engine/architecture.md sections 3.9, 4.3, 7.4, 8.2, 8.3 and 10.4;
design/research-engine/runbook-format-3.md sections 4, 11 and 14.5; gap map H01, H02, H11, H13.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat

from alpaca import claims as leases
from alpaca import db
from alpaca.gates import verdict as vc
from alpaca.research import events, paths
from alpaca import _nofollow
from alpaca.research.errors import IsolationError
from alpaca.research.types import Proposal, RunContext

ACTOR = events.ACTOR
PLAN_STEPS = ("claim", "repo", "prepare", "reproducer", "base-measure", "rounds", "result",
              "seal")
#: the folder of the writer's worktree its draft goes in; the engine copies it out by bytes
DRAFT_FOLDER = "alpaca-reproducer"
WRITER, REVIEWER = "reproducer-writer", "reproducer-reviewer"
_DRAFT_MAX_FILES = 200
_DRAFT_MAX_BYTES = 8 * 1024 * 1024
_MAX_TESTS = 50
_PROOF_TRIES = 100               # launch try numbers each proof of a reproducer may use
_LEASE_MINUTES = 60
_FTP = "fail-to-pass:"
_OUTCOME_OF = {"REPAIR-PREPARE-FAILED": "prepare-failed",
               "REPAIR-REPRODUCER-UNPROVED": "reproducer-unproved",
               "REPAIR-BASELINE-THIN": "baseline-thin"}


# ======================================================================== record reads
def _lc():
    from alpaca.research import lifecycle
    return lifecycle


def _events(conn, kind: str, ref: str | None = None) -> list:
    return _lc()._events(conn, kind, ref)


def _newest(conn, kind: str, ref: str) -> dict | None:
    got = _events(conn, kind, ref)
    return got[-1] if got else None


def _research(conn, task: str) -> dict:
    return _lc()._research(conn, task)


def _rel(root: str, path):
    return _lc()._rel(root, path)


def _abs(root: str, path):
    return _lc()._abs(root, path)


def _emit(conn, ctx: RunContext, kind: str, ref: str, *, code=None, reason=None, data=None,
          in_txn=False) -> dict:
    return events.emit(conn, kind, run=ctx.run, ref=ref, op=ctx.op, session=ctx.session,
                       code=code, reason=reason, data=data, in_txn=in_txn)


def _item(ctx: RunContext, conn, repair_task: str):
    """The RepairItem of the repair task: the contract case its task-add `research.case` names."""
    case = _research(conn, repair_task).get("case")
    for item in tuple(ctx.contract.repair or ()):
        if item.case_id == case:
            return item
    raise RuntimeError("%s names no repair case of the contract (%r)" % (repair_task, case))


def repair_dir(root: str, run: str, repair_task: str) -> str:
    """<run_dir>/repair/<repair task>: the writer's and reviewer's packets, the writer's tree,
    the draft, the base tree and the prepare diff (architecture.md section 8.4)."""
    return os.path.join(paths.run_dir(root, run), "repair", paths.check_name(repair_task))


def _clone(ctx: RunContext, repair_task: str):
    from alpaca.research import isolation
    clone = isolation.open_clone(ctx.root, ctx.run, line=repair_task)
    if clone is None:
        raise RuntimeError("the repair repository of %s is missing" % repair_task)
    return clone


def early_end(conn, repair_task: str) -> str | None:
    """The code of a recorded early end of the repair task (a failed prepare stage, an unproved
    reproducer, a thin base set), else None. Each later step reads it and does nothing."""
    for kind, flag, code in ((events.PREPARE, "ok", "REPAIR-PREPARE-FAILED"),
                             (events.REPRODUCER, "proved", "REPAIR-REPRODUCER-UNPROVED"),
                             (events.BASELINE, "ok", "REPAIR-BASELINE-THIN")):
        ev = _newest(conn, kind, repair_task)
        if ev is not None and ev["data"].get(flag) is not True:
            return code
    return None


# ======================================================================== files
def _hash_folder(folder: str) -> list:
    """[{path, sha256}] of every regular file under folder, paths relative to it, sorted."""
    from alpaca.research import gates
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if stat.S_ISREG(os.lstat(full).st_mode):
                out.append({"path": os.path.relpath(full, folder).replace(os.sep, "/"),
                            "sha256": gates._sha256_file(full)})
    out.sort(key=lambda f: f["path"])
    return out


def _matches(root: str, data: dict) -> bool:
    """True when the folder a research-reproducer names still holds exactly the files it hashed."""
    folder = _abs(root, data.get("dir"))
    if not folder or not os.path.isdir(folder) or os.path.islink(folder):
        return False
    want = sorted(((f.get("path"), f.get("sha256")) for f in data.get("files") or []
                   if isinstance(f, dict)), key=lambda p: str(p[0]))
    try:
        have = [(f["path"], f["sha256"]) for f in _hash_folder(folder)]
        odd = [n for dp, dn, fn in os.walk(folder) for n in dn + fn
               if not (stat.S_ISREG(os.lstat(os.path.join(dp, n)).st_mode)
                       or stat.S_ISDIR(os.lstat(os.path.join(dp, n)).st_mode))]
    except OSError:
        return False
    return not odd and have == want


def _remove(path: str) -> None:
    """Remove path (a folder, a file or a link) when it exists; a link is removed, never
    followed."""
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        _nofollow.remove_tree(path)          # no recursion, on Python 3.10 too (op-008 t-571)


def _fresh_dir(path: str) -> None:
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.lexists(path):
        shutil.rmtree(path)
    os.makedirs(path, mode=0o755)


def copy_by_bytes(src: str, dest: str) -> str | None:
    """Copy the folder src into a fresh dest by bytes: regular files and folders only, read with
    O_NOFOLLOW, at most _DRAFT_MAX_FILES files and _DRAFT_MAX_BYTES bytes. Returns None, or the
    reason the copy was refused (dest is then removed)."""
    try:
        st = os.lstat(src)
    except OSError:
        return "the draft folder %s is missing" % DRAFT_FOLDER
    if not stat.S_ISDIR(st.st_mode):
        return "the draft folder %s is not a folder" % DRAFT_FOLDER
    _fresh_dir(dest)
    count, size, problem = 0, 0, None
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, src)
        for name in sorted(dirnames):
            full = os.path.join(dirpath, name)
            if not stat.S_ISDIR(os.lstat(full).st_mode):
                problem = "the draft holds a link or special file: %s" % os.path.join(rel_dir, name)
                break
            os.makedirs(os.path.join(dest, rel_dir, name), exist_ok=True)
        if problem:
            break
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            info = os.lstat(full)
            if not stat.S_ISREG(info.st_mode):
                problem = "the draft holds a link or special file: %s" % rel
                break
            count += 1
            size += info.st_size
            if count > _DRAFT_MAX_FILES or size > _DRAFT_MAX_BYTES:
                problem = "the draft is over %d files or %d bytes" % (_DRAFT_MAX_FILES,
                                                                     _DRAFT_MAX_BYTES)
                break
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(_DRAFT_MAX_BYTES + 1)
            out = os.path.join(dest, rel)
            with open(out, "wb") as fh:
                fh.write(data)
            os.chmod(out, 0o755 if info.st_mode & 0o111 else 0o644)
        if problem:
            break
    if problem:
        shutil.rmtree(dest, ignore_errors=True)
        return problem
    if count == 0:
        shutil.rmtree(dest, ignore_errors=True)
        return "the draft folder %s holds no file" % DRAFT_FOLDER
    return None


def bug_report_text(ctx: RunContext, item) -> str | None:
    """The case's bug report from the frozen contract copy (contract/files/), never from the live
    checkout; None when the frozen copy is missing."""
    base = os.path.join(paths.contract_dir(ctx.root, ctx.run), "files")
    from alpaca.research import gates
    try:
        folder = gates._studio_folder(ctx.contract) or os.path.dirname(str(ctx.contract.runbook or ""))
    except ValueError:
        return None
    for cand in (os.path.join(base, folder, item.bug_report), os.path.join(base, item.bug_report)):
        if os.path.isfile(cand) and not os.path.islink(cand):
            with open(cand, encoding="utf-8", errors="replace") as fh:
                return fh.read()
    return None


# ======================================================================== hints
def _proposal_fields(proposal) -> dict:
    if proposal is None:
        return {}
    if isinstance(proposal, Proposal):
        return dict(proposal.fields or {})
    if isinstance(proposal, dict):
        return dict(proposal.get("fields") or proposal)
    return {}


def _paths_of(value) -> list:
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, (list, tuple)) else []


def hints(ctx: RunContext, repair_task: str, *, proposal: Proposal | None = None) -> dict:
    """No record write. For the proposer of a fix attempt (proposal None): the case's
    suspect_files, the gate chain and every research.coupled group that holds a suspect file.
    For its implementer: the same plus every group that holds a file of the proposal's
    `suspects` or `files` (runbook-format-3.md section 14.5), and the proposal's `evidence` chain
    as untrusted text. A group holds a path when runbook.path_admitted admits the path with the
    group's files read as rules. FR-005's localization hints are the suspect files and that
    chain."""
    from alpaca import runbook
    from alpaca.research import gates
    conn = db.connect_readonly(ctx.root)
    try:
        item = _item(ctx, conn, repair_task)
    finally:
        conn.close()
    fields = _proposal_fields(proposal)
    suspects = list(item.suspect_files or ())
    proposed = _paths_of(fields.get("suspects")) + _paths_of(fields.get("files"))
    candidates = suspects + [p for p in proposed if p not in suspects]
    groups = []
    for group in tuple(ctx.contract.coupled or ()):
        rules = tuple(group.files or ())
        hit = [p for p in candidates if runbook.path_admitted(p, rules)]
        if hit:
            groups.append({"id": group.id, "files": list(rules), "note": group.note,
                           "matched": hit})
    evidence = fields.get("evidence") if isinstance(fields.get("evidence"), str) else None
    return {"case": item.case_id, "suspect_files": suspects,
            "chain": list(gates.chain(ctx.contract, repair_case=item.case_id)),
            "coupled": groups, "proposal_suspects": _paths_of(fields.get("suspects")),
            "proposal_files": _paths_of(fields.get("files")), "evidence": evidence}


def hints_text(got: dict) -> str:
    """The hints as the plain text a prompt carries between the untrusted markers."""
    lines = ["Suspect files (where the bug report points):"]
    lines += ["- %s" % p for p in got.get("suspect_files") or []] or ["- (none named)"]
    lines.append("The gate chain a fix must pass, in order:")
    lines += ["- %s" % i for i in got.get("chain") or []]
    if got.get("proposal_suspects"):
        lines.append("The proposal's suspects:")
        lines += ["- %s" % p for p in got["proposal_suspects"]]
    if got.get("coupled"):
        lines.append("Coupled file groups (a change to one file of a group changes the others):")
        for g in got["coupled"]:
            lines.append("- %s: %s%s" % (g["id"], ", ".join(g["files"]),
                                         ". %s" % g["note"] if g.get("note") else ""))
    if got.get("evidence"):
        lines.append("The proposal's evidence chain, from the symptom back to the owning file:")
        lines.append(str(got["evidence"]))
    return "\n".join(lines)


# ======================================================================== trees
def _tree(ctx: RunContext, clone, path: str, commit: str, *, admin: bool = False):
    """A fresh tree of clone at commit under path, after removing a leftover of a killed runner
    at that path (destroy_worktree checks the folders are the engine's own)."""
    from alpaca.research import isolation
    old = isolation.tree_info(path, commit=commit, label=os.path.basename(path), admin=admin)
    if any(p and os.path.lexists(p) for p in (old.path, old.host_git, old.admin_dir)):
        isolation.destroy_worktree(clone, old)
    return isolation.fresh_tree(ctx.root, clone, commit=commit, path=path, admin=admin)


def case_base(conn, ctx: RunContext, repair_task: str) -> str:
    """The commit every tree of the repair task is cut at: the `head` of its research-prepare
    event when the case has a prepare stage, else its clone's root commit
    (isolation.open_clone(...).base_commit, which build_repair_repo made from RepairItem.base,
    else the run's base commit). A RepairItem.base is a commit of the target repository and is
    not in the repair clone, so no step cuts a tree at it."""
    ev = _newest(conn, events.PREPARE, repair_task)
    if ev is not None and ev["data"].get("ok") and ev["data"].get("head"):
        return ev["data"]["head"]
    return _clone(ctx, repair_task).base_commit


# ======================================================================== prepare
def _reset_branch(clone) -> None:
    """A research branch moved past its root commit with no research-prepare recorded (a runner
    killed between the prepare commit and its event) goes back to the root commit, so the
    stage runs again on a fresh tree and its commit is made once more."""
    from alpaca.research import isolation
    env = isolation.git_env(clone)
    isolation._git(["update-ref", "refs/heads/%s" % clone.branch, clone.base_commit, clone.head],
                   env=env)


def prepare(conn, ctx: RunContext, repair_task: str) -> dict:
    """Cuts isolation.fresh_tree(root, <the repair task's clone>, commit=<the clone's root
    commit>, path=repair/<repair task>/prepare), runs the stage contract.repair_prepare on it
    through gates.run_prepare (an offline gate jail that overlays the frozen copies of
    contract/files/ on the tree, `${WORKTREE}` that tree), then, on PASS,
    isolation.commit_prepared(parent=<the root commit>, out_dir=repair/<repair task>/) and
    destroys the tree. Records one research-prepare event: {repair_task, case, stage,
    gate_event, parent, head, diff ({path, sha256, bytes, empty}), ok} plus the commit's tree,
    paths, stamp and message, so a lost repository is rebuilt with the same head; a stage that
    does not PASS records ok false with code REPAIR-PREPARE-FAILED and commits nothing. With no
    prepare stage it records nothing. A recorded prepare is returned as it is."""
    from alpaca.research import gates, isolation
    stage = ctx.contract.repair_prepare
    if not stage:
        return {"skipped": True}
    done = _newest(conn, events.PREPARE, repair_task)
    if done is not None:
        return done["data"]
    item = _item(ctx, conn, repair_task)
    clone = _clone(ctx, repair_task)
    if clone.head != clone.base_commit:
        _reset_branch(clone)
        clone = _clone(ctx, repair_task)
    parent = clone.base_commit
    folder = repair_dir(ctx.root, ctx.run, repair_task)
    os.makedirs(folder, exist_ok=True)
    wt = _tree(ctx, clone, os.path.join(folder, "prepare"), parent)
    data = {"repair_task": repair_task, "case": item.case_id, "stage": stage, "gate_event": None,
            "parent": parent, "head": None, "diff": None, "ok": False, "tree": None,
            "paths": [], "stamp": None, "message": None, "reason": None}
    try:
        result = gates.run_prepare(conn, ctx.root, run=ctx.run, task=repair_task,
                                   contract=ctx.contract, wt=wt, session=ctx.session)
        data["gate_event"] = result.event_id
        if result.verdict == vc.PASS:
            stamp = paths.stamp()
            message = "research prepare %s of %s" % (repair_task, ctx.run)
            try:
                cap = isolation.commit_prepared(clone, wt, parent=parent, message=message,
                                                stamp=stamp, out_dir=folder)
                data.update(ok=True, head=cap.head, tree=cap.tree, paths=list(cap.paths),
                            stamp=cap.stamp, message=cap.message,
                            diff={"path": _rel(ctx.root, cap.path), "sha256": cap.sha256,
                                  "bytes": cap.bytes, "empty": bool(cap.empty)})
            except IsolationError as exc:
                data["reason"] = "the prepared tree could not be committed: %s: %s" % (
                    exc.reason, exc.detail)
        else:
            data["reason"] = "the prepare stage %s ended %s" % (stage, vc.name_of(result.verdict))
    finally:
        isolation.destroy_worktree(clone, wt)
    ok = bool(data["ok"])
    _emit(conn, ctx, events.PREPARE, repair_task, code=vc.PASS if ok else vc.FAIL,
          reason=None if ok else "REPAIR-PREPARE-FAILED", data=data)
    return data


# ======================================================================== the reproducer
def _role_spec(a, conn, role: str, try_n: int, **kw) -> dict:
    """The attempt step's LaunchSpec mapping for one of the two reproducer roles, with the
    role's own timeout (t-088's engine default unless the contract sets one) and the tools of
    its archetype in the research-repair formation."""
    from alpaca.runbook import RESEARCH_DEFAULTS
    from alpaca.research.types import AGENT_ROLE_KEYS
    spec = a.spec(conn, role, try_n, **kw)
    key = {label: k for k, label in AGENT_ROLE_KEYS.items()}[role]
    timeout = RESEARCH_DEFAULTS["runtime"]["timeouts"][key]
    for agent in tuple(a.contract.agents or ()):
        if agent.role == role and agent.timeout_s:
            timeout = int(agent.timeout_s)
    spec["timeout_s"] = int(timeout)
    try:
        from alpaca.research import verifier
        spec["tools"] = tuple(verifier.role_tools(a.root, "research-repair", role))
    except Exception:  # noqa: BLE001 - no tools: the issue refuses the job (design 26)
        pass
    return spec


def _final_json(result: dict) -> dict | None:
    text = str((result or {}).get("final_message") or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else ""
    try:
        got = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    return got if isinstance(got, dict) else None


def _test_names(value) -> tuple | None:
    """The writer's TEST names: 1 to _MAX_TESTS distinct non-empty strings with no whitespace
    and no colon (the markers grammar's names), else None."""
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_TESTS:
        return None
    out = []
    for name in value:
        if not isinstance(name, str) or not name or len(name) > 200 \
                or any(c.isspace() for c in name) or ":" in name or name in out:
            return None
        out.append(name)
    return tuple(out)


class _Proof:
    """The state of one proof of a repair task's reproducer."""

    def __init__(self, ctx: RunContext, repair_task: str, conn):
        self.ctx = ctx
        self.task = repair_task
        self.item = _item(ctx, conn, repair_task)
        self.folder = repair_dir(ctx.root, ctx.run, repair_task)
        self.data = {"repair_task": repair_task, "case": self.item.case_id,
                     "origin": self.item.fail_to_pass.origin, "proved": False,
                     "base_fail_event": None, "live_event": None, "review_event": None,
                     "writer_event": None, "dir": None, "files": [], "tests": [],
                     "base_commit": None, "reason": None, "supersedes": None}

    def attempt(self):
        return _lc()._Attempt(self.ctx, self.task)

    def first_try(self, conn) -> int:
        """The first try number of this proof's launches: each earlier research-reproducer of
        the task moves it by _PROOF_TRIES, so a second proof (a changed reproducer) launches its
        agents anew instead of replaying the first proof's recorded ends, while a resumed proof
        (no event of its own yet) finds its own launches again."""
        return 1 + _PROOF_TRIES * len(_events(conn, events.REPRODUCER, self.task))

    def run_role(self, conn, role: str, build):
        """lifecycle._Attempt.run_agent for the role: ("ok", result), ("paused", reason) on a
        budget hold or a stop, ("failed", reason) when every try failed."""
        a = self.attempt()
        how, result = a.run_agent(conn, role, build, first_try=self.first_try(conn))
        if how == "held":
            _lc()._budget_hold(conn, self.ctx, result.get("reason"))
            return "paused", "a %s launch was held by the budget check" % role
        if how == "stopped":
            return "paused", "the run is stopping"
        if how == "spent":
            return "failed", "every %s launch failed (class %s)" % (role, result)
        if result.get("outcome") != "ok":
            return "failed", "the %s ended %s" % (role, result.get("outcome"))
        return "ok", result


def _packet(proof: _Proof, conn, bug: str) -> str:
    packet = os.path.join(proof.folder, "packet")
    _fresh_dir(packet)
    with open(os.path.join(packet, "bug_report.md"), "w", encoding="utf-8") as fh:
        fh.write(bug)
    with open(os.path.join(packet, "hints.md"), "w", encoding="utf-8") as fh:
        fh.write(hints_text(hints(proof.ctx, proof.task)) + "\n")
    return packet


#: a writer tree's folder name: `writer` for try 1, `writer-t<n>` for try n > 1
_WRITER_TREE = re.compile(r"^writer(?:-t[0-9]+)?$")


def _writer_path(proof: _Proof, try_n: int) -> str:
    """repair/<task>/writer for try 1 and repair/<task>/writer-t<n> after: each writer try has its
    own tree, never given to another try (in-session-runtime.md section 28.1)."""
    name = "writer" if int(try_n) == 1 else "writer-t%d" % int(try_n)
    return os.path.join(proof.folder, name)


def _drop_writer_trees(proof: _Proof, clone) -> None:
    """Remove every writer tree under the repair folder (a leftover of a killed runner, or the
    trees this step cut) with their host and admin folders, quietly: a dead try that is still
    writing leaves its tree to the next start instead of failing the step."""
    from alpaca.research import isolation
    try:
        names = sorted(os.listdir(proof.folder))
    except OSError:
        return
    trees = set()
    for name in names:
        for suffix in (".git-host", ".git-admin"):       # a killed create may leave only these
            if name.endswith(suffix):
                name = name[:-len(suffix)]
        if _WRITER_TREE.fullmatch(name):
            trees.add(name)
    for name in sorted(trees):
        info = isolation.tree_info(os.path.join(proof.folder, name), commit="", label=name,
                                   admin=True)
        isolation.remove_quiet([info.path, info.host_git, info.admin_dir])


def _write_draft(proof: _Proof, conn, clone, base_commit: str, bug: str):
    """The writer's launch, each try in its own tree of the repair base (`writer`, then
    `writer-t<n>`; in-session-runtime.md section 28.1); the draft of the try whose result came
    back copied by bytes into repair/<task>/draft/, so nothing a dead try wrote, before it died or
    by a call still in flight, is frozen with the proof. Returns ("ok", tests), ("paused",
    reason) or ("failed", reason)."""
    from alpaca.research import prompting
    ctx = proof.ctx
    packet = _packet(proof, conn, bug)
    _drop_writer_trees(proof, clone)
    trees = {}
    try:
        a = proof.attempt()

        def build(try_n):
            wt = _tree(ctx, clone, _writer_path(proof, try_n), base_commit, admin=True)
            trees["last"] = trees[try_n] = wt
            text = prompting.render(WRITER, contract=ctx.contract,
                                    context={"run": ctx.run, "case": proof.item.case_id,
                                             "cwd": wt.path, "draft": DRAFT_FOLDER,
                                             "packet": packet, "attempt": proof.task},
                                    untrusted={"bug_report": bug,
                                               "hints": hints_text(hints(ctx, proof.task))})
            return _role_spec(a, conn, WRITER, try_n, cwd=wt.path, workdir=wt.path, prompt=text,
                              write_paths=(wt.path,), read_paths=(packet,),
                              git_admin=wt.admin_dir)
        how, result = proof.run_role(conn, WRITER, build)
        if how != "ok":
            return how, result
        proof.data["writer_event"] = result.get("launch_end")
        answer = _final_json(result)
        tests = _test_names((answer or {}).get("tests"))
        if tests is None:
            return "failed", ("the writer's final message names no valid TEST list: one JSON "
                              "object with `tests`, 1 to %d names" % _MAX_TESTS)
        # the tree of the try whose result came back: the last one build cut
        problem = copy_by_bytes(os.path.join(trees["last"].path, DRAFT_FOLDER),
                                os.path.join(proof.folder, "draft"))
        if problem:
            return "failed", problem
        return "ok", tests
    finally:
        _drop_writer_trees(proof, clone)


def _base_fail(proof: _Proof, conn, clone, base_commit: str, rep_dir: str, names: tuple):
    """Base FAIL shown live: the case's fail-to-pass command once on a fresh tree at the case
    base with rep_dir as ${REPRODUCER_DIR}. (proved, reason, GateResult)."""
    from alpaca.research import gates, isolation
    ctx = proof.ctx
    base = _tree(ctx, clone, os.path.join(proof.folder, "base"), base_commit)
    try:
        g = gates.run_item(conn, ctx.root, run=ctx.run, task=proof.task, contract=ctx.contract,
                           item=_FTP + proof.item.case_id, wt=base, side="base",
                           session=ctx.session, reproducer_dir=rep_dir)
    finally:
        isolation.destroy_worktree(clone, base)
    proof.data["base_fail_event"] = g.event_id
    live = g.event_id
    if g.reused:
        row = conn.execute("SELECT data FROM events WHERE id=?", (g.event_id,)).fetchone()
        live = json.loads(row["data"] or "{}").get("reused_from") if row else None
    proof.data["live_event"] = live
    if g.verdict != vc.PASS:
        return False, "the base run gave no valid TEST report (%s)" % vc.name_of(g.verdict), g
    if not names:
        return False, "the reproducer names no TEST", g
    report = dict(g.tests.tests) if g.tests else {}
    not_failing = ["%s (%s)" % (n, report.get(n, "absent")) for n in names
                   if report.get(n) != "FAIL"]
    if not_failing:
        return False, "not FAIL on the case base: %s" % ", ".join(not_failing), g
    return True, None, g


def _review(proof: _Proof, conn, draft: str, gate, names: tuple, bug: str):
    """The second agent's review of a generated draft: its packet holds the bug report, a copy of
    the draft, the base FAIL log and the TEST names. Approved only on {"verdict": "approve"}
    and `conditions` present as an empty list: an approve with conditions, with no `conditions`
    key or with any other shape counts as not approved (gap map H11). ("ok", None), ("paused",
    reason) or ("failed", reason)."""
    from alpaca.research import prompting
    ctx = proof.ctx
    review = os.path.join(proof.folder, "review")
    _fresh_dir(review)
    problem = copy_by_bytes(draft, os.path.join(review, "draft"))
    if problem:
        return "failed", problem
    with open(os.path.join(review, "bug_report.md"), "w", encoding="utf-8") as fh:
        fh.write(bug)
    log = gate.log if gate is not None else None
    with open(os.path.join(review, "base-fail.log"), "wb") as out:
        if log and os.path.isfile(log) and not os.path.islink(log):
            with open(log, "rb") as fh:
                out.write(fh.read(_DRAFT_MAX_BYTES))
    with open(os.path.join(review, "tests.txt"), "w", encoding="utf-8") as fh:
        fh.write("".join("%s\n" % n for n in names))
    text = prompting.render(REVIEWER, contract=ctx.contract,
                            context={"run": ctx.run, "case": proof.item.case_id, "cwd": review,
                                     "tests": "\n".join("- %s" % n for n in names),
                                     "attempt": proof.task},
                            untrusted={"bug_report": bug})
    a = proof.attempt()

    def build(try_n):
        return _role_spec(a, conn, REVIEWER, try_n, cwd=review, workdir=review, prompt=text)
    how, result = proof.run_role(conn, REVIEWER, build)
    if how != "ok":
        return how, result
    proof.data["review_event"] = result.get("launch_end")
    answer = _final_json(result) or {}
    if answer.get("verdict") != "approve":
        return "failed", "the reviewer did not approve: %s" % (
            str(answer.get("reason") or answer.get("verdict") or "no verdict")[:500])
    if not isinstance(answer.get("conditions"), list):
        return "failed", ("the reviewer's approve has no `conditions` list, which counts as not "
                          "approved")
    if answer["conditions"]:
        return "failed", "the reviewer approved with conditions, which counts as not approved"
    return "ok", None


def prove_reproducer(conn, ctx: RunContext, repair_task: str) -> dict:
    """Cuts the repair base tree with isolation.fresh_tree(root, <the repair task's clone>,
    commit=case_base(conn, ctx, repair_task), path=repair/<repair task>/base) (the gate jail
    overlays the frozen copies of contract/files/ on it). Origin store: the reproducer is the
    frozen contract/hidden/<case id>/ copy and its TEST names are the case's `tests`. Origin
    generate: it is the writer's draft, which the engine copies by bytes, never through a link,
    out of the writer's worktree (repair/<repair task>/writer, cut with fresh_tree(...,
    admin=True) at the same base before the writer's launch and destroyed after the copy) into
    repair/<repair task>/draft/; the writer names its TEST names in its final message. Either
    way it shows base FAIL live with gates.run_item(item="fail-to-pass:<case id>", wt=<the base
    tree>, side="base", reproducer_dir=<that folder>): every TEST it names must FAIL, and at
    least one must be named. For origin generate the reviewer's packet then carries the draft
    and that run's log, and, when the review approves, the draft is copied to
    paths.reproducer_dir(root, run, case id). Records research-reproducer with each frozen
    file's sha256, the TEST names and the run_item event as base_fail_event. A base run that
    does not FAIL, or a review that does not approve, records it with `proved` false.

    A recorded proof whose folder still hashes to it is returned as it is, and so is a recorded
    failure. A proved reproducer whose frozen copy changed gets a new event that names the old
    as `supersedes`: a store reproducer is shown failing on the base live again and its names
    may only grow; a generated one is recorded unproved, since its proof is a writer's launch,
    a review and base FAIL live (gap map H02, H11) and no writer launch of the engine made the
    change. The byte copies under repair/<repair task>/ (draft/ and review/draft/) are removed
    once the event is recorded. Returns the event data, or {"paused": True, "reason": ...} when
    a launch was held or the run is stopping (no event)."""
    newest = _newest(conn, events.REPRODUCER, repair_task)
    previous = None
    if newest is not None:
        d = newest["data"]
        if d.get("proved") is not True or _matches(ctx.root, d):
            return d
        previous = newest
    proof = _Proof(ctx, repair_task, conn)
    item = proof.item
    clone = _clone(ctx, repair_task)
    ctx.clone = clone
    base_commit = case_base(conn, ctx, repair_task)
    proof.data["base_commit"] = base_commit
    origin = item.fail_to_pass.origin
    os.makedirs(proof.folder, exist_ok=True)
    reason, gate = None, None
    if previous is not None:
        proof.data["supersedes"] = previous["id"]
    if origin == "store":
        rep_dir = paths.hidden_dir(ctx.root, ctx.run, item.case_id)
        names = tuple(item.fail_to_pass.tests or ())
        proof.data.update(dir=_rel(ctx.root, rep_dir), tests=list(names))
        if not os.path.isdir(rep_dir) or os.path.islink(rep_dir):
            reason = "the frozen store reproducer of case %s is missing" % item.case_id
        else:
            proof.data["files"] = _hash_folder(rep_dir)
            ok, reason, gate = _base_fail(proof, conn, clone, base_commit, rep_dir, names)
            if ok:
                proof.data["proved"] = True
    else:
        bug = bug_report_text(ctx, item)
        draft = os.path.join(proof.folder, "draft")
        names = ()
        if bug is None:
            reason = "the frozen copy of the bug report %s is missing" % item.bug_report
        elif previous is None:
            how, got = _write_draft(proof, conn, clone, base_commit, bug)
            if how == "paused":
                return {"paused": True, "reason": got}
            if how == "failed":
                reason = got
            else:
                names = got
        else:
            # the frozen copy changed after its proof and no writer launch of the engine wrote
            # the change: it lacks the writer step of the same proof (gap map H02), and a review
            # of the changed bytes alone would not see the fixes tried since the first proof
            names = tuple(previous["data"].get("tests") or ())
            proof.data["dir"] = previous["data"].get("dir")
            reason = ("the generated reproducer changed after its proof (event %d) and no "
                      "writer launch of the engine made the change; a generated reproducer "
                      "counts only with a writer's launch, a review and base FAIL shown live"
                      % previous["id"])
        want = tuple(item.fail_to_pass.tests or ())
        if previous is not None:
            want = tuple(dict.fromkeys(want + tuple(previous["data"].get("tests") or ())))
        if reason is None and not set(want) <= set(names):
            reason = "the TEST names may only grow: %s are missing" % ", ".join(
                sorted(set(want) - set(names)))
        proof.data["tests"] = list(names)
        if reason is None:
            proof.data.update(dir=_rel(ctx.root, draft), files=_hash_folder(draft))
            ok, reason, gate = _base_fail(proof, conn, clone, base_commit, draft, names)
            if ok:
                how, got = _review(proof, conn, draft, gate, names, bug)
                if how == "paused":
                    return {"paused": True, "reason": got}
                if how == "failed":
                    reason = got
                else:
                    frozen = paths.reproducer_dir(ctx.root, ctx.run, item.case_id)
                    problem = copy_by_bytes(draft, frozen)
                    if problem:
                        reason = problem
                    else:
                        proof.data.update(proved=True, dir=_rel(ctx.root, frozen),
                                          files=_hash_folder(frozen))
    proved = bool(proof.data["proved"])
    proof.data["reason"] = None if proved else (reason or "the reproducer was not proved")
    _emit(conn, ctx, events.REPRODUCER, repair_task, code=vc.PASS if proved else vc.FAIL,
          reason=None if proved else "REPAIR-REPRODUCER-UNPROVED", data=proof.data)
    for left in (os.path.join(proof.folder, "draft"),
                 os.path.join(proof.folder, "review", "draft")):
        _remove(left)
    return proof.data


def fix_problem(conn, ctx: RunContext, attempt: str) -> str | None:
    """For the `judge` step of a fix attempt: None when the repair task's newest
    research-reproducer is proved and was recorded before the attempt task was added (so the
    reproducer existed, proved, before any fix of this attempt; FR-005, EC-018), else the reason
    the fix is refused."""
    line = _research(conn, attempt).get("repair_task")
    if not line:
        return "%s is not a fix attempt of a repair task" % attempt
    rep = _newest(conn, events.REPRODUCER, line)
    if rep is None:
        return "no reproducing test: the repair task %s has no research-reproducer" % line
    if rep["data"].get("proved") is not True:
        return "the reproducing test of %s is not proved" % line
    added = conn.execute("SELECT id FROM events WHERE kind='task-add' AND ref=? ORDER BY id "
                         "LIMIT 1", (attempt,)).fetchone()
    if added is None or rep["id"] > added["id"]:
        return ("the reproducing test was proved (event %d) after the fix attempt %s started: a "
                "test written after the fix is never judged against it" % (rep["id"], attempt))
    return None


# ======================================================================== base measurement
def base_measure(conn, ctx: RunContext, repair_task: str) -> dict:
    """Cuts its own base tree with isolation.fresh_tree(root, <the repair task's clone>,
    commit=case_base(conn, ctx, repair_task), path=repair/<repair task>/base), after destroying
    any tree a crash left at that path; runs gates.pass_to_pass with that tree on both sides and
    baseline=(), then destroys the tree. Its PassToPass.base_pass is the repair task's base pass
    set (its `baseline` field is empty). Records one research-baseline event (ref the repair
    task) holding types.to_dict(PassToPass), `ok` (base_pass has at least
    contract.pass_to_pass.min_tests tests), `min_tests` and `left_out`, the tests that ran on the
    base and did not pass there (recorded and left out, EC-063). When `ok` is false the code is
    REPAIR-BASELINE-THIN. A recorded measurement is returned as it is."""
    from alpaca.research import gates, isolation
    from alpaca.research.types import to_dict
    done = _newest(conn, events.BASELINE, repair_task)
    if done is not None:
        return done["data"]
    clone = _clone(ctx, repair_task)
    commit = case_base(conn, ctx, repair_task)
    folder = repair_dir(ctx.root, ctx.run, repair_task)
    os.makedirs(folder, exist_ok=True)
    tree = _tree(ctx, clone, os.path.join(folder, "base"), commit)
    try:
        p2p = gates.pass_to_pass(conn, ctx.root, run=ctx.run, task=repair_task,
                                 contract=ctx.contract, base=tree, attempt=tree, baseline=(),
                                 session=ctx.session)
    finally:
        isolation.destroy_worktree(clone, tree)
    spec = ctx.contract.pass_to_pass
    min_tests = int(spec.min_tests) if spec is not None else 0
    left_out = []
    if p2p.base_event is not None:
        row = conn.execute("SELECT data FROM events WHERE id=?", (p2p.base_event,)).fetchone()
        tests = json.loads(row["data"] or "{}").get("tests") if row else None
        if isinstance(tests, dict):
            left_out = sorted(n for n, r in tests.items() if r != "PASS")
    ok = spec is not None and len(set(p2p.base_pass)) >= min_tests and bool(p2p.base_pass)
    data = {"repair_task": repair_task, "ok": ok, "min_tests": min_tests,
            "pass_to_pass": to_dict(p2p), "left_out": left_out, "base_commit": commit}
    _emit(conn, ctx, events.BASELINE, repair_task, code=vc.PASS if ok else vc.FAIL,
          reason=None if ok else "REPAIR-BASELINE-THIN", data=data)
    return data


# ======================================================================== result
def _merged(conn, ctx: RunContext, repair_task: str) -> dict | None:
    for ev in reversed(_events(conn, events.MERGE, ctx.run)):
        if ev["data"].get("repair_task") == repair_task:
            return ev
    return None


def record_result(conn, ctx: RunContext, repair_task: str) -> dict:
    """The `result` step: one research-repair-result event (section 8.3), the record the
    verifiers read for the repair task (section 11.2): outcome `fixed` with forked_from =
    case_base, head = the verified fix's head (the research branch head after its merge), diff =
    the fix attempt's research-diff {path, sha256, bytes, empty}, fix_attempt, and the ids of the
    research-prepare, research-reproducer, research-baseline and the fix's research-verify-end;
    or outcome `prepare-failed`, `reproducer-unproved` or `baseline-thin` with the code, no head
    and no diff; or `not-fixed` when the fix rounds ended with no verified fix (a control run's
    no-op rounds). A recorded result is returned as it is."""
    done = _newest(conn, events.REPAIR_RESULT, repair_task)
    if done is not None:
        return done["data"]
    item = _item(ctx, conn, repair_task)
    prep = _newest(conn, events.PREPARE, repair_task)
    rep = _newest(conn, events.REPRODUCER, repair_task)
    base = _newest(conn, events.BASELINE, repair_task)
    clone_path = _rel(ctx.root, paths.clone_dir(ctx.root, ctx.run, repair_task))
    data = {"repair_task": repair_task, "case": item.case_id, "outcome": None,
            "forked_from": None, "head": None, "diff": None, "fix_attempt": None,
            "fix_verify_event": None, "prepare_event": prep["id"] if prep else None,
            "reproducer_event": rep["id"] if rep else None,
            "baseline_event": base["id"] if base else None, "clone": clone_path}
    code = early_end(conn, repair_task)
    if code is None:
        data["forked_from"] = case_base(conn, ctx, repair_task)
        merged = _merged(conn, ctx, repair_task)
        if merged is not None:
            fix = merged["data"].get("attempt")
            diffs = _events(conn, events.DIFF, fix)
            d = diffs[-1]["data"] if diffs else {}
            ends = _events(conn, events.VERIFY_END, fix)
            data.update(outcome="fixed", head=merged["data"].get("commit"), fix_attempt=fix,
                        diff={"path": d.get("path"), "sha256": d.get("sha256"),
                              "bytes": d.get("bytes") or 0, "empty": bool(d.get("empty"))},
                        fix_verify_event=ends[-1]["id"] if ends else None)
        else:
            data["outcome"] = "not-fixed"
    else:
        data["outcome"] = _OUTCOME_OF[code]
    fixed = data["outcome"] == "fixed"
    _emit(conn, ctx, events.REPAIR_RESULT, repair_task, code=vc.PASS if fixed else vc.FAIL,
          reason=code, data=data)
    return data


# ======================================================================== the plan
class _Plan:
    """The steps of one repair task's plan; each takes the controller's connection."""

    def __init__(self, ctx: RunContext, task: str):
        self.ctx = ctx
        self.task = task

    def claim(self, conn) -> int:
        got = leases.take(conn, self.task, self.ctx.run, _LEASE_MINUTES,
                          session=self.ctx.session, reason="research repair task")
        if got.get("status") != leases.CLAIMED:
            raise RuntimeError("%s is held by %s" % (self.task, got.get("worker")))
        return vc.PASS

    # ------------------------------------------------------------ repo
    def expected_head(self, conn, base: str) -> str:
        merged = _merged(conn, self.ctx, self.task)
        if merged is not None:
            return merged["data"].get("commit")
        prep = _newest(conn, events.PREPARE, self.task)
        if prep is not None and prep["data"].get("ok") and prep["data"].get("head"):
            return prep["data"]["head"]
        return base

    def repo(self, conn) -> int:
        """isolation.build_repair_repo: one root commit of the case's base tree (FR-014); on
        resume a missing or differing repository is rebuilt (deterministic, the run's clone
        stamp) and the recorded prepare diff and fix merges are replayed with merge_champion,
        never running the stage again; a rebuild is recorded as research-infra class lost."""
        from alpaca.research import isolation
        from alpaca.research.types import DiffCapture
        ctx = self.ctx
        item = _item(ctx, conn, self.task)
        start = _lc()._events(conn, events.RUN_START, ctx.run)
        sdata = start[0]["data"] if start else {}
        clone, why = None, None
        try:
            clone = isolation.open_clone(ctx.root, ctx.run, line=self.task)
        except IsolationError as exc:
            why = "%s: %s" % (exc.reason, exc.detail)
        later = any(_events(conn, kind, self.task) for kind in
                    (events.PREPARE, events.REPRODUCER, events.BASELINE))
        if clone is not None and clone.head == self.expected_head(conn, clone.base_commit):
            ctx.clone = clone
            self.contract(conn, clone)
            return vc.PASS
        if clone is not None and not later and _merged(conn, ctx, self.task) is None:
            ctx.clone = clone          # a moved branch with nothing recorded: prepare resets it
            self.contract(conn, clone)
            return vc.PASS
        dest = paths.clone_dir(ctx.root, ctx.run, self.task)
        if os.path.islink(dest):
            os.unlink(dest)
        elif os.path.lexists(dest):
            shutil.rmtree(dest, ignore_errors=True)
        clone = isolation.build_repair_repo(ctx.root, ctx.run, ctx.contract, repair_task=self.task,
                                            base_commit=item.base or ctx.contract.repo_commit,
                                            fix_terms=tuple(item.fix_terms or ()),
                                            stamp=sdata.get("clone_stamp") or paths.stamp())
        replayed = 0
        parent = clone.base_commit
        prep = _newest(conn, events.PREPARE, self.task)
        if prep is not None and prep["data"].get("ok") and prep["data"].get("diff"):
            d = prep["data"]
            diff = d["diff"]
            cap = DiffCapture(forked_from=d.get("parent"), head=d.get("head"),
                              tree=d.get("tree") or "", path=_abs(ctx.root, diff.get("path")),
                              sha256=diff.get("sha256"), bytes=int(diff.get("bytes") or 0),
                              paths=tuple(d.get("paths") or ()), stamp=d.get("stamp") or "",
                              message=d.get("message") or "", empty=bool(diff.get("empty")))
            if not cap.empty:
                parent = isolation.merge_champion(clone, branch=clone.branch, parent=parent,
                                                  capture=cap, message=cap.message,
                                                  stamp=cap.stamp)
                replayed += 1
        merged = _merged(conn, ctx, self.task)
        if merged is not None:
            att = _lc()._Attempt(ctx, merged["data"].get("attempt"))
            diffs = att.diffs(conn)
            if len(diffs) == 1:
                cap = att.capture_of(diffs[0])
                isolation.merge_champion(clone, branch=clone.branch, parent=parent, capture=cap,
                                         message=cap.message, stamp=cap.stamp)
                replayed += 1
        clone = _clone(ctx, self.task)
        ctx.clone = clone
        if later or why or replayed:
            _emit(conn, ctx, events.INFRA, self.task, code=vc.BLOCKED,
                  data={"attempt": None, "role": None, "try": None, "class": "lost",
                        "detail": "the repair repository of %s was rebuilt from its base and %d "
                                  "recorded commit(s): %s" % (self.task, replayed,
                                                              why or "missing or moved"),
                        "wait_until": None})
        self.contract(conn, clone)
        return vc.PASS

    def contract(self, conn, clone) -> None:
        """The repair task's task-contract (architecture.md section 8.2), once."""
        from alpaca import taskcontract
        from alpaca.research import verify_gate
        if self.task in taskcontract.latest(conn):
            return
        item = _item(self.ctx, conn, self.task)
        taskcontract.record(conn, self.task, {
            "input": ["the bug report %s" % item.bug_report, "the case id %s" % item.case_id,
                      "the repair clone's root commit %s" % clone.base_commit,
                      "the frozen contract %s" % self.ctx.contract.sha256],
            "expected": ["research-prepare (when the case has a prepare stage), "
                         "research-reproducer, research-baseline, the verified fix merged into "
                         "%s, research-repair-result" % clone.branch],
            "done_bar": ["the reproducer fails on the case base and passes with the fix",
                         "the fix keeps every base pass-to-pass test",
                         "the fix's diff applies to the case base",
                         "the fix attempt was accepted by the repair rule and verified",
                         verify_gate.SELF_LINES[1]],
            "fail_cases": ["a fix judged against a reproducer that was not shown failing on the "
                           "base (FR-005)"]}, session=self.ctx.session, actor=ACTOR)

    def ensure_clone(self) -> None:
        if self.ctx.clone is None or self.ctx.clone.line != self.task:
            self.ctx.clone = _clone(self.ctx, self.task)

    # ------------------------------------------------------------ the early steps
    def prepare(self, conn) -> int:
        self.ensure_clone()
        prepare(conn, self.ctx, self.task)
        return vc.PASS

    def reproducer(self, conn) -> int:
        if early_end(conn, self.task):
            return vc.PASS
        self.ensure_clone()
        got = prove_reproducer(conn, self.ctx, self.task)
        return vc.PAUSED if got.get("paused") else vc.PASS

    def base_measure(self, conn) -> int:
        if early_end(conn, self.task):
            return vc.PASS
        self.ensure_clone()
        base_measure(conn, self.ctx, self.task)
        return vc.PASS

    # ------------------------------------------------------------ rounds
    def stopping(self, conn) -> bool:
        if self.ctx.stop is not None and self.ctx.stop.is_set():
            return True
        runners = _events(conn, events.RUNNER, self.ctx.run)
        since = runners[-1]["id"] if runners else 0
        return any(ev["id"] > since for ev in _events(conn, events.STOP, self.ctx.run))

    def next_round(self, conn) -> int:
        done = {ev["data"].get("round") for ev in _events(conn, events.ROUND, self.ctx.run)
                if ev["data"].get("repair_task") == self.task}
        r = 1
        while r in done:
            r += 1
        return r

    def rounds(self, conn) -> int:
        """The fix rounds: from the first fix round with no research-round of this task,
        controller.run of lifecycle.round_plan(ctx, r, repair_task=<task>), until a fix is
        accepted, verified and merged into research/<run>/<task>, or the task's bound (N, or the
        rounds_to of an owner extension of a hold this task's own `rounds` step opened:
        runner.rounds_bound with repair_task, section 4.3), where the run's extension hold opens
        with RUN-ROUNDS-REACHED and the step returns PAUSED. A control run gives each repair task
        contract.noop_rounds fix rounds and opens no hold. Before each round a changed
        reproducer is proved again (prove_reproducer); one that fails its new proof ends the
        rounds (`result` records it)."""
        from alpaca.formation import controller
        from alpaca.posture import loops
        from alpaca.research import holds, runner
        ctx = self.ctx
        if early_end(conn, self.task):
            return vc.PASS
        self.ctx.clone = _clone(ctx, self.task)
        control = _lc()._control(conn, ctx)
        r = self.next_round(conn)
        while True:
            if _merged(conn, ctx, self.task) is not None:
                return vc.PASS
            if self.stopping(conn):
                return vc.PAUSED
            if any(h["gate"] == "budget" for h in holds.open_holds(conn, ctx.run)):
                return vc.PAUSED
            if control and r > int(ctx.contract.noop_rounds):
                return vc.PASS
            bound = runner.rounds_bound(conn, ctx.run, ctx.contract, repair_task=self.task)
            if not control and r > bound:
                holds.open_hold(conn, ctx.root, ref=ctx.run, gate="extension",
                                code="RUN-ROUNDS-REACHED",
                                detail="%s reached %d fix rounds with no fix accepted and "
                                       "verified" % (self.task, bound),
                                op=ctx.op, bound=loops.count(conn, ctx.run, "research-extension"),
                                session=ctx.session)
                return vc.PAUSED
            got = prove_reproducer(conn, ctx, self.task)
            if got.get("paused"):
                return vc.PAUSED
            if got.get("proved") is not True:
                return vc.PASS
            code = controller.run(conn, _lc().round_plan(ctx, r, repair_task=self.task),
                                  root=ctx.root, session=ctx.session,
                                  tmp_prefixes=ctx.tmp_prefixes)
            self.ctx.clone = _clone(ctx, self.task)
            if code != vc.PASS:
                return code
            r += 1

    # ------------------------------------------------------------ result and seal
    def result(self, conn) -> int:
        record_result(conn, self.ctx, self.task)
        return vc.PASS

    def seal(self, conn) -> int:
        """outcome fixed: proofs.seal (the close plan, queued by the runner, verifies and closes
        it); any other outcome: the task moves to blocked with its code as the reason, with no
        seal and no verification (section 7.4)."""
        from alpaca.research import proofs
        res = _newest(conn, events.REPAIR_RESULT, self.task)
        d = res["data"] if res else {}
        if d.get("outcome") == "fixed":
            proofs.seal(conn, self.ctx.root, self.task, session=self.ctx.session)
            return vc.PASS
        reason = d.get("code") or "no fix was accepted and verified"
        return proofs.close(conn, self.ctx.root, self.task, session=self.ctx.session,
                            to="blocked", reason=reason)


def repair_plan(ctx: RunContext, repair_task: str) -> dict:
    """The per-repair-task plan, id the repair task (section 7.4): claim, repo, prepare (the
    case's prepare stage, committed as the case base; skipped with no event when
    contract.repair_prepare is None), reproducer (origin store: the frozen copy; origin generate:
    written and reviewed), base FAIL shown live, base-measure (the base pass-to-pass measurement,
    recorded as one research-baseline event), then the `rounds` step: for r from the first fix
    round with no research-round event whose repair_task is this task, controller.run(conn,
    lifecycle.round_plan(ctx, r, repair_task=repair_task), session=ctx.session, root=ctx.root,
    tmp_prefixes=ctx.tmp_prefixes), until a fix is accepted and verified (PASS) or r passes the
    task's bound (the extension hold of section 4.3, PAUSED); then `result` (one
    research-repair-result event) and `seal`. A prepare stage that does not PASS
    (REPAIR-PREPARE-FAILED), a reproducer that is not proved (REPAIR-REPRODUCER-UNPROVED) or a
    thin base set (REPAIR-BASELINE-THIN: fewer than contract.pass_to_pass.min_tests passing on
    the base) skips every later step up to `result`, which records that outcome, and the task
    moves to blocked with the code as its reason, with no seal and no verification, like a failed
    round 0 baseline (section 7.4). Its steps receive conn from the controller, like every plan
    step."""
    p = _Plan(ctx, repair_task)
    steps = {"claim": p.claim, "repo": p.repo, "prepare": p.prepare, "reproducer": p.reproducer,
             "base-measure": p.base_measure, "rounds": p.rounds, "result": p.result,
             "seal": p.seal}
    return {"id": repair_task, "steps": [{"name": n, "run": steps[n]} for n in PLAN_STEPS]}
