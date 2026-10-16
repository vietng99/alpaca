"""The ordered gate chain in an offline gate jail: gate stages, metrics, fail-to-pass and
pass-to-pass, judged on by-bytes copies of the evidence, and the gate re-runs a verifier asks for.

Design: design/research-engine/architecture.md sections 3.3, 4.7, 5.1, 5.2, 8.3, 8.4 and 13.2
(FR-022 to FR-027, FR-031, FR-057; EC-008, EC-039, EC-054, EC-055, EC-062), and
design/research-engine/runbook-format-3.md sections 2.2, 14.4 and 16.4.

What this module keeps to:

- Every command (a stage command, a plugin check, a metric repeat, a fail-to-pass or
  pass-to-pass run) runs through procs.run inside a bubblewrap jail of kind "gate", which runs in
  its own empty network namespace, so it has no network (jail.argv puts --unshare-net on every
  kind). The jail prints a start line before it runs the command; a log that does not start with
  it means the jail did not start, and the item is BLOCKED.
  Nothing ever runs outside a jail. A research-gate event records `offline: true` when every
  command of the item ran in a jail whose argv held --unshare-net (read from the argv built for
  that jail, not assumed), and `offline: null` with `ran: false` when no command ran
  (architecture.md says "always true"; null marks that no command ran, and the design text is
  to say "true for every item that ran; null with ran false otherwise").
- Before every gate jail, a contract with read paths runs contract.check_read_paths; a finding
  (a socket, FIFO or device, or a writable folder under a read path) makes the item BLOCKED
  (class gate-blocked) with "read path refused: <findings>" as its reason, and so does a check
  that cannot run (fail closed). A tree that holds a socket, FIFO or device is BLOCKED the same
  way, with "tree refused: <paths>": the jail has no network, but a socket file in the tree it
  binds could still reach a host listener. A tree folder the host cannot list, or an entry it
  cannot stat, is refused too, since the jail could chmod it open and reach a socket there.
- TEST and METRIC reports are read by markers.py only (strict marker lines; see its docstring),
  and a report of more than markers._MAX_RESULTS names, or an evidence folder of more than
  _MAX_EVIDENCE entries, _MAX_EVIDENCE_NAMES bytes of entry names or _MAX_EVIDENCE_BYTES bytes of
  content, is refused, so every event stays bounded: an evidence map holds at most
  _MAX_EVIDENCE_NAMES bytes of names and 64 hex digits per entry, once per run and once more
  at the top of a stage event.
- Every command gets a fresh random token on file descriptor 3 (ALPACA_RESULTS_FD=3), read from
  a pipe the host filled and closed before the jail started, so once the runner has read it no
  other process can. A TEST, METRIC or EVIDENCE line counts only when it ends with the token
  (`TEST: <name> ... PASS <token>`), and a block counts only when it ends with
  `ALPACA-RESULTS-END <token>` (markers._with_token). A marker line inside the block without the
  token, a bare END, or an END with another token is GATE-MARKERS (markers-conflict). So code
  that the frozen runner runs in a child process can neither print a block of its own nor add a
  counted line to the runner's block, even when it shares the runner's stdout or stderr: a line
  it prints for a test the runner does not report, or a METRIC line it prints before it fails,
  has no token and makes the item a conflict. Code that runs inside the runner's own process
  can read the token from its memory: a runner must run tested code in a child process
  (markers.py says the same). A child cannot read its parent's memory only while the host's
  Yama ptrace_scope is 1 or higher, so every run row and every event records the value read
  from /proc/sys/kernel/yama/ptrace_scope when its jail ran (`ptrace_scope`, null when the file
  is missing): an event whose value is 0 or null had no such guard.
- No process in the jail holds the log. procs.run opens the log as the stdout of _RELAY, a host
  process outside the jail's pid namespace, which starts bwrap with its stdout and stderr on one
  end of a socketpair and copies the other end into the log. Tested code therefore cannot reopen
  the log through /proc (a socket cannot be opened by path: ENXIO), pwrite or truncate it, or
  read the runner's lines before the host has them, so it cannot rewrite a METRIC value or TEST
  result the runner printed. It can still print lines of its own into the stream; none of
  them counts, since it has no token (above), and one that looks like a marker line makes the
  item a conflict. A pipe would not close this: a jailed process can fchmod its end
  of a pipe and open it again for reading through /proc. Because stdout is a socket, a runner
  writes to stderr with >&2, never by reopening /dev/stdout or /dev/stderr (ENXIO).
- A command's log is capped at _MAX_LOG_BYTES: the relay kills the jail once the log passes the
  cap (it can pass it by the one chunk of at most 64 KiB that crossed it, and the relay discards
  everything after that chunk), and a log over the cap
  is its own verdict (BLOCKED for a stage, a plugin check or a
  recovery; GATE-MARKERS, markers-conflict, for a metric repeat or a test run). The host reads
  at most _LOG_HEAD bytes of a log for the jail's start line, at most _MAX_LOG_BYTES to parse
  markers, and the last _LOG_TAIL bytes of a plugin's output, so a flood of output never fills
  the host's memory.
- The host never reads a live evidence folder. After each jail exits, judged_copy copies it by
  bytes into judged/<tag>-<repeat>/ and every check and metric reads that copy. The copy is
  taken after the jail exits, so a process that tested code left in the jail can still rewrite
  a file between the runner's write and the jail's end. A json-field metric under
  ${EVIDENCE_DIR} therefore counts only when the runner's token-closed block attests the file
  (`EVIDENCE: <path relative to the evidence folder> <sha256 of the bytes it wrote> <token>`):
  no such line is GATE-MARKERS (markers-missing), and a judged file with another sha256 is
  GATE-MARKERS (markers-conflict). A json-field path in the frozen runbook folder needs no
  attestation, since no jail can write it. A stage's file and json checks on the evidence folder
  (runbook.evaluate) have no such guard: they trust the stage's runner and whatever it runs. A
  write error while copying (a full disk, a quota) refuses the copy with GATE-EVIDENCE-LINK, so
  the item is recorded as BLOCKED instead of raising out of the chain.
- Frozen files are overlaid read-only on their paths in the tree, and every folder between the
  tree root and an overlaid file is bound onto itself, so it is a mount point that the command
  cannot rename or remove (EBUSY); the jail's own root is remounted read-only after its mounts,
  so no parent folder of a bound host path (the contract files, the reproducer, the evidence
  folder) can be renamed either. Code in the tree therefore cannot move the frozen runbook
  folder aside and put its own scripts at the frozen paths.
- The first non-PASS item stops the chain and is named in ChainResult.first_failure.
- Each item records one research-gate event per side with its command, return code, log, input
  fingerprint, offline flag and lock wait. A result is reused only when a recorded research-gate
  of the same run, task, stage and side carries the same input_sha256 (EC-008) and the run it
  records left its tree as it found it: the event holds the tree digest taken before the run
  (tree_sha256) and after it (tree_after_sha256), and only an event whose two are equal is
  reused. A stage that writes into its tree (a build, the repair prepare step) is therefore run
  again on a fresh or rebuilt tree instead of being answered with a PASS that left no output
  there. A verifier's re-run (side "verify") and the repair prepare step (side "prepare"), whose
  product is its tree, are never reused. The input fingerprint covers the whole tree (its top
  .git included), the contract, the frozen contract files (and the reproducer of a fail-to-pass
  item), the tool files, the content of every read path and the item id. The tree digest
  writes each folder and entry as one JSON line with its permission bits and its extended
  attributes, so neither a file name that spells another tree's lines, nor a chmod, nor a
  changed xattr is answered with a reused result. An entry the digest cannot read (a folder it
  cannot list, a file or xattr it cannot read) mixes a fresh random value into the digest, so
  that item is never reused.
- The host's digests are bounded, since they run outside every timeout (the digest after a
  run comes before its event is written). A digest reads at most _MAX_DIGEST_BYTES bytes of
  file content and walks at most _MAX_DIGEST_ENTRIES entries. A file whose size (st_size, read
  before any byte, so a sparse file of a petabyte costs one stat) would pass the byte bound is
  not read, and a walk past the entry bound stops; either one mixes a fresh random value into
  the digest, so the item still records its event at once and is never reused. Only the check
  that the frozen contract files did not change while an item ran (_frozen_state) digests
  without the bounds, since a random value there would read as a change; no jail can write
  that folder.
- Nothing the tested code leaves behind raises out of run_chain, so every item records its
  event: a folder above a frozen file that the host cannot search refuses the next jail (the
  item is BLOCKED), an evidence folder nested deeper than _MAX_EVIDENCE_DEPTH folders is refused
  with GATE-EVIDENCE-LINK, a home or evidence folder is removed without recursion (os.fwalk, so
  neither depth nor path length stops it), and a home that still cannot be removed is recorded
  on its run's row ("cleanup") without changing the run's result. A json-field metric file that
  cannot be decoded (bad JSON, nesting too deep for the decoder, an integer too large for a
  float) makes that repeat not ok with its reason, and a marker line too long to parse is a
  markers-conflict (markers._MAX_MARKER_LINE).
- Each run of an item on a side takes its own run number (the tag <side>-<k>, claimed by an
  atomic mkdir of <stage folder>/runs/<side>-<k>), and its logs, judged copies and homes are
  named <side>-<k>-<repeat>, so no run overwrites another's log or judged copy, and a reused
  event keeps the log and log_sha256 of the event it reuses. A verifier's re-run works in the
  gates/ folder of its own try folder (the parent of the tree it runs on), never in the
  attempt's.
- A heavy stage holds locks.heavy(stage) for its whole run (FR-026); its fingerprint and the
  reuse lookup are taken after the locks are held.
- alpaca/runbook.py stays unchanged (section 13.2): runbook.evaluate judges exit-code, file and
  json checks, runbook.respond decides a stage's next step, and plugin checks run here in the
  gate jail with the environment keys of runbook._run_plugin plus ALPACA_WORKTREE and
  ALPACA_CONTRACT_DIR, and its verdict mapping. A json-field metric reads its field with
  runbook._field, a private helper of alpaca/runbook.py, so that helper's name and behavior are
  a dependency of this module.
"""
from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import math
import os
import secrets
import stat
import sys

from alpaca import runbook
from alpaca.gates import contract as band
from alpaca.gates import monotonicity
from alpaca.gates import verdict as vc
from alpaca.research import contract as rcontract
from alpaca.research import events, jail, locks, markers, paths, procs
from alpaca import _nofollow
from alpaca.research.errors import GateInfraError, LockTimeout, MarkerError
from alpaca.research.types import (ChainResult, Contract, FailToPass, GateResult, JailSpec,
                                   MetricSample, MetricValue, PassToPass, TestReport,
                                   WorktreeInfo)

_FTP = "fail-to-pass:"
_PTP = "pass-to-pass"
_METRIC = "metric:"
_SIDES = ("attempt", "base", "verify", "prepare")
_JAIL_MARK = "ALPACA-GATE-JAIL-STARTED"
_EVIDENCE_VAR = "${EVIDENCE_DIR}"
_REUSABLE = ("PASS", "FAIL")
_MAX_TRIES = 64            # a stage's attempts and recoveries; the runbook's retry rule ends first
_PROBE_OUT = 300           # characters of a jail's own error text kept as the reason
_MAX_EVIDENCE = 10000      # entries judged_copy copies before it refuses the folder
_MAX_EVIDENCE_NAMES = 1 << 20    # bytes of relative entry names judged_copy lists
_MAX_EVIDENCE_BYTES = 256 << 20  # bytes of file content judged_copy copies
_MAX_EVIDENCE_DEPTH = 128  # folder levels judged_copy descends (one frame and one fd each)
_MAX_DIGEST_BYTES = 1 << 30      # bytes of file content one digest reads; past it, a gap
_MAX_DIGEST_ENTRIES = 1 << 20    # entries one digest walks; past it, a gap and the walk stops
_NEVER_REUSED = ("verify", "prepare", "holdout")   # sides whose recorded results are never reused
_PTRACE_SCOPE = "/proc/sys/kernel/yama/ptrace_scope"
_TREE_KEYS = ("tree_sha256", "tree_after_sha256")   # the tree's digests before and after a run
_MAX_LOG_BYTES = 32 << 20  # a command's log; the relay kills the jail past it
_LOG_HEAD = 4096           # bytes read for the jail's start line
_LOG_TAIL = 65536          # bytes of a plugin's output read for its last line
_RESULTS_FD = 3            # the descriptor that carries a run's results token
_SHOWN = 5                 # special files named in a refused tree's reason
_PYTHON = sys.executable or "python3"   # runs _RELAY on the host

# The log relay every gate jail runs under, a host process outside the jail: argv is the cap in
# bytes, then the bwrap command. bwrap's stdout and stderr are one end of a socketpair, and the
# relay copies the other end into its own stdout, the log procs.run opened. A process in the jail
# holds only the socket: opening /proc/self/fd/1 of a socket fails (ENXIO), pwrite and ftruncate
# on it fail, and the relay and its log are outside the jail's pid namespace, so nothing the jail
# runs can reach a byte the relay already wrote. A pipe would not do: a jailed process can
# fchmod its end and open the pipe again for reading through /proc, and so take the runner's
# lines before the host reads them. Once the log passes the cap, the relay writes the chunk that
# crossed it, SIGKILLs bwrap (its sandbox dies with it: --die-with-parent, and the jail's pid
# namespace ends with its first process), and discards the rest until the socket closes. A write
# error on the log stops the copy and never the drain. The relay exits with bwrap's return code,
# or dies of bwrap's signal.
_RELAY = r"""
import os, signal, socket, subprocess, sys
cap = int(sys.argv[1])
here, there = socket.socketpair()
child = subprocess.Popen(sys.argv[2:], stdout=there, stderr=there, close_fds=True)
there.close()
total, over, sink = 0, False, True
while True:
    try:
        data = here.recv(65536)
    except OSError:
        break
    if not data:
        break
    if over or not sink:
        continue
    view = memoryview(data)
    try:
        while view:
            view = view[os.write(1, view):]
    except OSError:
        sink = False
    total += len(data)
    if total > cap:
        over = True
        child.kill()
here.close()
rc = child.wait()
if rc < 0:
    try:
        signal.signal(-rc, signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    os.kill(os.getpid(), -rc)
sys.exit(rc)
"""


# ------------------------------------------------------------------------------ the chain
def chain(contract: Contract, *, repair_case: str | None = None) -> tuple[str, ...]:
    """The chain item ids in run order, t-088's order (runbook-format-3.md section 16.1): each
    gate stage by its stage id (contract.gates), then "metric:<id>" for each metric (improve),
    then "fail-to-pass:<case id>" and "pass-to-pass" (repair, for repair_case). These ids are the
    `stage` of every research-gate event and of a verifier's re-run request. Pure.

    Raises ValueError for a repair_case the contract does not declare."""
    items = [g.stage for g in contract.gates]
    if contract.mode == "improve":
        items += [_METRIC + m.id for m in contract.metrics]
    if repair_case is not None:
        case = _case(contract, repair_case)
        items += [_FTP + case.case_id, _PTP]
    return tuple(items)


def run_chain(conn, root: str, *, run: str, task: str, contract: Contract, wt: WorktreeInfo,
              mode: str, session: str, side: str = "attempt", stages: tuple | None = None,
              repair_case: str | None = None, base: WorktreeInfo | None = None,
              baseline: PassToPass | None = None) -> ChainResult:
    """Run the items of chain(contract, repair_case=repair_case) in order (or only `stages`); for
    a repair attempt the fail-to-pass and pass-to-pass items run through fail_to_pass and
    pass_to_pass below, whose verdicts they take. `base` (the repair task's base tree) and
    `baseline` (its base pass-to-pass measurement) are required when `repair_case` is set and
    unused otherwise; `baseline.base_pass` is the set pass_to_pass keeps.

    Each item runs under its own timeout in an offline gate jail; the first non-PASS stops the
    chain and is named; a heavy stage holds locks.heavy(stage) for its duration; each item
    records research-gate events (architecture.md section 3.3)."""
    return _run_chain(conn, root, run=run, task=task, contract=contract, wt=wt, mode=mode,
                      session=session, side=side, stages=stages, repair_case=repair_case,
                      base=base, baseline=baseline)


def _run_chain(conn, root, *, run, task, contract, wt, mode, session, side, stages,
               repair_case, base, baseline, timeout_cap=None, gates_root=None) -> ChainResult:
    if mode not in ("improve", "repair"):
        raise ValueError("mode is improve or repair, not %r" % (mode,))
    if side not in _SIDES:
        raise ValueError("side is one of %s, not %r" % (", ".join(_SIDES), side))
    full = chain(contract, repair_case=repair_case)
    if stages is None:
        todo = list(full)
    else:
        unknown = [s for s in stages if s not in full]
        if unknown:
            raise ValueError("not items of the chain: %s" % ", ".join(map(str, unknown)))
        todo = [item for item in full if item in stages]
    if repair_case is not None and any(i.startswith(_FTP) or i == _PTP for i in todo):
        if base is None or baseline is None:
            raise ValueError("a repair chain needs the base tree and the base measurement")
    cx = _Cx(conn, root, run, task, contract, session, gates_root=gates_root)
    results = []
    measured = {}
    ftp = ptp = None
    final, first = vc.PASS, None
    for item in todo:
        nn = full.index(item) + 1
        if item.startswith(_METRIC):
            metric = _metric(contract, item[len(_METRIC):])
            value, result = _run_metric(cx, metric=metric, nn=nn, wt=wt, side=side,
                                        timeout_cap=timeout_cap)
            measured[metric.id] = value
            results.append(result)
            code = result.verdict
        elif item.startswith(_FTP):
            ftp, got, code = _fail_to_pass(cx, case=item[len(_FTP):], base=base, attempt=wt,
                                           nn=nn)
            results.extend(got)
        elif item == _PTP:
            ptp, got, code = _pass_to_pass(cx, base=base, attempt=wt,
                                           baseline=tuple(baseline.base_pass), nn=nn)
            results.extend(got)
        else:
            result = _run_stage(cx, item=item, nn=nn, wt=wt, side=side, timeout_cap=timeout_cap)
            results.append(result)
            code = result.verdict
        if cx.stopped:
            # the runner's stop signal killed this item: the chain ends with no verdict
            final, first = vc.BLOCKED, item
            break
        if code != vc.PASS:
            final, first = code, item
            break
    return ChainResult(verdict=final, results=tuple(results), first_failure=first,
                       metrics=measured, fail_to_pass=ftp, pass_to_pass=ptp)


def input_fingerprint(*, tree: str, contract_sha256: str, files_sha256: str, tools_sha256: str,
                      stage: str) -> str:
    """The sha256 of the five inputs of one gate item: the tree's content digest, the contract
    hash, the frozen files' digest (with the reproducer's for a fail-to-pass item and the content
    of every read path the gate jail binds), the tool files' digest and the item id. Pure; two
    different values of any part give two different fingerprints, so a changed input is never
    answered with a recorded result (EC-008)."""
    body = json.dumps({"v": 1, "tree": tree, "contract": contract_sha256, "files": files_sha256,
                       "tools": tools_sha256, "stage": stage}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------- judged copies
def judged_copy(src: str, dest: str) -> dict:
    """Copy the evidence folder src, which a gate or holdout jail wrote, by bytes into dest, a new
    engine-only folder (mode 0700) that no jail mounts, and return {relative path: sha256}. Called
    only after the jail has exited, so nothing in it still writes. It walks src from a descriptor:
    each entry is opened relative to its parent's descriptor with O_NOFOLLOW (folders with
    O_DIRECTORY | O_NOFOLLOW) and checked with fstat after the open, so no path component is ever
    followed through a link and no symlink is ever descended. It raises GateInfraError with reason
    GATE-EVIDENCE-LINK, naming the relative path and "symlink", "special file" or "outside", for a
    symlink at any depth, a FIFO, socket or device, or an entry whose realpath is not inside src,
    "unreadable" or "name not UTF-8" for an entry it cannot open or whose name does not decode as
    UTF-8 (the path then printed with backslash escapes), ".: more than <_MAX_EVIDENCE>
    entries" for a folder that holds more files and folders than that, ".: more than
    <_MAX_EVIDENCE_NAMES> bytes of entry names", ".: more than <_MAX_EVIDENCE_BYTES> bytes" and
    ".: deeper than <_MAX_EVIDENCE_DEPTH> folders" past those bounds, so a flood of evidence
    files neither fills the disk nor makes an unbounded
    event, and "<relative path>: copy failed (<strerror>)" when writing the copy fails (a full
    disk, a quota, an I/O error; "." when dest itself cannot be made). It writes nothing to dest
    after that and deletes what it copied.

    The GateInfraError carries two arguments: "GATE-EVIDENCE-LINK" and the detail. dest must not
    exist yet; its parent folders are made when missing."""
    if os.path.lexists(dest):
        raise ValueError("the judged folder already exists: %s" % dest)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        top = os.open(src, flags)
    except OSError as exc:
        raise GateInfraError("GATE-EVIDENCE-LINK", ".: the evidence folder cannot be opened as "
                             "a folder without following a link (%s)"
                             % (exc.strerror or exc)) from None
    made = False
    out: dict = {}
    try:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            os.mkdir(dest, 0o700)
            made = True
            os.chmod(dest, 0o700)
        except OSError as exc:
            _refuse_entry(".", "copy failed (%s)" % (exc.strerror or exc))
        device = os.fstat(top).st_dev
        _copy_level(top, dest, "", device, out, [0, 0, 0])
    except BaseException:
        if made:
            _remove(dest)
        raise
    finally:
        os.close(top)
    return dict(sorted(out.items()))


def _refuse_entry(rel: str, what: str):
    raise GateInfraError("GATE-EVIDENCE-LINK", "%s: %s" % (_printable(rel), what))


def _printable(text: str) -> str:
    """text with any byte that is not UTF-8 shown as a backslash escape, so it can be recorded."""
    return os.fsencode(text).decode("utf-8", "backslashreplace")


def _open_refusal(exc: OSError) -> str:
    """The reason word for an entry os.open refused: a link met with O_NOFOLLOW is "symlink", a
    non-folder met with O_DIRECTORY is "special file", anything else "unreadable"."""
    if exc.errno == errno.ELOOP:
        return "symlink"
    if exc.errno == errno.ENOTDIR:
        return "special file"
    return "unreadable"


def _copy_failed(path: str, exc: OSError):
    _refuse_entry(path, "copy failed (%s)" % (exc.strerror or exc))


def _copy_level(dir_fd: int, dest: str, rel: str, device: int, out: dict, seen: list,
                depth: int = 0) -> None:
    """Copy one folder level, `depth` folders below the evidence folder; seen is [entries, name
    bytes, content bytes] so far. A folder deeper than _MAX_EVIDENCE_DEPTH is refused before it is
    opened, so the recursion and the descriptors it holds open stay bounded."""
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError:
        _refuse_entry(rel or ".", "unreadable")
    for name in names:
        seen[0] += 1
        if seen[0] > _MAX_EVIDENCE:
            _refuse_entry(".", "more than %d entries" % _MAX_EVIDENCE)
        path = name if not rel else rel + "/" + name
        try:
            seen[1] += len(path.encode("utf-8"))
        except UnicodeEncodeError:
            _refuse_entry(path, "name not UTF-8")
        if seen[1] > _MAX_EVIDENCE_NAMES:
            _refuse_entry(".", "more than %d bytes of entry names" % _MAX_EVIDENCE_NAMES)
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            _refuse_entry(path, "unreadable")
        if stat.S_ISLNK(st.st_mode):
            _refuse_entry(path, "symlink")
        if stat.S_ISDIR(st.st_mode):
            if depth + 1 > _MAX_EVIDENCE_DEPTH:
                _refuse_entry(".", "deeper than %d folders" % _MAX_EVIDENCE_DEPTH)
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(name, flags, dir_fd=dir_fd)
            except OSError as exc:
                _refuse_entry(path, _open_refusal(exc))
            try:
                fst = os.fstat(fd)
                if not stat.S_ISDIR(fst.st_mode):
                    _refuse_entry(path, "special file")
                if fst.st_dev != device:
                    _refuse_entry(path, "outside")
                target = os.path.join(dest, name)
                try:
                    os.mkdir(target, 0o700)
                except OSError as exc:
                    _copy_failed(path, exc)
                _copy_level(fd, target, path, device, out, seen, depth + 1)
            finally:
                os.close(fd)
            continue
        if not stat.S_ISREG(st.st_mode):
            _refuse_entry(path, "special file")
        flags = (os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except OSError as exc:
            _refuse_entry(path, _open_refusal(exc))
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode):
                _refuse_entry(path, "special file")
            if fst.st_dev != device:
                _refuse_entry(path, "outside")
            if seen[2] + fst.st_size > _MAX_EVIDENCE_BYTES:
                _refuse_entry(".", "more than %d bytes" % _MAX_EVIDENCE_BYTES)
            digest = hashlib.sha256()
            target = os.path.join(dest, name)
            try:
                wfd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600)
            except OSError as exc:
                _copy_failed(path, exc)
            try:
                while True:
                    try:
                        chunk = os.read(fd, 1 << 20)
                    except OSError:
                        _refuse_entry(path, "unreadable")
                    if not chunk:
                        break
                    seen[2] += len(chunk)
                    if seen[2] > _MAX_EVIDENCE_BYTES:
                        _refuse_entry(".", "more than %d bytes" % _MAX_EVIDENCE_BYTES)
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        try:
                            view = view[os.write(wfd, view):]
                        except OSError as exc:
                            _copy_failed(path, exc)
            finally:
                try:
                    os.close(wfd)
                except OSError as exc:        # a delayed write error (a quota, NFS)
                    _copy_failed(path, exc)
            out[path] = digest.hexdigest()
        finally:
            os.close(fd)


# ------------------------------------------------------------------ one-tree test items
def run_item(conn, root: str, *, run: str, task: str, contract: Contract, item: str,
             wt: WorktreeInfo, side: str, session: str, reproducer_dir: str | None = None,
             timeout_s: float | None = None) -> GateResult:
    """One fail-to-pass or pass-to-pass item on one tree: `item` is "fail-to-pass:<case id>" or
    "pass-to-pass"; its command (the case's fail_to_pass command, or
    contract.pass_to_pass.command) runs once on wt under its own timeout (or timeout_s when
    smaller) through procs.run in an offline gate jail with the frozen overlays and, for a
    fail-to-pass item, reproducer_dir bound read-only as `${REPRODUCER_DIR}`; the log's TEST
    markers go into GateResult.tests (markers.parse_tests). Records one research-gate event with
    `side` ("base", "attempt" or "verify") and returns the GateResult with its event id. Compares
    nothing: the caller folds the result.

    The event's verdict is PASS when the run gave a valid TEST report, FAIL with GATE-MARKERS when
    its log lacks the markers or reports a test twice with different results, and BLOCKED when
    the jail did not start or the run timed out. A fail-to-pass item with no reproducer_dir runs
    the case's frozen reproducer folder."""
    if side not in _SIDES:
        raise ValueError("side is one of %s, not %r" % (", ".join(_SIDES), side))
    cx = _Cx(conn, root, run, task, contract, session)
    return _run_item(cx, item=item, wt=wt, side=side, reproducer_dir=reproducer_dir,
                     timeout_s=timeout_s)


def _run_item(cx, *, item, wt, side, reproducer_dir=None, timeout_s=None) -> GateResult:
    contract = cx.contract
    if item == _PTP:
        spec = contract.pass_to_pass
        if spec is None:
            raise ValueError("the contract declares no pass_to_pass")
        command, own, reproducer = spec.command, spec.timeout_s, None
    elif item.startswith(_FTP):
        case = _case(contract, item[len(_FTP):])
        command, own = case.fail_to_pass.command, case.fail_to_pass.timeout_s
        reproducer = reproducer_dir or _frozen_reproducer(cx, case)
    else:
        raise ValueError("run_item takes a fail-to-pass or pass-to-pass item, not %r" % (item,))
    timeout = min(float(own), float(timeout_s)) if timeout_s else float(own)
    side_run = _test_side(cx, item=item, nn=_position(contract, item), tree=wt, side=side,
                          command=command, timeout=timeout, repeats=1, reproducer=reproducer,
                          prefer="PASS")
    return _record_side(cx, side_run, verdict=side_run["verdict"])


# ------------------------------------------------------------------------- prepare stage
def run_prepare(conn, root: str, *, run: str, task: str, contract: Contract,
                wt: WorktreeInfo, session: str) -> GateResult:
    """The repair task's prepare stage (contract.repair_prepare, a stage id) on wt, a fresh tree
    of the repair clone at its root commit: run and judged as a gate stage is by run_chain (its
    runbook timeout, an offline gate jail with the frozen overlays and no reproducer, a heavy
    stage's locks, its checks on the judged copy), with `${WORKTREE}` the tree. The cwd is
    writable, so the stage's output lands in the tree. Records one research-gate event with ref
    the repair task, `stage` the stage id and `side` "prepare". Its product is the tree, so a
    recorded prepare result is never reused: every call runs the stage (a resume that cuts a
    fresh tree gets that tree prepared)."""
    if not contract.repair_prepare:
        raise ValueError("the contract declares no repair.prepare stage")
    cx = _Cx(conn, root, run, task, contract, session)
    return _run_stage(cx, item=contract.repair_prepare, nn=0, wt=wt, side="prepare")


# ------------------------------------------------------------------------- holdout stage
def run_holdout_stage(conn, root: str, *, run: str, contract: Contract, stage: str,
                      wt: WorktreeInfo, session: str) -> GateResult:
    """One holdout stage (a stage id of contract.holdout_stages) on wt, the check's own tree of the
    champion (holdout.run_holdout cuts it at paths.holdout_dir(root, run, stage)/tree): run and
    judged as a gate stage is by run_chain (its runbook timeout and retry rule, its checks on the
    judged copy), in an offline jail of kind holdout. Its folder is paths.holdout_dir(root, run,
    stage), so the evidence, the judged copies, the logs and the homes lie beside the tree, and
    the frozen held-out files of contract/holdout/ are overlaid read-only at their paths in the
    tree on top of the frozen copies of contract/files/. Records one research-gate event with
    ref the run and side "holdout"; the side is never reused (holdout.run_holdout reuses by
    champion through research-holdout)."""
    if stage not in tuple(contract.holdout_stages or ()):
        raise ValueError("%r is not a holdout stage of the contract" % (stage,))
    cx = _Cx(conn, root, run, run, contract, session, kind="holdout",
             stage_dir=paths.holdout_dir(root, run, stage))
    return _run_stage(cx, item=stage, nn=0, wt=wt, side="holdout")


# ------------------------------------------------------------ fail-to-pass, pass-to-pass
def fail_to_pass(conn, root: str, *, run: str, task: str, contract: Contract, case: str,
                 base: WorktreeInfo, attempt: WorktreeInfo, session: str) -> FailToPass:
    """For a fix attempt only (run_chain with repair_case set). The case's frozen reproducer,
    `${REPRODUCER_DIR}`, mounted only into this stage's gate jail: paths.hidden_dir(root, run,
    case) for origin store, or paths.reproducer_dir(root, run, case) for origin generate, whose
    files must still hash to the newest research-reproducer event of the repair task (else the
    stage is BLOCKED). Run with the case's fail_to_pass command, timeout and repeats on the base
    tree and on the attempt tree; each verdict from its named TEST marker (the case's `tests`, or
    the names frozen with the proof); a test that passes on the base is refused as weak."""
    cx = _Cx(conn, root, run, task, contract, session)
    result, _got, _code = _fail_to_pass(cx, case=case, base=base, attempt=attempt,
                                        nn=_position(contract, _FTP + case))
    return result


def pass_to_pass(conn, root: str, *, run: str, task: str, contract: Contract,
                 base: WorktreeInfo, attempt: WorktreeInfo, baseline: tuple,
                 session: str) -> PassToPass:
    """contract.pass_to_pass.command from the frozen contract copy (`${CONTRACT_DIR}`) on both
    trees, under its timeout; PassToPass.baseline is `baseline` as given, and the attempt's pass
    set may not lose a test of it: monotonicity.diff({t: 1 for t in baseline},
    {t: 1 for t in attempt_pass}) and each MISSING violation it returns names one lost test.
    The contract.pass_to_pass.min_tests floor applies to the baseline given: a non-empty baseline
    with fewer tests makes the item BLOCKED."""
    cx = _Cx(conn, root, run, task, contract, session)
    result, _got, _code = _pass_to_pass(cx, base=base, attempt=attempt, baseline=tuple(baseline),
                                        nn=_position(contract, _PTP))
    return result


def _fail_to_pass(cx, *, case: str, base, attempt, nn):
    """(FailToPass, GateResults, item verdict)."""
    item = _FTP + case
    spec = _case(cx.contract, case)
    names = tuple(spec.fail_to_pass.tests)
    empty = FailToPass(tests=names, base={}, attempt={}, weak=(), ok=False)
    rep_dir = _frozen_reproducer(cx, spec)
    proof_tests, problem = _reproducer_check(cx, spec, rep_dir)
    if problem:
        result = _record_blocked(cx, item=item, side="base", detail=problem)
        return empty, (result,), vc.BLOCKED
    names = names or tuple(proof_tests)
    if not names:
        result = _record_blocked(cx, item=item, side="base",
                                 detail="case %s names no reproducer test" % case)
        return empty, (result,), vc.BLOCKED
    ftp = spec.fail_to_pass
    got = []
    base_run = _test_side(cx, item=item, nn=nn, tree=base, side="base", command=ftp.command,
                          timeout=ftp.timeout_s, repeats=ftp.repeats, reproducer=rep_dir,
                          prefer="FAIL")
    if base_run["verdict"] != vc.PASS:
        got.append(_record_side(cx, base_run, verdict=base_run["verdict"],
                                extra={"tests_named": list(names)}))
        return (FailToPass(tests=names, base={}, attempt={}, weak=(), ok=False,
                           base_event=got[0].event_id), tuple(got), base_run["verdict"])
    base_fold = {n: base_run["tests"][n] for n in names if n in base_run["tests"]}
    weak = tuple(n for n in names if base_fold.get(n) != "FAIL")
    base_code = vc.FAIL if weak else vc.PASS
    base_detail = ("weak: %s does not fail on the base tree" % ", ".join(weak)) if weak else None
    got.append(_record_side(cx, base_run, verdict=base_code, detail=base_detail,
                            extra={"tests_named": list(names), "weak": list(weak)}))
    att_run = _test_side(cx, item=item, nn=nn, tree=attempt, side="attempt", command=ftp.command,
                         timeout=ftp.timeout_s, repeats=ftp.repeats, reproducer=rep_dir,
                         prefer="PASS")
    if att_run["verdict"] != vc.PASS:
        got.append(_record_side(cx, att_run, verdict=att_run["verdict"],
                                extra={"tests_named": list(names)}))
        result = FailToPass(tests=names, base=base_fold, attempt={}, weak=weak, ok=False,
                            base_event=got[0].event_id, attempt_event=got[1].event_id)
        return result, tuple(got), band.worst([base_code, att_run["verdict"]])
    att_fold = {n: att_run["tests"][n] for n in names if n in att_run["tests"]}
    unfixed = tuple(n for n in names if att_fold.get(n) != "PASS")
    ok = not weak and not unfixed
    att_code = vc.FAIL if unfixed else vc.PASS
    att_detail = ("not fixed: %s does not pass on the attempt tree" % ", ".join(unfixed)
                  if unfixed else None)
    got.append(_record_side(cx, att_run, verdict=att_code, detail=att_detail,
                            extra={"tests_named": list(names), "weak": list(weak),
                                   "fail_to_pass": {"ok": ok, "base": base_fold,
                                                    "attempt": att_fold}}))
    result = FailToPass(tests=names, base=base_fold, attempt=att_fold, weak=weak, ok=ok,
                        base_event=got[0].event_id, attempt_event=got[1].event_id)
    return result, tuple(got), (vc.PASS if ok else band.worst([base_code, att_code]))


def _pass_to_pass(cx, *, base, attempt, baseline: tuple, nn):
    """(PassToPass, GateResults, item verdict)."""
    spec = cx.contract.pass_to_pass
    empty = PassToPass(baseline=baseline, base_pass=(), attempt_pass=(), lost=(), ok=False)
    if spec is None:
        result = _record_blocked(cx, item=_PTP, side="attempt",
                                 detail="the contract declares no pass_to_pass")
        return empty, (result,), vc.BLOCKED
    if baseline and len(set(baseline)) < int(spec.min_tests):
        result = _record_blocked(cx, item=_PTP, side="attempt",
                                 detail="the baseline holds %d test(s), fewer than min_tests %d"
                                 % (len(set(baseline)), spec.min_tests))
        return empty, (result,), vc.BLOCKED
    got = []
    base_run = _test_side(cx, item=_PTP, nn=nn, tree=base, side="base", command=spec.command,
                          timeout=spec.timeout_s, repeats=1, reproducer=None, prefer="PASS")
    got.append(_record_side(cx, base_run, verdict=base_run["verdict"]))
    if base_run["verdict"] != vc.PASS:
        return (PassToPass(baseline=baseline, base_pass=(), attempt_pass=(), lost=(), ok=False,
                           base_event=got[0].event_id), tuple(got), base_run["verdict"])
    base_pass = tuple(sorted(n for n, r in base_run["tests"].items() if r == "PASS"))
    att_run = _test_side(cx, item=_PTP, nn=nn, tree=attempt, side="attempt", command=spec.command,
                         timeout=spec.timeout_s, repeats=1, reproducer=None, prefer="PASS")
    if att_run["verdict"] != vc.PASS:
        got.append(_record_side(cx, att_run, verdict=att_run["verdict"]))
        return (PassToPass(baseline=baseline, base_pass=base_pass, attempt_pass=(), lost=(),
                           ok=False, base_event=got[0].event_id, attempt_event=got[1].event_id),
                tuple(got), att_run["verdict"])
    attempt_pass = tuple(sorted(n for n, r in att_run["tests"].items() if r == "PASS"))
    violations = monotonicity.diff({t: 1 for t in baseline}, {t: 1 for t in attempt_pass})
    lost = tuple(t for t in sorted(set(baseline))
                 if any(v.startswith("%s gate=%s baseline=" % (monotonicity.MISSING, t))
                        for v in violations))
    ok = not violations
    code = vc.PASS if ok else vc.FAIL
    detail = None if ok else "lost: %s" % ", ".join(lost or violations)
    got.append(_record_side(cx, att_run, verdict=code, detail=detail,
                            extra={"pass_to_pass": {"baseline": sorted(set(baseline)),
                                                    "base_pass": list(base_pass),
                                                    "attempt_pass": list(attempt_pass),
                                                    "lost": list(lost), "ok": ok}}))
    result = PassToPass(baseline=baseline, base_pass=base_pass, attempt_pass=attempt_pass,
                        lost=lost, ok=ok, base_event=got[0].event_id,
                        attempt_event=got[1].event_id)
    return result, tuple(got), code


# ----------------------------------------------------------------------- verifier re-runs
def _rerun_tree(value, side: str):
    """The tree of one side of a verifier's re-run as a WorktreeInfo. The verifier's serve thread
    holds its try's trees as folder paths (verifier.serve's `trees`: {"folder", "tree", "base"}),
    so a path string of an existing folder is taken as that tree (only its path is read here); a
    WorktreeInfo is kept as it is, and anything else is no tree (t-173 fix batch 3, F15: every
    `--gate` request of the session runtime and of the headless path was refused with "no tree
    tree was given for the re-run", record events 77676 to 77738 of rr-002)."""
    if isinstance(value, WorktreeInfo):
        return value
    if isinstance(value, (str, os.PathLike)):
        path = os.path.abspath(os.fspath(value))
        if os.path.isdir(path) and not os.path.islink(path):
            return WorktreeInfo(path=path, label=side, commit="", admin_dir=None)
    return None


def verify_rerun(conn, root: str, *, run: str, task: str, contract: Contract, request: dict,
                 trees: dict, session: str) -> dict:
    """A verifier's `--gate` request (verifier.md sections 4.1 and 5.6): request {stage, side
    ("tree" or "base"), n}, trees {"tree": WorktreeInfo, "base": WorktreeInfo} of the verifier's
    try, or the folder path of each, as the serve thread holds them (_rerun_tree). A request
    whose stage is not an item of chain(contract) for the task's repair case, or whose side is
    not "tree" or "base", is refused: nothing runs, and one research-gate event
    with side "verify", verdict BLOCKED and `refused` (the reason) is recorded. Otherwise the one
    requested stage runs on the single requested tree only, in an offline gate jail, with the
    smaller of its own timeout and contract.verifier_rerun_timeout_s; heavy stages take their
    machine locks. Returns the receipt {stage, side, n, rc, verdict, tests, log, log_sha256,
    input_sha256, lock_wait_s, event, refused}.

    The task's repair case comes only from its record (the case of its own fail-to-pass
    research-gate events); a task without one has no fail-to-pass or pass-to-pass item, so such a
    request is refused. The re-run works in <try folder>/gates/<nn>-<slug>/, the try folder being
    the parent of the requested tree, and the receipt's log_sha256 is the one its event
    recorded."""
    request = request if isinstance(request, dict) else {}
    stage, side, n = request.get("stage"), request.get("side"), request.get("n")
    refused = None
    tree = _rerun_tree((trees or {}).get(side), side) if side in ("tree", "base") else None
    gates_root = None
    if isinstance(tree, WorktreeInfo):
        gates_root = os.path.join(os.path.dirname(os.path.normpath(os.path.abspath(tree.path))),
                                  "gates")
    cx = _Cx(conn, root, run, task, contract, session, gates_root=gates_root)
    case = _task_case(cx)
    if side not in ("tree", "base"):
        refused = "side %r is not tree or base" % (side,)
    else:
        try:
            items = chain(contract, repair_case=case)
        except ValueError:
            items = chain(contract)
        if not isinstance(stage, str) or stage not in items:
            refused = "stage %r is not an item of the chain" % (stage,)
            if (isinstance(stage, str) and (stage.startswith(_FTP) or stage == _PTP)
                    and case is None):
                refused += " (the task's record holds no fail-to-pass of a repair case)"
        elif not isinstance(tree, WorktreeInfo):
            refused = "no %s tree was given for the re-run" % side
    if refused:
        eid = _record(cx, item=str(stage)[:200], side="verify", verdict=vc.BLOCKED,
                      detail=refused, refused=refused, extra={"request": {"side": str(side)[:20],
                                                                          "n": n}})
        return {"stage": stage, "side": side, "n": n, "rc": None,
                "verdict": vc.name_of(vc.BLOCKED), "tests": None, "log": None,
                "log_sha256": None, "input_sha256": None, "lock_wait_s": 0.0, "event": eid,
                "refused": refused}
    cap = float(contract.verifier_rerun_timeout_s)
    if stage.startswith(_FTP) or stage == _PTP:
        reproducer = None
        if stage.startswith(_FTP):
            reproducer = _frozen_reproducer(cx, _case(contract, stage[len(_FTP):]))
        result = _run_item(cx, item=stage, wt=tree, side="verify", reproducer_dir=reproducer,
                           timeout_s=cap)
    else:
        done = _run_chain(conn, root, run=run, task=task, contract=contract, wt=tree,
                          mode=contract.mode, session=session, side="verify", stages=(stage,),
                          repair_case=None, base=None, baseline=None, timeout_cap=cap,
                          gates_root=gates_root)
        result = done.results[-1]
    recorded = _event_data(cx, result.event_id)
    return {"stage": stage, "side": side, "n": n, "rc": result.rc,
            "verdict": vc.name_of(result.verdict),
            "tests": dict(result.tests.tests) if result.tests else None, "log": result.log,
            "log_sha256": recorded.get("log_sha256"), "input_sha256": result.input_sha256,
            "lock_wait_s": result.lock_wait_s, "event": result.event_id, "refused": None}


# --------------------------------------------------------------------------- metrics
def measure(conn, root: str, *, run: str, task: str, contract: Contract, wt: WorktreeInfo,
            session: str) -> dict[str, MetricValue]:
    """Each metric with its declared repeats, seeds and timeout; every repeat and the median
    recorded. For an even count Metric.median picks it: "worse" takes the worse of the two middle
    values for the metric's direction, "middle" their mean. A json-field value is read from the
    repeat's judged_copy, never from the live evidence folder (run_chain above).

    Unlike run_chain, every metric is measured even when an earlier one failed."""
    cx = _Cx(conn, root, run, task, contract, session)
    full = chain(contract)
    out = {}
    for metric in contract.metrics:
        item = _METRIC + metric.id
        nn = full.index(item) + 1 if item in full else len(full) + 1
        value, _result = _run_metric(cx, metric=metric, nn=nn, wt=wt, side="attempt")
        out[metric.id] = value
    return out


def _median(values: list, direction: str, rule: str):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    low, high = ordered[mid - 1], ordered[mid]
    if rule == "middle":
        # halve first: (low + high) overflows to inf for two finite values near the float limit
        return low / 2.0 + high / 2.0
    return low if direction == "higher" else high


def _spread(values: list):
    """max - min of the ok values, or None; inf when the difference overflows (the caller
    refuses a non-finite spread)."""
    return (max(values) - min(values)) if values else None


def _run_metric(cx, *, metric, nn, wt, side, timeout_cap=None):
    """(MetricValue, GateResult) for one metric: every repeat in its own jail, parsed from the
    log (markers) or the repeat's judged copy (json-field)."""
    item = _METRIC + metric.id
    tree = _dir_digest(wt.path)
    fp = _fingerprint(cx, wt.path, item, tree=tree)
    if side not in _NEVER_REUSED:
        prev = _find_reusable(cx, item, side, fp)
        if prev is not None and isinstance(prev[1].get("metric"), dict):
            result = _record_reused(cx, prev, item=item, side=side, fp=fp)
            m = prev[1]["metric"]
            value = MetricValue(metric=metric.id, median=m.get("median"),
                                repeats=tuple(m.get("values") or ()), n_ok=int(m.get("n_ok") or 0),
                                spread=m.get("spread"), untested=bool(m.get("untested")))
            return value, result
    before = _frozen_state(cx)
    stage_dir = _stage_dir(cx, nn, item)
    tag = _claim_tag(stage_dir, side)
    timeout = float(metric.timeout_s)
    if timeout_cap:
        timeout = min(timeout, float(timeout_cap))
    repeats = max(1, int(metric.repeats))
    samples, rows = [], []
    blocked = None
    marker_failure = False
    last = None
    for r in range(1, repeats + 1):
        seed = metric.seeds[r - 1] if len(metric.seeds) >= r else None
        last = _run_command(cx, stage_dir=stage_dir, wt=wt, tag=tag, repeat=r, stage=item,
                            command=metric.command, workdir=metric.workdir, env_map=None,
                            timeout=timeout, seed=seed)
        row = _row(cx, last, repeat=r)
        row["seed"] = seed
        if last["blocked"]:
            blocked = last
            row.update(value=None, ok=False)
            rows.append(row)
            break
        value, why = None, None
        if last["log_over"]:
            marker_failure = True
            why = "markers-conflict: %s" % _log_over_words()
        elif last["timed_out"]:
            why = "timed out after %ss" % _num(timeout)
        elif last["rc"] != 0:
            why = "exit %s" % last["rc"]
        else:
            try:
                value = _metric_value(cx, metric, last)
            except MarkerError as exc:
                marker_failure = True
                why = "%s: %s" % _marker_words(exc)
            except _Unreadable as exc:
                why = str(exc)
        ok = value is not None
        row.update(value=value, ok=ok, reason=why)
        rows.append(row)
        samples.append(MetricSample(metric=metric.id, repeat=r, value=value, ok=ok,
                                    log=last["log"]))
    good = [s.value for s in samples if s.ok]
    n_ok = len(good)
    median = _median(good, metric.direction, metric.median)
    spread = _spread(good)
    # finite repeats can still give an infinite spread (1.7e308 against -1.7e308); such a value
    # is never recorded (strict JSON has no Infinity) and never scored
    overflow = [name for name, got in (("median", median), ("spread", spread))
                if got is not None and not math.isfinite(got)]
    if overflow:
        median = spread = None
    # DD-11 (runbook-format-3.md section 16.4): fewer ok repeats than asked on a noisy metric,
    # or a spread wider than the margin on any metric, leaves the value UNTESTED
    untested = ((metric.kind == "noisy" and n_ok < repeats)
                or (spread is not None and spread > float(metric.margin)))
    value = MetricValue(metric=metric.id, median=median,
                        repeats=tuple(s.value for s in samples), n_ok=n_ok, spread=spread,
                        untested=untested)
    code, reason, detail = vc.PASS, None, None
    if blocked is not None:
        code, reason, detail = vc.BLOCKED, blocked["code"], blocked["detail"]
    elif overflow:
        code, reason = vc.FAIL, "GATE-MARKERS"
        detail = ("markers-conflict: the %s of the repeats is not a finite number"
                  % " and the ".join(overflow))
    elif n_ok < int(metric.min_ok):
        code = vc.FAIL
        reason = "GATE-MARKERS" if marker_failure else None
        detail = "%d of %d repeat(s) ok, fewer than min_ok %d" % (n_ok, repeats, metric.min_ok)
    changed = _frozen_changed(cx, before)
    if changed and code == vc.PASS:
        code, reason, detail = vc.BLOCKED, None, changed
    after = _dir_digest(wt.path)
    summary = {"id": metric.id, "unit": metric.unit, "direction": metric.direction,
               "kind": metric.kind, "median": median, "values": list(value.repeats),
               "n_ok": n_ok, "min_ok": int(metric.min_ok), "spread": spread,
               "untested": untested}
    last = last or {}
    eid = _record(cx, item=item, side=side, verdict=code, code=reason, detail=detail,
                  command=last.get("command"), rc=last.get("rc"),
                  timed_out=bool(last.get("timed_out")), log=last.get("log"), fp=fp,
                  offline=_offline(rows), repeats=rows,
                  extra={"metric": summary, "median": median, "ran": _ran(rows), "tag": tag,
                         "tree_sha256": tree, "tree_after_sha256": after})
    result = GateResult(stage=item, side=side, verdict=code, rc=last.get("rc"),
                        timed_out=bool(last.get("timed_out")), log=last.get("log") or "",
                        input_sha256=fp, offline=_offline(rows), lock_wait_s=0.0, reused=False,
                        samples=tuple(samples), tests=None, event_id=eid)
    return value, result


class _Unreadable(Exception):
    """A json-field metric whose file or field cannot be read as a finite number."""


def _metric_value(cx, metric, run) -> float:
    """One repeat's value. A markers metric reads its METRIC line from the run's token-closed
    block. A json-field metric reads its field from the frozen runbook folder or, for a path
    under ${EVIDENCE_DIR}, from the repeat's judged copy; the judged file then counts only when
    the runner's token-closed block attests its bytes (`EVIDENCE: <path> <sha256>`,
    markers._parse_evidence). Tested code can leave a process in the jail that swaps the file
    after the runner wrote it and before the jail ends, so an unattested file is refused
    (GATE-MARKERS, markers-missing) and a file whose sha256 differs from the attested one is
    refused (markers-conflict). Raises MarkerError or _Unreadable."""
    if metric.parser == "markers":
        return markers.parse_metric(markers._with_token(_log_text(run["log"]), run["token"]),
                                    metric.id)
    if metric.parser != "json-field":
        raise _Unreadable("unknown metric parser %r" % (metric.parser,))
    if not metric.path or not metric.field:
        raise _Unreadable("a json-field metric needs path and field")
    values = dict(run["values"])
    attested = None
    evidence = metric.path.startswith(_EVIDENCE_VAR)
    if evidence:
        base = run["judged"]
        rel = runbook.substitute(metric.path[len(_EVIDENCE_VAR):], values).lstrip("/")
    else:
        base = _frozen_runbook_dir(cx)
        rel = runbook.substitute(metric.path, values)
    path = os.path.normpath(os.path.join(base, rel))
    if not base or os.path.commonpath([path, os.path.abspath(base)]) != os.path.abspath(base):
        raise _Unreadable("%s resolves outside the folder it is read from" % metric.path)
    if evidence:
        attested = markers._parse_evidence(
            markers._with_token(_log_text(run["log"]), run["token"]),
            os.path.relpath(path, os.path.abspath(base)).replace(os.sep, "/"))
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise _Unreadable("%s is not a regular file" % metric.path)
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise _Unreadable("cannot read %s as JSON: %s" % (metric.path, exc)) from None
    if attested is not None:
        held = hashlib.sha256(data).hexdigest()
        if held != attested:
            raise MarkerError("markers-conflict",
                              "%s in the judged copy has sha256 %s, but the runner's block "
                              "attests %s" % (metric.path, held, attested))
    # the file's bytes can come from tested code, so every error of the decode and the field
    # read is an unreadable value, never a raise out of run_chain: json.loads raises
    # RecursionError on deep nesting, and math.isfinite raises OverflowError on an integer too
    # large for a float
    try:
        doc = json.loads(data.decode("utf-8"))
    except Exception as exc:          # noqa: BLE001 - ValueError, RecursionError and the rest
        raise _Unreadable("cannot read %s as JSON: %s" % (metric.path, str(exc)[:200])) from None
    try:
        got = runbook._field(doc, metric.field)
    except KeyError:
        raise _Unreadable("%s has no field %s" % (metric.path, metric.field)) from None
    try:
        number = None if isinstance(got, bool) or not isinstance(got, (int, float)) else float(got)
    except OverflowError:
        number = None
    if number is None or not math.isfinite(number):
        raise _Unreadable("%s %s is not a finite number" % (metric.path, metric.field))
    return number


# ----------------------------------------------------------------------------- stages
def _run_stage(cx, *, item, nn, wt, side, timeout_cap=None) -> GateResult:
    """One gate stage (or the prepare stage): its attempts under the runbook's retry rule and
    fail cases, each command in its own jail, its checks on the judged copy."""
    mapping = _stage_map(cx.contract, item)
    gate = _gate_stage(cx.contract, item)
    if mapping is None or not mapping.get("run"):
        return _record_blocked(cx, item=item, side=side,
                               detail="stage %s is not a stage with a command in the frozen "
                                      "runbook" % item)
    timeout = float(gate.timeout_s if gate else (mapping.get("timeout") or 0))
    if timeout <= 0:
        return _record_blocked(cx, item=item, side=side,
                               detail="stage %s has no timeout" % item)
    if timeout_cap:
        timeout = min(timeout, float(timeout_cap))
    heavy = bool(gate and gate.heavy)
    lock_timeout = float(gate.lock_timeout_s) if gate else 0.0
    guard = (locks.heavy(item, cx.root, timeout_s=lock_timeout) if heavy
             else contextlib.nullcontext(()))
    try:
        with guard as holds:
            # the fingerprint and the reuse lookup come after the locks are held, so a tree that
            # changes while the stage waits for its lock is fingerprinted as it is when it runs
            wait = round(sum(h.waited_s for h in holds), 3)
            tree = _dir_digest(wt.path)
            fp = _fingerprint(cx, wt.path, item, tree=tree)
            if side not in _NEVER_REUSED:
                prev = _find_reusable(cx, item, side, fp)
                if prev is not None:
                    return _record_reused(cx, prev, item=item, side=side, fp=fp)
            return _stage_tries(cx, item=item, nn=nn, wt=wt, side=side, mapping=mapping,
                                timeout=timeout, fp=fp, wait=wait, tree=tree)
    except LockTimeout as exc:
        return _record_blocked(cx, item=item, side=side, code="GATE-LOCK-TIMEOUT",
                               detail=str(exc), fp=_fingerprint(cx, wt.path, item),
                               lock_wait_s=lock_timeout)


def _stage_tries(cx, *, item, nn, wt, side, mapping, timeout, fp, wait, tree) -> GateResult:
    """The stage's tries on wt; `tree` is wt's digest before the first try. The event records it
    and the digest after the last try, so a stage that wrote into its tree is never reused."""
    before = _frozen_state(cx)
    stage_dir = _stage_dir(cx, nn, item)
    tag = _claim_tag(stage_dir, side)
    knobs = _knob_values(cx.contract)
    data = _runbook_data(cx.contract)
    rows = []
    attempt = 1
    end, reason, detail = None, None, None
    last = None
    for _ in range(_MAX_TRIES):
        last = _run_command(cx, stage_dir=stage_dir, wt=wt, tag=tag, repeat=attempt,
                            stage=item, command=mapping.get("run"),
                            workdir=mapping.get("workdir"), env_map=mapping.get("env"),
                            timeout=timeout, knobs=knobs)
        row = _row(cx, last, repeat=attempt)
        row["knobs"] = dict(knobs)
        rows.append(row)
        if last["blocked"]:
            end, reason, detail = vc.BLOCKED, last["code"], last["detail"]
            break
        if last["timed_out"]:
            end, detail = vc.BLOCKED, "stage %s timed out after %ss" % (item, _num(timeout))
            break
        if last["log_over"]:
            end, detail = vc.BLOCKED, "stage %s: %s" % (item, _log_over_words())
            break
        results, why = _checks(cx, mapping.get("checks") or [], run=last, wt=wt, stage=item,
                               attempt=attempt, knobs=knobs, stage_dir=stage_dir, tag=tag)
        row["checks"] = {k: [vc.name_of(v), why[k]] for k, v in results.items()}
        detected = {}
        if any(v != vc.PASS for v in results.values()):
            for fail in mapping.get("fails") or []:
                if isinstance(fail, dict) and isinstance(fail.get("detect"), dict):
                    code, _why = _one_check(cx, fail["detect"], run=last, wt=wt, stage=item,
                                            attempt=attempt, knobs=knobs, stage_dir=stage_dir,
                                            tag=tag)
                    detected[fail.get("id")] = code
        row["detected"] = {str(k): vc.name_of(v) for k, v in detected.items()}
        step = runbook.respond(data, mapping, results, attempt, knobs, detected)
        row["step"] = step.get("reason")
        if step.get("run"):
            ok, why_rec = _recovery(cx, step["run"], stage_dir=stage_dir, wt=wt, tag=tag,
                                    attempt=attempt, knobs=knobs)
            row["recovery"] = {"stage": step["run"], "ok": ok, "reason": why_rec}
            if not ok:
                end, detail = vc.FAIL, "recovery stage %s failed: %s" % (step["run"], why_rec)
                break
            attempt += 1
            continue
        if step.get("end") is not None:
            end = step["end"]
            if end != vc.PASS:
                failing = sorted(k for k, v in results.items() if v != vc.PASS)
                detail = "%s; failing check(s): %s" % (step.get("reason"), ", ".join(failing))
            break
        knobs = dict(step.get("knobs") or knobs)
        attempt += 1
    else:
        end, detail = vc.BLOCKED, "stage %s passed %d tries without an end" % (item, _MAX_TRIES)
    changed = _frozen_changed(cx, before)
    if changed and end == vc.PASS:
        end, reason, detail = vc.BLOCKED, None, changed
    after = _dir_digest(wt.path)
    eid = _record(cx, item=item, side=side, verdict=end, code=reason, detail=detail,
                  command=last.get("command"), rc=last.get("rc"),
                  timed_out=bool(last.get("timed_out")), log=last.get("log"), fp=fp,
                  offline=_offline(rows), lock_wait_s=wait, repeats=rows,
                  extra={"ran": _ran(rows), "attempts": attempt,
                         "evidence": last.get("evidence"),
                         "judged": _rel(cx.root, last.get("judged")), "tag": tag,
                         "tree_sha256": tree, "tree_after_sha256": after})
    return GateResult(stage=item, side=side, verdict=end, rc=last.get("rc"),
                      timed_out=bool(last.get("timed_out")), log=last.get("log") or "",
                      input_sha256=fp, offline=_offline(rows), lock_wait_s=wait, reused=False,
                      samples=(), tests=None, event_id=eid)


def _recovery(cx, stage_id, *, stage_dir, wt, tag, attempt, knobs):
    """Run a recovery stage once in its own jail; (every check passed, reason)."""
    mapping = _stage_map(cx.contract, stage_id)
    if mapping is None or not mapping.get("run"):
        return False, "stage %s is not in the frozen runbook" % stage_id
    rec_dir = os.path.join(stage_dir, "recovery-%s" % paths.stage_slug(stage_id))
    timeout = float(mapping.get("timeout") or 0)
    if timeout <= 0:
        return False, "stage %s has no timeout" % stage_id
    done = _run_command(cx, stage_dir=rec_dir, wt=wt, tag=tag, repeat=attempt, stage=stage_id,
                        command=mapping.get("run"), workdir=mapping.get("workdir"),
                        env_map=mapping.get("env"), timeout=timeout, knobs=knobs)
    if done["blocked"]:
        return False, done["detail"]
    if done["timed_out"]:
        return False, "timed out after %ss" % _num(timeout)
    if done["log_over"]:
        return False, _log_over_words()
    results, why = _checks(cx, mapping.get("checks") or [], run=done, wt=wt, stage=stage_id,
                           attempt=attempt, knobs=knobs, stage_dir=rec_dir, tag=tag)
    failing = sorted(k for k, v in results.items() if v != vc.PASS)
    if failing or not results:
        return False, "failing check(s): %s" % (", ".join(failing) or "none declared")
    return True, "every check passed"


def _checks(cx, checks, **kw):
    results, why = {}, {}
    for chk in checks:
        if not isinstance(chk, dict):
            continue
        ident = str(chk.get("id"))
        results[ident], why[ident] = _one_check(cx, chk, **kw)
    return results, why


def _one_check(cx, chk, *, run, wt, stage, attempt, knobs, stage_dir, tag):
    """(verdict code, reason) of one check or detect, judged on the run's judged copy."""
    if chk.get("type") == "plugin":
        return _plugin(cx, chk, run=run, wt=wt, stage=stage, attempt=attempt, knobs=knobs,
                       stage_dir=stage_dir, tag=tag)
    frozen = _frozen_runbook_dir(cx)
    try:
        return runbook.evaluate(chk, workdir=frozen, runbook_dir=frozen, exit_code=run["rc"],
                                knobs=knobs, stage=stage, attempt=attempt,
                                evidence_dir=run["judged"])
    except Exception as exc:          # a malformed check reads as missing input, never a pass
        return vc.BLOCKED, "check %s could not be judged: %s" % (chk.get("id"), exc)


def _plugin_env(*, runbook_dir, stage, check, attempt, evidence, knobs, worktree, contract_dir):
    """The ALPACA_* names a plugin check gets: those runbook._run_plugin sets (its env dict
    of ALPACA_RUNBOOK_DIR, ALPACA_STAGE, ALPACA_CHECK, ALPACA_ATTEMPT, ALPACA_EVIDENCE_DIR and
    ALPACA_KNOB_*) and ALPACA_WORKTREE and ALPACA_CONTRACT_DIR (t-088's names)."""
    env = {"ALPACA_RUNBOOK_DIR": runbook_dir, "ALPACA_STAGE": str(stage),
           "ALPACA_CHECK": str(check), "ALPACA_ATTEMPT": str(attempt),
           "ALPACA_EVIDENCE_DIR": evidence or "", "ALPACA_WORKTREE": worktree,
           "ALPACA_CONTRACT_DIR": contract_dir}
    for name, value in (knobs or {}).items():
        env["ALPACA_KNOB_%s" % name] = str(value)
    return env


def _plugin_verdict(*, script, started, timed_out, rc, output, timeout, error=None):
    """runbook._run_plugin's mapping (its except clauses and verdict tail; cited by name so the
    pointer survives line drift in alpaca/runbook.py): a timeout or a start failure is BLOCKED,
    an exit in the verdict band is that verdict with the last output line as the reason, any
    other exit is BLOCKED."""
    if not started:
        return vc.BLOCKED, "plugin %s did not start: %s" % (script, error or "no jail")
    if timed_out:
        return vc.BLOCKED, "plugin %s timed out after %ss" % (script, _num(timeout))
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    reason = lines[-1][:300] if lines else "no reason printed"
    if rc in vc.VERDICT_BAND:
        return rc, reason
    return vc.BLOCKED, "plugin exit %s is outside the verdict band: %s" % (rc, reason)


def _plugin(cx, chk, *, run, wt, stage, attempt, knobs, stage_dir, tag):
    frozen = _frozen_runbook_dir(cx)
    script_rel = str(chk.get("script") or "")
    script = os.path.normpath(os.path.join(frozen, script_rel))
    if not script_rel or os.path.commonpath([script, frozen]) != frozen:
        return vc.BLOCKED, "plugin %s is not a script in the runbook folder" % script_rel
    evidence = run["evidence_dir"]
    values = dict(knobs)
    values.update(RUNBOOK_DIR=frozen, STAGE=stage, ATTEMPT=attempt, EVIDENCE_DIR=evidence)
    args = [runbook.substitute(str(a), values) for a in chk.get("args", [])]
    timeout = chk.get("timeout", runbook.PLUGIN_TIMEOUT)
    check_dir = os.path.join(stage_dir, "checks", paths.stage_slug(str(chk.get("id") or "check")))
    # a check (or a detect) may run more than once in a run, so each call takes its own tag
    ptag = _claim_tag(check_dir, tag)
    penv = _plugin_env(runbook_dir=frozen, stage=stage, check=chk.get("id"), attempt=attempt,
                       evidence=evidence, knobs=knobs, worktree=wt.path,
                       contract_dir=_files_dir(cx))
    done = _jailed(cx, wt_path=wt.path, cwd=run["cwd"], stage_dir=check_dir, tag=ptag,
                   repeat=attempt, argv=[script] + args,
                   extra={"WORKTREE": wt.path, "CONTRACT_DIR": _files_dir(cx),
                          "EVIDENCE_DIR": evidence, "PWD": run["cwd"]},
                   timeout=float(timeout), evidence=evidence, evidence_rw=False,
                   plugin_env=penv)
    failed = _cleanup(paths.stage_home(check_dir, ptag, attempt))
    if done["started"] and done["log_over"]:
        return vc.BLOCKED, "plugin %s: %s" % (script_rel, _log_over_words())
    code, why = _plugin_verdict(script=script_rel, started=done["started"],
                                timed_out=done["timed_out"], rc=done["rc"],
                                output=_log_tail(done["log"]) if done["log"] else "",
                                timeout=timeout, error=done.get("detail"))
    return code, (why if not failed else "%s (%s)" % (why, failed))


# ---------------------------------------------------------------- test runs (one side)
def _test_side(cx, *, item, nn, tree, side, command, timeout, repeats, reproducer, prefer):
    """Run a TEST-marker command `repeats` times on one tree; a dict the caller records. Its
    verdict is PASS for a valid report, FAIL (GATE-MARKERS) without one (a block not closed by
    `ALPACA-RESULTS-END <this run's token>` is none, and neither is a log over _MAX_LOG_BYTES),
    BLOCKED when a jail did not start or a run timed out. A run that died from a signal (rc
    below 0 or 128 and above) gives no valid report: code in the tree could have printed a
    forged block and killed the command before its real lines, so it is FAIL with GATE-MARKERS,
    "killed by signal". A plain non-zero exit keeps its report. `tests` folds the repeats: a
    name keeps `prefer` only when every repeat reported it so. `tree` and `tree_after` are the
    tree's digests before and after the runs, which the event records (a side that wrote into its
    tree is never reused)."""
    extra = _dir_digest(reproducer) if reproducer else ""
    digest = _dir_digest(tree.path)
    fp = _fingerprint(cx, tree.path, item, extra=extra, tree=digest)
    out = {"item": item, "side": side, "fp": fp, "tests": {}, "rows": [], "last": {},
           "verdict": vc.PASS, "code": None, "detail": None, "reused": None, "tree": digest,
           "tree_after": None}
    if side not in _NEVER_REUSED:
        prev = _find_reusable(cx, item, side, fp)
        if prev is not None and isinstance(prev[1].get("tests"), dict):
            out.update(reused=prev, tests=dict(prev[1]["tests"]))
            return out
    if reproducer is not None and not os.path.isdir(reproducer):
        out.update(verdict=vc.BLOCKED, detail="the reproducer folder is missing: %s"
                   % _rel(cx.root, reproducer))
        return out
    _test_runs(cx, out, item=item, nn=nn, tree=tree, side=side, command=command,
               timeout=timeout, repeats=repeats, reproducer=reproducer, prefer=prefer)
    out["tree_after"] = _dir_digest(tree.path)
    return out


def _test_runs(cx, out, *, item, nn, tree, side, command, timeout, repeats, reproducer, prefer):
    """The runs of _test_side, filling `out` in place."""
    before = _frozen_state(cx)
    stage_dir = _stage_dir(cx, nn, item)
    tag = _claim_tag(stage_dir, side)
    out["tag"] = tag
    reports = []
    for r in range(1, max(1, int(repeats)) + 1):
        done = _run_command(cx, stage_dir=stage_dir, wt=tree, tag=tag, repeat=r, stage=item,
                            command=command, workdir=None, env_map=None, timeout=float(timeout),
                            reproducer=reproducer)
        out["last"] = done
        out["rows"].append(_row(cx, done, repeat=r))
        if done["blocked"]:
            out.update(verdict=vc.BLOCKED, code=done["code"], detail=done["detail"])
            return
        if done["timed_out"]:
            out.update(verdict=vc.BLOCKED, detail="%s timed out after %ss" % (item, _num(timeout)))
            return
        if done["log_over"]:
            out.update(verdict=vc.FAIL, code="GATE-MARKERS",
                       detail="markers-conflict: %s" % _log_over_words())
            return
        rc = done["rc"]
        if rc is not None and (rc < 0 or rc >= 128):
            out.update(verdict=vc.FAIL, code="GATE-MARKERS",
                       detail="killed by signal: the run ended with rc %s, so its TEST report "
                              "is not trusted" % rc)
            return
        try:
            report = markers.parse_tests(markers._with_token(_log_text(done["log"]),
                                                             done["token"]))
        except MarkerError as exc:
            out.update(verdict=vc.FAIL, code="GATE-MARKERS", detail="%s: %s" % _marker_words(exc))
            return
        out["rows"][-1]["tests"] = dict(report.tests)
        reports.append(report.tests)
    out["tests"] = _fold(reports, prefer)
    changed = _frozen_changed(cx, before)
    if changed:
        out.update(verdict=vc.BLOCKED, detail=changed)


def _fold(reports: list, prefer: str) -> dict:
    names = sorted({n for rep in reports for n in rep})
    out = {}
    for name in names:
        seen = [rep.get(name) for rep in reports]
        if all(s == prefer for s in seen):
            out[name] = prefer
            continue
        for word in (("PASS", "SKIP") if prefer == "FAIL" else ("FAIL", "SKIP")):
            if word in seen:
                out[name] = word
                break
        else:
            out[name] = "SKIP"
    return out


def _record_side(cx, side_run, *, verdict, detail=None, extra=None) -> GateResult:
    """Record one side of a test item: a new run, or a reused one."""
    item, side, fp = side_run["item"], side_run["side"], side_run["fp"]
    valid = side_run["verdict"] == vc.PASS
    report = TestReport(tests=dict(side_run["tests"]), begin=True, end=True) if valid else None
    tests = dict(side_run["tests"]) if valid else None
    if side_run["reused"] is not None:
        prev_id, prev = side_run["reused"]
        more = dict(extra or {})
        more.update({k: prev.get(k) for k in _TREE_KEYS})
        more.update(reused_from=prev_id, ran=False)
        eid = _record(cx, item=item, side=side, verdict=verdict, detail=detail,
                      command=prev.get("command"), rc=prev.get("rc"),
                      timed_out=bool(prev.get("timed_out")), log=_abs(cx.root, prev.get("log")),
                      log_sha256=prev.get("log_sha256"), fp=fp, offline=prev.get("offline"),
                      reused=True, repeats=prev.get("repeats"), tests=tests, extra=more)
        return GateResult(stage=item, side=side, verdict=verdict, rc=prev.get("rc"),
                          timed_out=bool(prev.get("timed_out")),
                          log=_abs(cx.root, prev.get("log")) or "", input_sha256=fp,
                          offline=prev.get("offline"), reused=True, tests=report, event_id=eid)
    last = side_run["last"] or {}
    rows = side_run["rows"]
    more = dict(extra or {})
    more["ran"] = _ran(rows)
    more.update(tree_sha256=side_run["tree"], tree_after_sha256=side_run["tree_after"])
    if side_run.get("tag"):
        more["tag"] = side_run["tag"]
    code = side_run["code"] if verdict == side_run["verdict"] else None
    eid = _record(cx, item=item, side=side, verdict=verdict, code=code,
                  detail=detail if detail is not None else side_run["detail"],
                  command=last.get("command"), rc=last.get("rc"),
                  timed_out=bool(last.get("timed_out")), log=last.get("log"), fp=fp,
                  offline=_offline(rows), repeats=rows, tests=tests, extra=more)
    return GateResult(stage=item, side=side, verdict=verdict, rc=last.get("rc"),
                      timed_out=bool(last.get("timed_out")), log=last.get("log") or "",
                      input_sha256=fp, offline=_offline(rows), reused=False, tests=report,
                      event_id=eid)


# ------------------------------------------------------------------- one jailed command
def _run_command(cx, *, stage_dir, wt, tag, repeat, stage, command, workdir, env_map, timeout,
                 knobs=None, seed=None, reproducer=None) -> dict:
    """Substitute and run one command with `sh -c` in a gate jail on the tree wt, then copy its
    evidence into judged/<tag>-<repeat>/ (tag is the run's <side>-<k>). A dict: command, rc,
    timed_out, log, started, blocked, code, detail, judged, evidence (relative path -> sha256),
    cwd, values, token (this run's results token, never recorded) and log_over (the log passed
    _MAX_LOG_BYTES)."""
    knobs = dict(_knob_values(cx.contract) if knobs is None else knobs)
    studio = _studio_pack(cx.contract)
    # a studio pack is not in the tree: commands start at the tree root, and RUNBOOK_DIR is the frozen copy
    tree_rb = os.path.normpath(wt.path if studio else os.path.join(wt.path, _runbook_folder(cx.contract)))
    evidence = os.path.join(stage_dir, "evidence")
    values = dict(knobs)
    values.update(RUNBOOK_DIR=_frozen_runbook_dir(cx) if studio else tree_rb, STAGE=stage, ATTEMPT=repeat,
                  EVIDENCE_DIR=evidence, WORKTREE=wt.path, CONTRACT_DIR=_files_dir(cx), REPEAT=repeat)
    extra = {"WORKTREE": wt.path, "CONTRACT_DIR": _files_dir(cx), "EVIDENCE_DIR": evidence,
             "REPEAT": str(repeat)}
    if seed is not None:
        values["SEED"] = seed
        extra["SEED"] = str(seed)
    if reproducer is not None:
        values["REPRODUCER_DIR"] = reproducer
        extra["REPRODUCER_DIR"] = reproducer
    text = runbook.substitute(str(command or ""), values)
    token = secrets.token_hex(16)
    out = {"command": text, "rc": None, "timed_out": False, "log": None, "started": False,
           "blocked": False, "code": None, "detail": None, "judged": None, "evidence": None,
           "evidence_dir": evidence, "cwd": tree_rb, "values": values, "offline": None,
           "token": token, "log_over": False, "ptrace_scope": None, "stopped": False}
    cwd = tree_rb
    if workdir:
        cwd = os.path.normpath(os.path.join(tree_rb, runbook.substitute(str(workdir), values)))
    if os.path.commonpath([cwd, os.path.abspath(wt.path)]) != os.path.abspath(wt.path):
        out.update(blocked=True, detail="workdir %s is outside the tree" % workdir)
        return out
    out["cwd"] = cwd
    extra["PWD"] = cwd
    for name, value in (env_map or {}).items():
        extra[str(name)] = runbook.substitute(str(value), values)
    done = _jailed(cx, wt_path=wt.path, cwd=cwd, stage_dir=stage_dir, tag=tag, repeat=repeat,
                   argv=["/bin/sh", "-c", text], extra=extra, timeout=timeout,
                   evidence=evidence, evidence_rw=True, reproducer=reproducer, token=token)
    out.update(rc=done["rc"], timed_out=done["timed_out"], log=done["log"],
               started=done["started"], offline=done["offline"], log_over=done["log_over"],
               ptrace_scope=done["ptrace_scope"], stopped=bool(done.get("stopped")))
    try:
        if done.get("stopped"):
            # killed by the runner's stop signal: no result, and no judged copy of a cut run
            out.update(blocked=True, detail="the run was stopped by the runner's stop signal")
            return out
        if not done["started"]:
            out.update(blocked=True, detail=done["detail"])
            return out
        judged = os.path.join(stage_dir, "judged", "%s-%d" % (tag, repeat))
        try:
            out["evidence"] = judged_copy(evidence, judged)
            out["judged"] = judged
        except GateInfraError as exc:
            out.update(blocked=True, code="GATE-EVIDENCE-LINK",
                       detail=exc.args[1] if len(exc.args) > 1 else str(exc))
        except ValueError as exc:            # never overwrite another run's judged copy
            out.update(blocked=True, detail=str(exc))
        return out
    finally:
        failed = _cleanup(paths.stage_home(stage_dir, tag, repeat))
        if failed:
            out["cleanup"] = failed


def _jailed(cx, *, wt_path, cwd, stage_dir, tag, repeat, argv, extra, timeout, evidence,
            evidence_rw, reproducer=None, plugin_env=None, token=None) -> dict:
    """Run argv in a fresh gate jail. A dict: started, offline, rc, timed_out, log, log_over,
    detail.

    The jail binds the tree and the evidence folder (read-only for a plugin check) and a fresh
    home read-write, and the frozen contract files, the tool files, the read paths and, for a
    fail-to-pass run, the reproducer read-only; every frozen file is overlaid read-only on its
    path in the tree, every folder between the tree root and an overlaid file is bound onto
    itself (a mount point cannot be renamed or removed), and the jail's root is remounted
    read-only last. Before a jail that binds read paths, contract.check_read_paths runs, and a
    finding (or a check that cannot run) refuses the jail; a socket, FIFO or device in the tree
    refuses it too (_tree_specials). With a token, the command's file descriptor 3 reads the
    token and a newline from a pipe the host filled and closed, and stdin stays /dev/null. The
    log is <tag>-<repeat>.log and an existing one is never overwritten. The jail runs under
    _RELAY, so its stdout and stderr are a socket and only the relay, outside the jail, writes the
    log; the relay kills the jail once the log passes _MAX_LOG_BYTES, and log_over is then true.
    The first line of the log is the jail's start line; without it the jail did not start.
    `offline` is true when the jail's argv holds --unshare-net and the jail started."""
    out = {"started": False, "offline": None, "rc": None, "timed_out": False, "log": None,
           "log_over": False, "detail": None, "ptrace_scope": None, "stopped": False}
    ok, why = jail.available()
    if not ok:
        out["detail"] = "bubblewrap is not available, so no gate runs: %s" % (why,)
        return out
    refused = _read_path_findings(cx.contract)
    if refused:
        out["detail"] = "read path refused: %s" % "; ".join(refused)
        return out
    special = _tree_specials(wt_path)
    if special:
        out["detail"] = "tree refused: %s" % "; ".join(special)
        return out
    home = paths.stage_home(stage_dir, tag, repeat)
    log = os.path.join(stage_dir, "%s-%d.log" % (tag, repeat))
    try:
        os.makedirs(stage_dir, exist_ok=True)
        if os.path.lexists(log):
            out["detail"] = "the gate log %s already exists and is never overwritten" % log
            return out
        if evidence_rw:
            _fresh_dir(evidence)
        elif not os.path.isdir(evidence):
            _fresh_dir(evidence)
        _fresh_dir(home)
    except OSError as exc:
        out["detail"] = "the gate jail could not be set up: %s" % (exc.strerror or exc)
        return out
    try:
        overlays = _overlays(cx, wt_path)
    except _Refused as exc:
        out["detail"] = str(exc)
        return out
    ro, rw = [], [wt_path] + _overlay_parents(wt_path, overlays) + [home]
    files = _files_dir(cx)
    if os.path.isdir(files):
        ro.append(files)
    for tool in cx.contract.tools_real:
        real = str(tool)
        if real == "/usr" or real.startswith("/usr/") or not os.path.exists(real):
            continue
        ro.append(real)
    for path in cx.contract.read_real:
        if os.path.exists(str(path)):
            ro.append(str(path))
    if reproducer is not None:
        ro.append(reproducer)
    (rw if evidence_rw else ro).append(evidence)
    spec = JailSpec(kind=cx.kind, cwd=cwd, ro=tuple(ro), rw=tuple(rw), overlays=tuple(overlays))
    try:
        env = procs.gate_environment(extra, home=home, tools_real=tuple(cx.contract.tools_real))
        prefix = jail.argv(spec)
    except ValueError as exc:
        out["detail"] = "the gate jail was refused: %s" % exc
        return out
    env.update(plugin_env or {})
    prefix = _read_only_parents(prefix, spec, cwd)
    wrapper = 'echo %s; exec "$@"' % _JAIL_MARK
    if token is not None:
        env["ALPACA_RESULTS_FD"] = str(_RESULTS_FD)
        wrapper = 'echo %s; exec %d<&0 0</dev/null; exec "$@"' % (_JAIL_MARK, _RESULTS_FD)
    full = prefix + ["--", "/bin/sh", "-c", wrapper, "sh"] + list(argv)
    # the jail's stdout and stderr are one end of a socketpair; the relay, a host process outside
    # the jail's pid namespace, copies the other end into the log (see _RELAY)
    relayed = [_PYTHON, "-I", "-S", "-c", _RELAY, str(_MAX_LOG_BYTES)] + full
    registry = paths.pids_dir(cx.root, cx.run)
    pipe_r = None
    try:
        stdin = None
        if token is not None:
            pipe_r, pipe_w = os.pipe()
            try:
                os.write(pipe_w, (token + "\n").encode("ascii"))
            finally:
                os.close(pipe_w)
            # the relay's stdin opens the pipe's read end again, bwrap inherits it, and the
            # wrapper moves it to fd 3
            stdin = "/proc/self/fd/%d" % pipe_r
        result = procs.run(relayed, cwd=stage_dir, env=env, timeout_s=float(timeout), log=log,
                           stdin=stdin, tag="gate-%s" % secrets.token_hex(8),
                           registry=registry)
    except OSError as exc:
        out["detail"] = "the gate jail could not be started: %s" % (exc.strerror or exc)
        return out
    finally:
        if pipe_r is not None:
            os.close(pipe_r)
    if getattr(result, "stopped", False):
        cx.stopped = True
        out["stopped"] = True
    head = _log_head(log)
    first = head.split("\n", 1)[0].strip() if head else ""
    out.update(log=log, rc=result.rc, timed_out=bool(result.timed_out),
               log_over=_log_size(log) > _MAX_LOG_BYTES)
    if first == _JAIL_MARK:
        out.update(started=True, offline="--unshare-net" in prefix, ptrace_scope=_ptrace_scope())
    elif result.timed_out:
        out["detail"] = "the gate jail did not start before its timeout"
    else:
        out["detail"] = ("the log does not start with the gate jail's start line, so the jail "
                         "did not start: %s" % (head.strip()[:_PROBE_OUT] or "no output"))
    return out


def _read_only_parents(prefix: list, spec, cwd: str) -> list:
    """prefix (jail.argv(spec)) with the parent folders of every bound host path made read-only.
    bwrap makes the folders above a bound path in the jail's own root, which the command could
    rename (with the bound path inside) and fill with its own files; `--remount-ro /` after every
    mount stops that. A bound path under /tmp has its parents in the jail's /tmp tmpfs instead,
    which stays writable for TMPDIR: each first folder under /tmp that holds a bound path (and is
    not itself bound) gets a tmpfs of its own before the binds, remounted read-only last. The
    tmpfs options go right after jail.argv's fixed part, the longest common prefix of prefix
    and the argv of a spec with nothing bound."""
    bound = [os.path.normpath(str(p)) for p in tuple(spec.ro) + tuple(spec.rw)]
    bound += [os.path.normpath(str(dest)) for _src, dest in spec.overlays]
    exact = set(bound)
    tops = set()
    for path in bound:
        if path.startswith("/tmp/"):
            first = "/tmp/" + path[len("/tmp/"):].split("/")[0]
            if first not in exact:
                tops.add(first)
    if not tops:
        return list(prefix) + ["--remount-ro", "/"]
    empty = jail.argv(JailSpec(kind=spec.kind, cwd=cwd))
    at = 0
    while at < min(len(prefix), len(empty)) and prefix[at] == empty[at]:
        at += 1
    before = [opt for top in sorted(tops) for opt in ("--tmpfs", top)]
    after = [opt for top in sorted(tops) for opt in ("--remount-ro", top)]
    return list(prefix[:at]) + before + list(prefix[at:]) + after + ["--remount-ro", "/"]


def _overlay_parents(tree: str, overlays: list) -> list:
    """Every folder strictly between the tree root and an overlaid file, parents first, each
    once. The gate jail binds each onto itself: a mount point cannot be renamed or removed, so
    no command can move a frozen file's folder aside and put its own file at the frozen path."""
    top = os.path.abspath(tree)
    found = set()
    for _src, dest in overlays:
        here = os.path.dirname(os.path.abspath(dest))
        while here != top and os.path.commonpath([here, top]) == top:
            found.add(here)
            here = os.path.dirname(here)
    return sorted(found, key=lambda p: (p.count(os.sep), p))


def _tree_specials(top: str) -> list:
    """The sockets, FIFOs and devices in the tree a gate jail binds, as "<relative path>: socket"
    (or "FIFO", "device"), sorted, the first _SHOWN of them and a count of the rest. The gate jail
    runs in its own empty network namespace, but it shares the host's file system, so a socket
    file in the tree could reach a host listener; a tree that holds one is refused before the jail
    starts, as a read path finding is. A folder the host cannot list ("<path>: folder cannot be
    listed") or an entry it cannot stat ("<path>: entry cannot be read") is refused the same way:
    the jail runs as the tree's owner, so it can chmod such a folder open and reach a socket the
    host never saw. A missing folder or entry (the tree itself, or one removed while the walk
    runs) is skipped: there is nothing to reach. Links are not followed."""
    found = []
    stack = [top]
    while stack:
        here = stack.pop()
        try:
            with os.scandir(here) as entries:
                listed = list(entries)
        except FileNotFoundError:
            continue
        except OSError:
            found.append("%s: folder cannot be listed" % _printable(os.path.relpath(here, top)))
            continue
        for entry in listed:
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            except OSError:
                found.append("%s: entry cannot be read"
                             % _printable(os.path.relpath(entry.path, top)))
                continue
            if stat.S_ISDIR(mode):
                stack.append(entry.path)
                continue
            kind = ("socket" if stat.S_ISSOCK(mode) else "FIFO" if stat.S_ISFIFO(mode)
                    else "device" if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) else None)
            if kind:
                found.append("%s: %s" % (_printable(os.path.relpath(entry.path, top)), kind))
    found.sort()
    if len(found) > _SHOWN:
        found = found[:_SHOWN] + ["and %d more" % (len(found) - _SHOWN)]
    return found


def _read_path_findings(contract) -> list:
    """contract.check_read_paths over contract.read_real (architecture.md section 3.5): [] when
    the contract has no read path or every one is clean. The check fails closed: when it cannot
    run (t-096 has not built it yet, or it raised), that is the one finding."""
    if not contract.read_real:
        return []
    try:
        findings = rcontract.check_read_paths(contract)
    except NotImplementedError:
        return ["contract.check_read_paths is not built yet (t-096), so no read path is checked "
                "and none is bound"]
    except Exception as exc:          # the check raises nothing by design; any raise refuses
        return ["contract.check_read_paths failed: %s" % _printable(str(exc))]
    return [_printable(str(f)) for f in (findings or [])]


# ------------------------------------------------------------------------- the record
class _Cx:
    """What one gate call works with."""

    def __init__(self, conn, root, run, task, contract, session, gates_root=None,
                 kind="gate", stage_dir=None):
        self.conn, self.root, self.run, self.task = conn, os.path.abspath(root), run, task
        self.contract, self.session = contract, session
        # the folder that holds gates/<nn>-<slug>/: the attempt's, or a verifier try's own
        self.gates_root = os.path.abspath(gates_root) if gates_root else None
        # "holdout" for a holdout check (run_holdout_stage): its jail is of kind holdout, its
        # stage folder is `stage_dir` (paths.holdout_dir), and the frozen held-out files of
        # contract/holdout/ are overlaid on its tree with the frozen copies of contract/files/
        self.kind = kind
        self.stage_dir = os.path.abspath(stage_dir) if stage_dir else None
        # set once the runner's stop signal killed a jail of this call (procs.stop_all): every
        # later record of the call is BLOCKED with `stopped` true, so no verdict of a killed
        # gate is recorded or reused (EC-016)
        self.stopped = False
        paths.check_name(run)
        paths.check_name(task)
        self._op = False

    @property
    def op(self):
        if self._op is False:
            try:
                if self.kind == "holdout":     # the task is the run id: the op is the run's
                    row = self.conn.execute("SELECT op FROM events WHERE kind=? AND ref=? "
                                            "ORDER BY id LIMIT 1",
                                            (events.RUN_START, self.run)).fetchone()
                else:
                    row = self.conn.execute("SELECT op FROM tasks WHERE id=?",
                                            (self.task,)).fetchone()
                self._op = row["op"] if row else None
            except Exception:
                self._op = None
        return self._op


_HASH_LOG = object()


def _record(cx, *, item, side, verdict, code=None, detail=None, command=None, rc=None,
            timed_out=False, log=None, log_sha256=_HASH_LOG, fp="", offline=None,
            lock_wait_s=0.0, reused=False, repeats=(), tests=None, refused=None,
            extra=None) -> int:
    """Emit one research-gate event. log_sha256 is the log's hash now, or, for a reused result,
    the hash the reused event recorded (its log is never re-hashed)."""
    if log_sha256 is _HASH_LOG:
        log_sha256 = _sha256_file(log)
    data = {"stage": item, "side": side, "command": command, "rc": rc,
            "timed_out": bool(timed_out), "log": _rel(cx.root, log) if log else None,
            "log_sha256": log_sha256, "input_sha256": fp, "offline": offline,
            "lock_wait_s": float(lock_wait_s), "reused": bool(reused),
            "repeats": list(repeats or ()), "tests": tests, "refused": refused,
            "reason": detail, "ptrace_scope": _ptrace(repeats)}
    data.update(extra or {})
    if getattr(cx, "stopped", False):
        # the runner's stop signal killed a jail of this item: the result is no verdict, only
        # the fact that the item was stopped; it is never reused (_find_reusable) and an attempt
        # verdict never reads it (state.attempt_verdict)
        verdict, code = vc.BLOCKED, None
        data["stopped"] = True
        data["reason"] = "stopped by the runner's stop signal (procs.stop_all)%s" % (
            ": %s" % detail if detail else "")
    event = events.emit(cx.conn, events.GATE, run=cx.run, ref=cx.task, op=cx.op,
                        session=cx.session, code=verdict, reason=code, data=data)
    return int(event["id"])


def _record_blocked(cx, *, item, side, detail, code=None, fp="", lock_wait_s=0.0) -> GateResult:
    eid = _record(cx, item=item, side=side, verdict=vc.BLOCKED, code=code, detail=detail, fp=fp,
                  lock_wait_s=lock_wait_s, extra={"ran": False})
    return GateResult(stage=item, side=side, verdict=vc.BLOCKED, rc=None, timed_out=False,
                      log="", input_sha256=fp, offline=None, lock_wait_s=float(lock_wait_s),
                      event_id=eid)


def _event_data(cx, event_id) -> dict:
    row = cx.conn.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()
    try:
        return json.loads(row["data"] or "{}") if row else {}
    except ValueError:
        return {}


def _find_reusable(cx, item, side, fp):
    """(event id, data) of the newest research-gate of this run, task, stage and side whose
    input_sha256 is fp, whose verdict is PASS or FAIL and whose run left its tree as it found it
    (tree_sha256 equals tree_after_sha256); else None. A side in _NEVER_REUSED gets None."""
    if side in _NEVER_REUSED:
        return None
    rows = cx.conn.execute("SELECT id, data FROM events WHERE kind=? AND ref=? ORDER BY id DESC",
                           (events.GATE, cx.task)).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            continue
        if (data.get("run") == cx.run and data.get("stage") == item and data.get("side") == side
                and data.get("input_sha256") == fp and data.get("verdict") in _REUSABLE
                and not data.get("refused") and not data.get("stopped")
                and data.get("tree_sha256")
                and data.get("tree_sha256") == data.get("tree_after_sha256")):
            return int(row["id"]), data
    return None


def _record_reused(cx, prev, *, item, side, fp) -> GateResult:
    prev_id, data = prev
    code = vc.PASS if data.get("verdict") == "PASS" else vc.FAIL
    keep = {k: data.get(k) for k in ("metric", "median", "evidence", "judged", "attempts", "tag")
            + _TREE_KEYS if k in data}
    keep.update(reused_from=prev_id, ran=False)
    eid = _record(cx, item=item, side=side, verdict=code, code=data.get("code"),
                  detail=data.get("reason"), command=data.get("command"), rc=data.get("rc"),
                  timed_out=bool(data.get("timed_out")), log=_abs(cx.root, data.get("log")),
                  log_sha256=data.get("log_sha256"), fp=fp, offline=data.get("offline"),
                  reused=True, repeats=data.get("repeats"), tests=data.get("tests"), extra=keep)
    tests = data.get("tests")
    return GateResult(stage=item, side=side, verdict=code, rc=data.get("rc"),
                      timed_out=bool(data.get("timed_out")),
                      log=_abs(cx.root, data.get("log")) or "", input_sha256=fp,
                      offline=data.get("offline"), lock_wait_s=0.0, reused=True,
                      tests=(TestReport(tests=dict(tests), begin=True, end=True)
                             if isinstance(tests, dict) else None), event_id=eid)


def _task_case(cx):
    """The repair case whose fail-to-pass this task ran on the base or attempt side, else None."""
    rows = cx.conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id",
                           (events.GATE, cx.task)).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            continue
        stage = data.get("stage")
        if (data.get("run") == cx.run and isinstance(stage, str) and stage.startswith(_FTP)
                and data.get("side") in ("base", "attempt")):
            return stage[len(_FTP):]
    return None


# ------------------------------------------------------------------ reproducer and case
def _frozen_reproducer(cx, case) -> str:
    if case.fail_to_pass.origin == "store":
        return paths.hidden_dir(cx.root, cx.run, case.case_id)
    return paths.reproducer_dir(cx.root, cx.run, case.case_id)


def _reproducer_check(cx, case, rep_dir):
    """(the TEST names frozen with the proof, problem): the newest research-reproducer event of
    this run and case must exist and say proved true, and every file of rep_dir must hash to it
    (paths relative to the reproducer folder). The event is matched by run and case: this module
    is not told the repair task, and a case belongs to one repair task of its run."""
    rows = cx.conn.execute("SELECT data FROM events WHERE kind=? ORDER BY id DESC",
                           (events.REPRODUCER,)).fetchall()
    found = None
    for row in rows:
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            continue
        if data.get("run") == cx.run and data.get("case") == case.case_id:
            found = data
            break
    if found is None:
        return (), "no research-reproducer event for case %s" % case.case_id
    if found.get("proved") is not True:
        return (), ("the newest research-reproducer event of case %s does not say proved"
                    % case.case_id)
    if not os.path.isdir(rep_dir) or os.path.islink(rep_dir):
        return (), "the frozen reproducer folder of case %s is missing" % case.case_id
    want = _named_hashes(found.get("files"), rep_dir, cx.root)
    have, odd = {}, []
    for dirpath, dirnames, filenames in os.walk(rep_dir):
        for name in sorted(dirnames + filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, rep_dir).replace(os.sep, "/")
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                odd.append(rel)
                continue
            have[rel] = _sha256_file(full)
    if odd or want != have:
        changed = sorted(set(want) ^ set(have) | {k for k in want if have.get(k) != want[k]}
                         | set(odd))
        return (), ("the reproducer of case %s no longer hashes to its research-reproducer "
                    "event: %s" % (case.case_id, ", ".join(changed[:10])))
    tests = found.get("tests") or ()
    return tuple(str(t) for t in tests), None


def _named_hashes(files, rep_dir, root) -> dict:
    """The research-reproducer `files` value as {path relative to rep_dir: sha256}; it may be a
    mapping or a list of {path, sha256}, each path relative to the folder, to the root, or
    absolute."""
    pairs = []
    if isinstance(files, dict):
        pairs = list(files.items())
    elif isinstance(files, list):
        pairs = [(f.get("path"), f.get("sha256")) for f in files if isinstance(f, dict)]
    out = {}
    folder_rel = os.path.relpath(rep_dir, root).replace(os.sep, "/")
    for path, digest in pairs:
        if not isinstance(path, str):
            continue
        rel = path
        if os.path.isabs(rel):
            rel = os.path.relpath(rel, rep_dir)
        rel = rel.replace(os.sep, "/")
        if rel.startswith(folder_rel + "/"):
            rel = rel[len(folder_rel) + 1:]
        out[rel] = digest
    return out


def _case(contract, case_id):
    for case in contract.repair:
        if case.case_id == case_id:
            return case
    raise ValueError("the contract declares no repair case %r" % (case_id,))


def _metric(contract, metric_id):
    for metric in contract.metrics:
        if metric.id == metric_id:
            return metric
    raise ValueError("the contract declares no metric %r" % (metric_id,))


def _position(contract, item) -> int:
    """The 1-based place of an item in the chain it belongs to (0 for the prepare stage)."""
    case = None
    if item.startswith(_FTP):
        case = item[len(_FTP):]
    elif item == _PTP and contract.repair:
        case = contract.repair[0].case_id
    if case is not None:
        full = chain(contract, repair_case=case)
        return full.index(item) + 1
    full = chain(contract)
    return full.index(item) + 1 if item in full else len(full) + 1


# ------------------------------------------------------------------ contract helpers
def _stage_map(contract, stage_id):
    for stage in contract.stages:
        if isinstance(stage, dict) and stage.get("id") == stage_id:
            return stage
    return None


def _gate_stage(contract, stage_id):
    for gate in contract.gates:
        if gate.stage == stage_id:
            return gate
    return None


def _knob_values(contract) -> dict:
    out = {}
    for name, value in (contract.knobs or {}).items():
        out[name] = value.get("default") if isinstance(value, dict) else value
    return out


def _runbook_data(contract) -> dict:
    knobs = []
    for name, value in (contract.knobs or {}).items():
        spec = dict(value) if isinstance(value, dict) else {"default": value}
        spec["id"] = name
        knobs.append(spec)
    return {"knobs": knobs, "stages": [s for s in contract.stages if isinstance(s, dict)]}


def _studio_pack(contract) -> bool:
    """True for a pack frozen from an attached project's studio (op-062 t-653): it is not in the run tree.

    The contract object records `pack_source: studio`, but the decision is read from the two paths a frozen
    contract always holds, so no field of `types.Contract` carries it: `repo.path` is a folder below the project
    and the runbook (a project-relative path) is not under it. A freeze accepts a pack only inside the
    repository or, in an attached project, in the studio, so a runbook outside a relative `repo.path` can be
    nothing else. (Before t-653 that case was refused at the freeze and never reached a run.)"""
    repo = os.path.normpath(str(getattr(contract, "repo_path", None) or "."))
    if os.path.isabs(repo) or repo == ".":
        return False
    rb = os.path.normpath(str(getattr(contract, "runbook", None) or ""))
    return not (rb == repo or rb.startswith(repo + os.sep))


def _studio_folder(contract):
    """`@studio/<the runbook's folder in the studio>` for a studio pack: the name its frozen copy has under
    contract/files/, and a place that is not in any run tree. None for any other pack. ValueError when the
    runbook path is not inside the project."""
    if not _studio_pack(contract):
        return None
    folder = os.path.dirname(os.path.normpath(str(contract.runbook or "").replace("\\", "/")))
    if os.path.isabs(folder) or folder in ("", ".", "..") or folder.startswith("../"):
        raise ValueError("the studio pack folder %r is not inside the project" % folder)
    return "@studio/" + folder


def _runbook_folder(contract) -> str:
    """The runbook's folder relative to the root of the repository the run works on (for a studio pack, its
    `_studio_folder`)."""
    studio = _studio_folder(contract)
    if studio is not None:
        return studio
    rb = str(contract.runbook or "").replace("\\", "/")
    repo = str(contract.repo_path or ".").replace("\\", "/").strip("/")
    if repo not in ("", ".") and rb.startswith(repo + "/"):
        rb = rb[len(repo) + 1:]
    folder = os.path.dirname(os.path.normpath(rb)) if rb else ""
    if os.path.isabs(folder) or folder == ".." or folder.startswith("../"):
        raise ValueError("the runbook folder %r is not inside the repository" % folder)
    return "" if folder in (".", "") else folder


def _files_dir(cx) -> str:
    return os.path.join(paths.contract_dir(cx.root, cx.run), "files")


def _frozen_runbook_dir(cx) -> str:
    return os.path.normpath(os.path.join(_files_dir(cx), _runbook_folder(cx.contract)))


def _stage_dir(cx, nn, item) -> str:
    """<gates root>/<nn>-<slug>: the attempt's attempts/<task>/gates/ (sides attempt, base and
    prepare), or for a verifier's re-run the gates/ folder of its own try folder, so the two
    verifier slots of a round never share a stage folder."""
    if cx.stage_dir:                       # a holdout check's folder, paths.holdout_dir
        return cx.stage_dir
    root = cx.gates_root or os.path.join(paths.attempt_dir(cx.root, cx.run, cx.task), "gates")
    return os.path.join(root, "%02d-%s" % (int(nn), paths.stage_slug(item)))


def _claim_tag(stage_dir, side) -> str:
    """The run tag <side>-<k> of one run of an item: the smallest k whose folder
    <stage_dir>/runs/<side>-<k> this call makes (mkdir is atomic, so two runs never share k).
    Every log, judged copy and home of the run is named after the tag."""
    runs = os.path.join(stage_dir, "runs")
    os.makedirs(runs, exist_ok=True)
    k = 1
    while True:
        tag = "%s-%d" % (side, k)
        try:
            os.mkdir(os.path.join(runs, tag), 0o700)
            return tag
        except FileExistsError:
            k += 1


class _Refused(Exception):
    """A frozen overlay whose place in the tree is reached through a link or cannot be read."""


def _overlays(cx, tree) -> list:
    """(frozen copy, its path in the tree) for every regular file under contract/files/ whose
    path exists in the tree. An absent path is not overlaid: bwrap would make its mount point in
    the tree on the host, and a deleted protected file is the fingerprint and purge steps' finding
    (architecture.md section 9.3). A path reached through a link, one that is not a regular
    file, or one the host cannot stat (a folder above it made unsearchable) refuses the jail."""
    out = _overlay_pairs(_files_dir(cx), tree)
    if getattr(cx, "kind", "gate") == "holdout":
        # a holdout check's tree also holds the frozen held-out files, read-only at their paths
        out += _overlay_pairs(_holdout_files_dir(cx), tree)
    return out


def _holdout_files_dir(cx) -> str:
    return os.path.join(paths.contract_dir(cx.root, cx.run), "holdout")


def _overlay_pairs(base, tree) -> list:
    out = []
    if not os.path.isdir(base):
        return out
    tree = os.path.abspath(tree)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(filenames):
            src = os.path.join(dirpath, name)
            if not stat.S_ISREG(os.lstat(src).st_mode):
                continue
            rel = os.path.relpath(src, base)
            cur = tree
            absent = False
            for part in rel.split(os.sep):
                cur = os.path.join(cur, part)
                try:
                    st = os.lstat(cur)
                except FileNotFoundError:
                    absent = True
                    break
                except OSError as exc:        # a folder the tested code made unsearchable
                    raise _Refused("the frozen file %s cannot be overlaid: %s cannot be read "
                                   "(%s)" % (rel, os.path.relpath(cur, tree),
                                             exc.strerror or exc)) from None
                if stat.S_ISLNK(st.st_mode):
                    raise _Refused("the frozen file %s cannot be overlaid: %s is a symlink in "
                                   "the tree" % (rel, os.path.relpath(cur, tree)))
                if cur != os.path.join(tree, rel) and not stat.S_ISDIR(st.st_mode):
                    raise _Refused("the frozen file %s cannot be overlaid: %s is not a folder "
                                   "in the tree" % (rel, os.path.relpath(cur, tree)))
                if cur == os.path.join(tree, rel) and not stat.S_ISREG(st.st_mode):
                    raise _Refused("the frozen file %s cannot be overlaid: its place in the "
                                   "tree is not a regular file" % rel)
            if not absent:
                out.append((src, os.path.join(tree, rel)))
    return out


# ----------------------------------------------------------------------- digests
def _sha256_file(path, limit=None) -> str | None:
    """The sha256 of a file's bytes, or None when it cannot be read or, with a limit, when it
    holds more than limit bytes: the size is read with fstat before any byte, and the read stops
    past the limit too, so a sparse or growing file never costs more than limit bytes."""
    if not path:
        return None
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            if limit is not None and os.fstat(fh.fileno()).st_size > limit:
                return None
            done = 0
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                done += len(chunk)
                if limit is not None and done > limit:
                    return None
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _dir_digest(path, *, bounded=True) -> str:
    """A digest of a folder's content: every folder's and entry's relative path, type,
    permission bits and bytes (a link's target), and the extended attributes (names and values)
    of every folder and file, links never followed, a top-level .git included. Each entry is one
    JSON array line, so a name that holds a newline or a space cannot spell another entry's line
    and two different trees never give the same lines. An entry it cannot read (a folder it
    cannot list, an entry it cannot stat, a file it cannot open or read, a link it cannot read,
    an xattr it cannot read) mixes a fresh random value into the digest, so that digest never
    equals an earlier one and nothing is reused on it.
    With bounded (the default), it reads at most _MAX_DIGEST_BYTES bytes of file content in all
    and walks at most _MAX_DIGEST_ENTRIES entries: a file whose st_size would pass the byte
    bound is a gap without a read, and the walk stops with a gap past the entry bound. The host
    takes this digest outside every timeout, so a tree that tested code filled with a sparse
    file of a petabyte, or with millions of entries, costs a bounded time and is never reused."""
    digest = hashlib.sha256()
    if not path or not os.path.isdir(path):
        digest.update(b"absent")
        return digest.hexdigest()
    top = os.path.abspath(path)
    gaps = []
    left = [_MAX_DIGEST_BYTES if bounded else None, _MAX_DIGEST_ENTRIES if bounded else None]

    def _gap(what):
        gaps.append(what)
        digest.update(b"unreadable " + os.urandom(32))

    def _count() -> bool:
        """False once the walk has passed its entry bound (after mixing in a gap)."""
        if left[1] is None:
            return True
        left[1] -= 1
        if left[1] < 0:
            _gap("more than %d entries" % _MAX_DIGEST_ENTRIES)
            return False
        return True

    for dirpath, dirnames, filenames in os.walk(top, onerror=lambda exc: _gap(exc)):
        if not _count():
            break
        dirnames.sort()
        entries = sorted(filenames + [d for d in dirnames
                                      if os.path.islink(os.path.join(dirpath, d))])
        rel_dir = os.path.relpath(dirpath, top)
        try:
            digest.update(_entry_line("D", rel_dir, stat.S_IMODE(os.lstat(dirpath).st_mode),
                                      _xattrs(dirpath)))
        except OSError:
            _gap(rel_dir)
        stop = False
        for name in entries:
            if not _count():
                stop = True
                break
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, top)
            try:
                st = os.lstat(full)
                if stat.S_ISLNK(st.st_mode):
                    line = _entry_line("L", rel, os.readlink(full))
                elif stat.S_ISREG(st.st_mode):
                    if left[0] is not None and st.st_size > left[0]:
                        content = None
                    else:
                        content = _sha256_file(full, left[0])
                        if left[0] is not None:
                            left[0] -= st.st_size
                    if content is None:
                        _gap(rel)
                    line = _entry_line("F", rel, stat.S_IMODE(st.st_mode), content,
                                       _xattrs(full))
                else:
                    line = _entry_line("S", rel, stat.S_IFMT(st.st_mode))
            except OSError:
                _gap(rel)
                continue
            digest.update(line)
        if stop:
            break
    return digest.hexdigest()


def _xattrs(path) -> list:
    """[[name, hex value], ...] of path's extended attributes, sorted by name, the link itself
    never followed; [] on a file system that keeps none. Raises OSError when one cannot be
    read (a user xattr of a file the host may not read), which the digest turns into a gap."""
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return []
        raise
    out = []
    for name in sorted(names):
        try:
            value = os.getxattr(path, name, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.ENODATA:        # removed since it was listed
                continue
            raise
        out.append([name, value.hex()])
    return out


def _entry_line(*parts) -> bytes:
    """One digest line: the parts as a JSON array, strings kept with surrogateescape so a name
    that is not UTF-8 still hashes by its own bytes."""
    return (json.dumps(list(parts), ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "ascii")


def _path_digest(path) -> str:
    """_dir_digest of a folder, the bytes of a file (a random value when it cannot be read), a
    link's target, or "absent"."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable " + os.urandom(32).hex()
    if stat.S_ISLNK(st.st_mode):
        try:
            return "L " + os.readlink(path)
        except OSError:
            return "unreadable " + os.urandom(32).hex()
    if stat.S_ISDIR(st.st_mode):
        return "D " + _dir_digest(path)
    if stat.S_ISREG(st.st_mode):
        return "F %s" % (_sha256_file(path, _MAX_DIGEST_BYTES)
                         or "unreadable " + os.urandom(32).hex())
    return "S %o" % stat.S_IFMT(st.st_mode)


def _read_digest(contract) -> str:
    """The content of every read path the gate jail binds (contract.read_real), in sorted order."""
    digest = hashlib.sha256()
    for path in sorted(str(p) for p in contract.read_real):
        digest.update(_entry_line(path, _path_digest(path)))
    return digest.hexdigest()


def _tools_digest(contract) -> str:
    digest = hashlib.sha256()
    for tool in contract.tools_real:
        digest.update(("%s %s\n" % (tool, _sha256_file(str(tool)) or "missing")).encode("utf-8"))
    return digest.hexdigest()


def _contract_digest(contract) -> str:
    """contract.sha256 with the parts the gates read (a frozen contract carries its hash; this
    also covers a contract built without one)."""
    parts = {"sha256": contract.sha256, "stages": list(contract.stages),
             "knobs": contract.knobs, "gates": [list(_astuple(g)) for g in contract.gates],
             "metrics": [list(_astuple(m)) for m in contract.metrics],
             "repair": [repr(r) for r in contract.repair], "p2p": repr(contract.pass_to_pass),
             "runbook": contract.runbook, "repo": contract.repo_path}
    body = json.dumps(parts, sort_keys=True, default=repr)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _astuple(obj) -> tuple:
    return tuple(repr(v) for v in dataclasses.astuple(obj))


def _frozen_state(cx) -> tuple:
    """The frozen contract files and the tool files, digested without the digest bounds: a gap
    here would read as a change and fail every item, and no jail can write either of them."""
    return (_dir_digest(_files_dir(cx), bounded=False), _tools_digest(cx.contract))


def _frozen_changed(cx, before) -> str | None:
    after = _frozen_state(cx)
    if after == before:
        return None
    what = []
    if after[0] != before[0]:
        what.append("the frozen contract files")
    if after[1] != before[1]:
        what.append("the tool files")
    return "the inputs changed while the item ran: %s" % " and ".join(what)


def _fingerprint(cx, tree_path, item, *, extra="", tree=None) -> str:
    """input_fingerprint of one item. The files part holds the frozen files' hash and digest,
    `extra` (the reproducer's digest for a fail-to-pass item) and the read paths' content, which
    the gate jail binds and a stage can read. `tree` is tree_path's _dir_digest when the caller
    has taken it already."""
    files = hashlib.sha256(("%s\n%s\n%s\nread %s" % (
        cx.contract.files_sha256, _dir_digest(_files_dir(cx)), extra,
        _read_digest(cx.contract))).encode("utf-8"))
    return input_fingerprint(tree=_dir_digest(tree_path) if tree is None else tree,
                             contract_sha256=_contract_digest(cx.contract),
                             files_sha256=files.hexdigest(),
                             tools_sha256=_tools_digest(cx.contract), stage=item)


# ------------------------------------------------------------------------ small helpers
def _row(cx, run, *, repeat) -> dict:
    row = {"repeat": repeat, "command": run.get("command"), "rc": run.get("rc"),
           "timed_out": bool(run.get("timed_out")), "log": _rel(cx.root, run.get("log")),
           "log_sha256": _sha256_file(run.get("log")), "offline": run.get("offline"),
           "ptrace_scope": run.get("ptrace_scope"), "ran": bool(run.get("started")),
           "code": run.get("code"), "reason": run.get("detail"), "evidence": run.get("evidence")}
    if run.get("cleanup"):
        row["cleanup"] = run["cleanup"]
    if run.get("stopped"):
        row["stopped"] = True
    return row


def _ptrace_scope() -> int | None:
    """The host's Yama ptrace_scope (0 lets a process read its parent's memory), or None when
    the file is missing or unreadable."""
    try:
        with open(_PTRACE_SCOPE, encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _ptrace(rows) -> int | None:
    """The lowest ptrace_scope over the runs that ran, or None when one ran without a value or
    none ran."""
    ran = [r for r in rows or () if isinstance(r, dict) and r.get("ran")]
    values = [r.get("ptrace_scope") for r in ran]
    if not ran or any(not isinstance(v, int) or isinstance(v, bool) for v in values):
        return None
    return min(values)


def _offline(rows) -> bool | None:
    """True when every run of the item ran in a jail whose argv held --unshare-net; False when
    one ran in a jail without it; None when one did not run at all."""
    if any(r.get("ran") and r.get("offline") is False for r in rows or ()):
        return False
    if rows and all(r.get("ran") and r.get("offline") is True for r in rows):
        return True
    return None


def _ran(rows) -> bool:
    return bool(rows) and any(r.get("ran") for r in rows)


def _marker_words(exc) -> tuple:
    args = list(exc.args) + ["", ""]
    return str(args[0]), str(args[1])


def _log_size(path) -> int:
    try:
        return os.stat(path).st_size if path else 0
    except OSError:
        return 0


def _read_log(path, *, size: int, tail: bool = False) -> tuple:
    """(at most `size` bytes of the log as text, whether the text starts at the log's first
    byte): the head, or with tail the last bytes. The bytes are decoded as UTF-8 with
    replacement, and every "\r" is kept (universal newlines would turn a lone "\r" into a line
    break before markers.py can refuse it)."""
    if not path:
        return "", True
    try:
        with open(path, "rb") as fh:
            start = 0
            if tail:
                end = fh.seek(0, os.SEEK_END)
                start = max(0, end - size)
                fh.seek(start)
            data = fh.read(size)
    except OSError:
        return "", True
    return data.decode("utf-8", errors="replace"), start == 0


def _log_text(path) -> str:
    """The first _MAX_LOG_BYTES of the log, for the marker parsers; a log over the cap is
    refused before it is parsed (log_over), so nothing past the cap is ever needed."""
    return _read_log(path, size=_MAX_LOG_BYTES)[0]


def _log_head(path) -> str:
    """The first _LOG_HEAD bytes of the log: enough for the jail's start line."""
    return _read_log(path, size=_LOG_HEAD)[0]


def _log_tail(path) -> str:
    """The last _LOG_TAIL bytes of a plugin's log, without the jail's start line when the tail
    reaches the start of the log: its last line is the plugin's reason."""
    text, from_start = _read_log(path, size=_LOG_TAIL, tail=True)
    return _strip_mark(text) if from_start else text


def _log_over_words() -> str:
    return "the log passed the cap of %d bytes, so the run was stopped" % _MAX_LOG_BYTES


def _strip_mark(text: str) -> str:
    head, sep, rest = text.partition("\n")
    return rest if head.strip() == _JAIL_MARK else text


def _num(value) -> str:
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _rel(root, path):
    if not path:
        return None
    path = os.path.abspath(path)
    try:
        if os.path.commonpath([path, root]) == root:
            return os.path.relpath(path, root)
    except ValueError:
        pass
    return path


def _abs(root, path):
    if not path:
        return None
    return path if os.path.isabs(path) else os.path.join(root, path)


def _remove(path) -> None:
    """Remove a file or a folder tree without following links; a folder a jail made unreadable
    is opened up first. The walk does not recurse (_nofollow.remove_tree), so a tree of any depth is
    removed or raises OSError, on Python 3.10 too (op-008 t-571)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return
    _nofollow.remove_tree(path)


def _cleanup(path) -> str | None:
    """_remove(path); None when it worked, else what went wrong, for the run's record. A failure
    never replaces the result of the run it cleans up after."""
    try:
        _remove(path)
    except Exception as exc:          # OSError, or anything else: the result stands
        return "the home %s could not be removed: %s" % (
            _printable(path), _printable("%s: %s" % (type(exc).__name__, exc))[:_PROBE_OUT])
    return None


def _fresh_dir(path) -> None:
    _remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)

