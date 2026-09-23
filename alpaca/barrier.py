"""barrier.py - the outbound push barrier and the tier rule at the push boundary (M4.11).

The barrier is the mechanical control at the moment the bytes leave. It is NOT the decision to
push: a push to a shared remote stays a human decision at every autodrive level (CLAUDE.md boot
rule 3, `doctrine/leaves/hitl-decision-gate.md`), so the decision stays a review card (M3.5). The
barrier only REFUSES mechanically when what is leaving carries something it must not.

What it does, and why each clause is here:

  * `install(root)` writes a git pre-push hook and PINS a copy of this package and of the barrier
    settings in the git directory. Git runs pre-push before any object transfer, with the
    candidate refs on stdin, so it is the one seam where "about to leave" is still catchable. The
    hook runs the pinned copy, never the checked-out code, so a checkout of an older commit or a
    project.yaml without the barrier block cannot switch the barrier off (review 3, B8).
  * the scan reads EVERY OBJECT a push would send (`git rev-list --objects <local> --not
    <remote>`), not the working tree: each commit with its full tree, each annotated tag at every
    level of a tag chain, and any tree or blob a tag points at (B2). A secret deleted from the tip
    still ships in the commit that added it.
  * every tree entry's path name is read, whatever its mode, so a gitlink named with a term is
    caught (B3); a protected PREFIX, NAME or PATH (project.yaml `barrier.protected_paths`, compared
    without case) blocks the push (B13).
  * compressed files (gzip, tar, zip, xz, bzip2, zstd, WOFF fonts) are opened and their members
    and member names scanned (alpaca/gates/containers.py, B4). A recognised container that cannot
    be opened, or a blob too large for the text lane, refuses unless `barrier.allow_blobs` names
    its digest with a reason.
  * git runs with replace refs off (GIT_NO_REPLACE_OBJECTS), since pack-objects sends the real
    objects; a grafts file or a shallow repository refuses (B6).
  * the sealed-term list (`barrier.terms`) is resolved from the MAIN worktree, so a linked worktree
    shares it. A configured list that is absent REFUSES unless `barrier.terms_optional: true`
    says to push with the terms unchecked, and then the report and the gate line say so. Shape
    rules and protected paths run either way (B5). The list may carry `re:` lines and
    `!include:` lines (B1, B7); a line the barrier cannot use refuses.
  * metadata (commit and tag messages, ref names) is matched against the sealed terms; author,
    committer and tagger e-mails are also matched against the mailbox shape rule, so a real
    address cannot leave as an identity unless an allow rule names it (B7).
  * a shape false positive is cleared only by a narrow allow rule (project.yaml `barrier.allow`,
    `<regex>  # <reason>`), matched against the WHOLE value and never against a sealed term.
  * the report names DIGESTS and line numbers, never paths, refs or terms: a leak report that
    printed the offending value would re-leak it.
  * ANY error in the scan REFUSES the push (fail-closed). A barrier that lets a push through when
    it could not look is not a barrier; every exception folds to BLOCKED.

What a pre-push hook cannot stop is written down in docs/shipping.md (`--no-verify`,
`core.hooksPath`, uploads that are not a push).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import namedtuple

from alpaca import paths, project
from alpaca.gates import containers
from alpaca.gates import leak_audit
from alpaca.gates import verdict as vc

INSTRUMENT = "outbound-barrier"

R_PROTECTED = "PROTECTED-PATH-IN-PUSH"
R_LEAK = "SEALED-TERM-IN-PUSH"
R_SCAN_ERROR = "SCAN-ERROR-REFUSES"
R_NO_COMMITS = "NO-COMMITS-TO-SCAN"
R_CONFIG = "CONFIG-UNREADABLE-REFUSES"
R_TERMS_INVALID = "TERM-LIST-INVALID-REFUSES"
R_TERMS_MISSING = "TERM-LIST-MISSING-REFUSES"
R_TERMS_UNCHECKED = "TERMS-NOT-CHECKED"
R_UNOPENED = "BLOB-NOT-FULLY-SCANNED-REFUSES"
R_HISTORY = "HISTORY-REWRITTEN-LOCALLY-REFUSES"
R_PIN = "PINNED-BARRIER-UNUSABLE-REFUSES"

HOOK_NAME = "pre-push"
PIN_NAME = "alpaca-barrier"
_ZERO = "0" * 40

Verdict = namedtuple("Verdict", "verdict reasons report")


# ------------------------------------------------------------------ configuration (DATA)
def protected_paths(root):
    """The protected prefixes, names and paths, read from project.yaml `barrier.protected_paths`.
    DATA, never a code literal."""
    try:
        cfg = project.load(root) or {}
    except Exception:
        cfg = {}
    block = cfg.get("barrier") or {}
    return [str(p) for p in (block.get("protected_paths") or []) if str(p).strip()]


def tier(root):
    """The project tier (project.yaml top-level `tier`). Public is the default posture."""
    try:
        cfg = project.load(root) or {}
    except Exception:
        cfg = {}
    return str(cfg.get("tier") or "public")


def _digest(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8", "surrogateescape"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def path_is_protected(path, protected):
    """The rule `path` hits, or None. Case does not matter (`.ALPACA/x` hits `.alpaca/`).

    A rule ending in a slash is a folder: it hits that folder at the root, and when it names one
    folder (`.alpaca/`) it hits a folder of that name at any depth. A rule with no slash is a name
    (`.env`): it hits that name as any part of the path, so `sub/.env` is caught. A rule with an
    inner slash is a path from the root. `*` and `?` match within one part (`.env.*.local`)."""
    p = path.replace(os.sep, "/")
    if p.startswith("./"):
        p = p[2:]
    pl = p.lower()
    parts = pl.split("/")
    for rule in protected:
        r = rule.replace(os.sep, "/")
        if r.startswith("./"):
            r = r[2:]
        rl = r.lower()
        if rl.endswith("/"):
            body = rl.rstrip("/")
            if pl == body or pl.startswith(body + "/") or fnmatch.fnmatchcase(pl, body):
                return rule
            if "/" not in body and any(fnmatch.fnmatchcase(c, body) for c in parts[:-1]):
                return rule
        elif "/" in rl:
            if fnmatch.fnmatchcase(pl, rl):
                return rule
        elif any(fnmatch.fnmatchcase(c, rl) for c in parts):
            return rule
    return None


# ------------------------------------------------------------------ git plumbing
def _env():
    """git's environment for every barrier call: replace refs OFF, because pack-objects sends the
    real objects, so the barrier must read what is sent, not a local replacement."""
    env = dict(os.environ)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _gitb(root, *args):
    """Run git read-only in `root` and return its raw output. Raises on nonzero so the caller folds
    it to a refusal. Output is bytes: a path or message that is not UTF-8 is scanned, not refused."""
    out = subprocess.run(["git", "-C", root, *args], capture_output=True, env=_env())
    if out.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (args[0] if args else "",
                                                  out.stderr.decode("utf-8", "replace").strip()[:300]))
    return out.stdout


def _git(root, *args):
    return _gitb(root, *args).decode("utf-8", "surrogateescape")


def _exists(root, sha):
    return subprocess.run(["git", "-C", root, "cat-file", "-e", sha], capture_output=True,
                          env=_env()).returncode == 0


def _text_view(raw):
    """A text view of bytes for the text lanes: the strict codec ladder first, then UTF-8 with
    the undecodable bytes kept as escapes. The byte lane always reads the raw bytes as well."""
    text, _codec = leak_audit.decode_explicit(raw)
    return text if text is not None else raw.decode("utf-8", "surrogateescape")


class _Objects(object):
    """One `git cat-file --batch-check` and one `--batch` process for a whole scan, replace refs
    off. `read` returns an object whole; `chunks` streams it, so a large blob is never held whole."""

    def __init__(self, root):
        self.root = root
        self._procs = {}

    def _proc(self, which):
        p = self._procs.get(which)
        if p is None:
            p = subprocess.Popen(["git", "-C", self.root, "cat-file", "--" + which],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env=_env())
            self._procs[which] = p
        return p

    def _ask(self, which, sha):
        p = self._proc(which)
        p.stdin.write(sha.encode("ascii") + b"\n")
        p.stdin.flush()
        head = p.stdout.readline().split()
        if len(head) < 3 or head[1] == b"missing":
            raise RuntimeError("object %s is missing" % sha[:12])
        return p, head[1].decode("ascii"), int(head[2])

    def info(self, sha):
        _p, typ, size = self._ask("batch-check", sha)
        return typ, size

    @staticmethod
    def _readn(stream, n):
        buf = []
        while n > 0:
            chunk = stream.read(n)
            if not chunk:
                raise RuntimeError("git cat-file ended early")
            buf.append(chunk)
            n -= len(chunk)
        return b"".join(buf)

    def read(self, sha):
        p, _typ, size = self._ask("batch", sha)
        data = self._readn(p.stdout, size)
        self._readn(p.stdout, 1)
        return data

    def chunks(self, sha):
        p, _typ, size = self._ask("batch", sha)
        left = size
        while left:
            n = min(left, leak_audit.BYTE_CHUNK)
            yield self._readn(p.stdout, n)
            left -= n
        self._readn(p.stdout, 1)

    def close(self):
        for p in self._procs.values():
            try:
                p.stdin.close()
                p.wait(timeout=10)
            except Exception:
                p.kill()
        self._procs = {}


def commit_files(root, sha):
    """Every (path, blob_sha) tracked in commit `sha`, read from the object store (not the tree)."""
    files = []
    for mode, typ, obj, path in _tree_entries(root, sha):
        if typ == "blob":
            files.append((_text_view(path), obj))
    return files


def _tree_entries(root, treeish):
    """(mode, type, sha, path bytes) of every entry of `treeish`, recursively, trees and gitlinks
    included (`ls-tree -r -t`), read from the object store."""
    out = []
    for entry in _gitb(root, "ls-tree", "-r", "-t", "-z", treeish).split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        cols = meta.split()
        if len(cols) >= 3:
            out.append((cols[0].decode(), cols[1].decode(), cols[2].decode(), path))
    return out


def _entering_args(root, local_sha, remote_sha):
    """The `--not <remote>` part: omitted for a new remote ref, and when the remote's commit is not
    in this repository (a forced push over history this clone never had), so everything reachable
    from the local ref is scanned."""
    if not remote_sha or set(remote_sha) == {"0"} or not _exists(root, remote_sha):
        return [local_sha]
    return [local_sha, "--not", remote_sha]


def commits_entering(root, local_sha, remote_sha):
    """The commit shas that would enter the remote: those reachable from the local ref but not the
    remote ref. A fresh remote (remote_sha all-zero) means every commit reachable from local."""
    out = _git(root, "rev-list", *_entering_args(root, local_sha, remote_sha))
    return [line.strip() for line in out.splitlines() if line.strip()]


def objects_entering(root, local_sha, remote_sha):
    """Every object the push would send, as (sha, path bytes): commits, trees, blobs and tags at
    every level of a tag chain (`git rev-list --objects`)."""
    out = _gitb(root, "rev-list", "--objects", *_entering_args(root, local_sha, remote_sha))
    objs = []
    for line in out.split(b"\n"):
        if not line:
            continue
        sha, _, path = line.partition(b" ")
        objs.append((sha.decode("ascii"), path))
    return objs


def main_worktree(root):
    """The main worktree of the repository `root` belongs to (the first `git worktree list`
    entry), so a linked worktree finds the clone-local files (.alpaca/) of the main one."""
    try:
        for line in _git(root, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                return line[len("worktree "):]
    except Exception:
        pass
    return root


def _history_problems(root):
    """Local history rewrites git does not send as they look: a grafts file, a shallow clone."""
    problems = []
    grafts = _git(root, "rev-parse", "--git-path", "info/grafts").strip()
    if not os.path.isabs(grafts):
        grafts = os.path.join(root, grafts)
    if os.path.exists(grafts):
        problems.append("a grafts file (info/grafts) rewrites history in this clone only; git sends "
                        "the real history. Remove it, or rewrite the history for real")
    if _git(root, "rev-parse", "--is-shallow-repository").strip() == "true":
        problems.append("the repository is shallow, so the barrier cannot read the history it "
                        "would push; push from a full clone")
    return problems


# ------------------------------------------------------------------ the scan
def _config(root):
    """project.yaml, read strictly: an unreadable file raises, and the caller refuses the push. A
    missing file is an empty configuration (no protected paths, no terms)."""
    return project.load(root) or {}


_SAFE_DETAIL = ("list line", "included list", "the term list", "project.yaml:barrier.allow")


def _terms(root, cfg, base=None):
    """(TermList | None, reasons, detail, refusal_code | None).

    A configured list that is absent (or a link that points nowhere) refuses, unless
    `barrier.terms_optional: true`; then, as with no list configured, an empty TermList carries the
    shape allow rules so the shape rules still run, and the report says the terms were NOT
    checked. A list that is present but unusable, or a malformed allow rule, refuses."""
    block = cfg.get("barrier") or {}
    configured = block.get("terms")
    inline = block.get("sealed_terms")
    optional = block.get("terms_optional") is True
    base = base or main_worktree(root)
    rules, ar, ad = leak_audit.parse_shape_allow(block.get("allow"), "project.yaml:barrier.allow")
    if ar:
        return None, [R_TERMS_INVALID] + ar, ["an allow rule is unusable, so the push is refused"] + ad, \
            vc.BLOCKED
    empty = leak_audit.TermList()
    empty.shape_allow = rules
    if configured:
        configured = str(configured)
        p = configured if os.path.isabs(configured) else os.path.join(base, configured)
        if not os.path.isfile(p):
            how = " (a link that points nowhere)" if os.path.islink(p) else ""
            if optional:
                return empty, [R_TERMS_UNCHECKED], [
                    "sealed terms were NOT checked: the configured term list (barrier.terms: %s) is "
                    "absent%s and barrier.terms_optional is set; protected paths and shape rules were "
                    "checked" % (configured, how)], None
            return None, [R_TERMS_MISSING], [
                "sealed terms were NOT checked, so the push is refused: the configured term list "
                "(barrier.terms: %s) is absent in the main worktree%s. Supply it, or set "
                "barrier.terms_optional: true to push with the terms unchecked" % (configured, how)], \
                vc.BLOCKED
    elif not (isinstance(inline, list) and inline):
        return empty, [R_TERMS_UNCHECKED], [
            "sealed terms were NOT checked: no term list is configured (barrier.terms); protected "
            "paths and shape rules were checked"], None
    tl, reasons, detail = leak_audit.load_terms_from_project(root, cfg=cfg, base=base)
    bad = [r for r in reasons if r in (leak_audit.R_ALLOW_NO_REASON, leak_audit.R_ALLOW_BAD_REGEX,
                                       leak_audit.R_TERMS_EMPTY, leak_audit.R_TERMS_UNDECODABLE,
                                       leak_audit.R_TERM_TOO_SHORT, leak_audit.R_TERMS_ABSENT,
                                       leak_audit.R_TERM_LINE_UNUSABLE,
                                       leak_audit.R_TERM_INCLUDE_ABSENT)]
    if bad or tl is None:
        safe = [d if d.startswith(_SAFE_DETAIL) else "the term list could not be read" for d in detail]
        return None, [R_TERMS_INVALID] + bad, ["the term list or an allow rule is unusable, so the "
                                               "push is refused"] + safe[:10], vc.BLOCKED
    return tl, [], [], None


def _allow_blobs(block):
    """({blob sha: reason}, problems) from `barrier.allow_blobs` (`<sha>  # <reason>`): blobs the
    barrier may pass although it could not read them in full (a container it cannot open, a blob
    over the text-scan cap). The byte lane still scans them for the terms."""
    ok, bad = {}, []
    for n, entry in enumerate(block.get("allow_blobs") or [], start=1):
        body = str(entry)
        if "#" not in body:
            bad.append("barrier.allow_blobs entry %d carries no '# reason'" % n)
            continue
        sha, reason = body.split("#", 1)
        sha, reason = sha.strip().lower(), reason.strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) or not reason:
            bad.append("barrier.allow_blobs entry %d needs a full blob digest and a reason" % n)
            continue
        ok[sha] = reason
    return ok, bad


def _meta_hits(term_list, raw):
    """Sealed-term hits in commit, tag or ref metadata: the text lanes (terms and `re:` lines, no
    shape rules) and the byte lane. `raw` may be bytes or text."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "surrogateescape")
    th, _ts = leak_audit.scan_text(term_list, _text_view(raw), "", shape_on=False)
    if th:
        return True
    return bool(leak_audit.byte_hits(term_list, [raw]))


_IDENTITY = re.compile(rb"^(?:author|committer|tagger) [^\n]*<([^>\n]*)>", re.M)
_MAILBOX = dict(leak_audit.SHAPE_RULES)["shape:mailbox"]


def _identity_hits(term_list, raw):
    """True when an author, committer or tagger e-mail has the shape of a real mailbox and no
    allow rule names it (a real address must not leave as an identity by accident)."""
    head = raw.split(b"\n\n", 1)[0]
    for m in _IDENTITY.finditer(head):
        email = _text_view(m.group(1))
        mm = _MAILBOX.search(leak_audit.defang(email))
        if mm and not leak_audit._shape_allowed(term_list, mm.group(0))[0]:
            return True
    return False


def _blob_hits(term_list, raw, path, shape_on=True):
    """Term, `re:` and shape hits in one blob held in memory: the text lanes first, the byte lane as
    the backstop, exactly as the audit does per file."""
    text, _codec = leak_audit.decode_explicit(raw)
    hits, accounted = [], set()
    if text is not None:
        th, _ts = leak_audit.scan_text(term_list, text, path, shape_on=shape_on)
        hits.extend(th)
        accounted = {r["rule"] for r in th if r["kind"] == "term"}
    for rule in sorted(leak_audit.byte_hits(term_list, [raw], accounted)):
        hits.append({"rule": rule, "kind": "term", "lane": "bytes"})
    return hits


def _member_hits(term_list, name, data):
    """Term and `re:` hits in one container member and its name. Shape rules stay off inside a
    container: vendored packages carry their upstream authors' addresses."""
    th, _ts = leak_audit.scan_text(term_list, name, name, shape_on=False)
    if th:
        return True
    if len(data) > leak_audit.TEXT_SCAN_MAX_BYTES:
        raise containers.Unopenable("a member over the text-scan cap")
    return bool(_blob_hits(term_list, data, name, shape_on=False))


class _Scan(object):
    """The state of one scan: what was read, what was found. Every object is judged once."""

    def __init__(self, root, term_list, protected, allow_blobs, report, reasons, verdicts, detail):
        self.root, self.tl, self.protected, self.allow_blobs = root, term_list, protected, allow_blobs
        self.report, self.reasons, self.verdicts, self.detail = report, reasons, verdicts, detail
        self.objs = _Objects(root)
        self.blob_seen, self.path_seen, self.commit_seen = set(), set(), set()
        self.tag_seen, self.tree_seen = set(), set()
        self.sent_blobs = None     # with an object walk: the blobs the push sends (the rest are there)

    def _block(self, entry, reason, code):
        self.report["blocked"].append(dict(entry, reason=reason))
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.verdicts.append(code)

    def leak(self, entry):
        self._block(entry, R_LEAK, vc.FAIL)

    def commit(self, sha):
        if sha in self.commit_seen:
            return
        self.commit_seen.add(sha)
        self.report["commits_scanned"] += 1
        raw = self.objs.read(sha)
        if _meta_hits(self.tl, raw):
            self.leak({"commit": sha[:12], "where": "commit-metadata"})
        if _identity_hits(self.tl, raw):
            self.leak({"commit": sha[:12], "where": "identity"})
        m = re.match(rb"tree ([0-9a-f]+)", raw)
        if m:
            self.tree_seen.add(m.group(1).decode("ascii"))
        self.tree(sha, commit=sha)

    def tree(self, treeish, commit=None):
        for _mode, typ, obj, path in _tree_entries(self.root, treeish):
            name = _text_view(path)
            where = {"commit": commit[:12]} if commit else {"object": treeish[:12]}
            if typ != "tree":
                self.report["files_scanned"] += 1
            rule = path_is_protected(name, self.protected)
            if rule:
                self._block(dict(where, path_digest=_digest(path), rule_digest=_digest(rule)),
                            R_PROTECTED, vc.BLOCKED)
                continue
            if path not in self.path_seen:
                self.path_seen.add(path)
                th, _ts = leak_audit.scan_text(self.tl, name, name, shape_on=True)
                if th or leak_audit.byte_hits(self.tl, [path]):
                    self.leak(dict(where, path_digest=_digest(path), where="path-name"))
            if typ == "blob" and (self.sent_blobs is None or obj in self.sent_blobs):
                self.blob(obj, name, where)

    def blob(self, sha, name, where):
        if sha in self.blob_seen:
            return
        self.blob_seen.add(sha)
        entry = dict(where, path_digest=_digest(name), blob_digest=sha[:12])
        _typ, size = self.objs.info(sha)
        if size > leak_audit.TEXT_SCAN_MAX_BYTES:
            if leak_audit.byte_hits(self.tl, self.objs.chunks(sha)):
                self.leak(dict(entry, where="content"))
            else:
                self._unopened(sha, entry, "larger than the text-scan cap, so only its raw bytes "
                                           "were read")
            return
        raw = self.objs.read(sha)
        if _blob_hits(self.tl, raw, name):
            self.leak(dict(entry, where="content"))
            return
        try:
            for mname, data in containers.members(raw):
                if _member_hits(self.tl, mname, data):
                    self.leak(dict(entry, where="inside-container"))
                    return
        except containers.Unopenable as e:
            self._unopened(sha, entry, "a compressed file the barrier cannot open (%s)" % e)

    def _unopened(self, sha, entry, why):
        if sha.lower() in self.allow_blobs:
            return
        self._block(dict(entry, where="not-fully-read"), R_UNOPENED, vc.BLOCKED)
        self.detail.append("blob %s: %s; if it is known to be clean, name its digest in "
                           "barrier.allow_blobs with a reason" % (sha[:12], why))

    def tag(self, sha, ref=None):
        """Scan one annotated tag object; return (target sha, target type)."""
        raw = self.objs.read(sha)
        if sha not in self.tag_seen:
            self.tag_seen.add(sha)
            entry = {"ref_digest": _digest(ref)} if ref else {"object": sha[:12]}
            if _meta_hits(self.tl, raw):
                self.leak(dict(entry, where="tag-object"))
            if _identity_hits(self.tl, raw):
                self.leak(dict(entry, where="identity"))
        m = re.match(rb"object ([0-9a-f]+)\ntype ([a-z]+)", raw)
        return (m.group(1).decode("ascii"), m.group(2).decode("ascii")) if m else (None, None)

    def object(self, sha, path, typ):
        if typ == "commit":
            self.commit(sha)
        elif typ == "tag":
            self.tag(sha)
        elif typ == "tree":
            if not path and sha not in self.tree_seen:      # a tree a tag points at
                self.tree_seen.add(sha)
                self.tree(sha)
        elif typ == "blob":
            self.blob(sha, _text_view(path), {"object": sha[:12]})

    def ref(self, name, sha, walked):
        if _meta_hits(self.tl, name):
            self.leak({"ref_digest": _digest(name), "where": "ref-name"})
        if not sha or set(sha) == {"0"} or walked:
            return
        typ = self.objs.info(sha)[0]                         # no object walk: peel by hand
        seen = set()
        while typ == "tag" and sha not in seen:
            seen.add(sha)
            sha, typ = self.tag(sha, ref=name)
        if typ in ("commit", "tree", "blob") and sha:
            self.object(sha, b"", typ)


def scan(root, commits, protected=None, term_list=None, refs=None, objects=None, cfg=None,
         terms_base=None):
    """Scan what a push would send and return a Verdict.

    `commits` are commit shas (each read with its full tree); `objects` is the object walk of the
    push, as (sha, path bytes) from `objects_entering` (the hook passes it); `refs` are the
    (ref name, sha) pairs the push updates; `cfg` is the barrier configuration (the hook passes
    the pinned copy; default: this checkout's project.yaml); `terms_base` is where a relative
    `barrier.terms` resolves (default: the main worktree).

    PASS only when nothing protected and no sealed term or unallowed shape is found. A protected
    path blocks (BLOCKED); a sealed term fails (FAIL); a blob the barrier could not read in full,
    an absent or unusable term list, a local history rewrite or any error refuses (BLOCKED,
    fail-closed). The report names digests, never paths, refs or terms.
    """
    report = {"instrument": INSTRUMENT, "commits_scanned": 0, "files_scanned": 0,
              "blocked": [], "tier": "?"}
    reasons, verdicts, detail = [], [], []

    def done(verdict, why):
        report["reason_codes"] = why
        report["detail"] = detail
        report["verdict"] = verdict
        return Verdict(verdict, why, report)

    if cfg is None:
        try:
            cfg = _config(root)
        except Exception as e:
            detail.append("project.yaml could not be read, so the barrier cannot know what to "
                          "refuse: %s: %s" % (type(e).__name__, e))
            return done(vc.BLOCKED, [R_CONFIG])
    block = cfg.get("barrier") or {}
    report["tier"] = str(cfg.get("tier") or "public")
    if protected is None:
        protected = [str(p) for p in (block.get("protected_paths") or []) if str(p).strip()]
    allow_blobs, bad_blobs = _allow_blobs(block)
    if bad_blobs:
        detail.extend(["an allow_blobs entry is unusable, so the push is refused"] + bad_blobs)
        return done(vc.BLOCKED, [R_TERMS_INVALID])
    if term_list is None:
        try:
            term_list, treasons, tdetail, refusal = _terms(root, cfg, terms_base)
        except Exception as e:
            detail.append("the term list could not be resolved: %s" % type(e).__name__)
            return done(vc.BLOCKED, [R_SCAN_ERROR])
        detail.extend(tdetail)
        if refusal is not None:
            return done(refusal, treasons)
        reasons.extend(treasons)

    if not commits and not refs and not objects:
        detail.append("no commits are entering the remote; nothing to scan")
        return done(vc.PASS, reasons + [R_NO_COMMITS])

    s = _Scan(root, term_list, protected, allow_blobs, report, reasons, verdicts, detail)
    try:
        problems = _history_problems(root)
        if problems:
            detail.extend(problems)
            return done(vc.BLOCKED, reasons + [R_HISTORY])
        for sha in commits or []:
            s.commit(sha)
        typed = [(sha, path, s.objs.info(sha)[0]) for sha, path in (objects or [])]
        if objects is not None and not commits:
            # a blob the remote already has is not sent; its path names are still read
            s.sent_blobs = {sha for sha, _path, typ in typed if typ == "blob"}
        for sha, path, typ in typed:                          # commits first: they mark root trees
            if typ == "commit":
                s.object(sha, path, typ)
        for sha, path, typ in typed:
            if typ != "commit":
                s.object(sha, path, typ)
        for name, sha in (refs or []):
            s.ref(name, sha, walked=objects is not None)
    except Exception as e:
        # Fail-closed: a scan that could not look does not let the push through.
        reasons.append(R_SCAN_ERROR)
        detail.append("scan error refuses the push: %s: %s" % (type(e).__name__, e))
        return done(vc.BLOCKED, reasons)
    finally:
        s.objs.close()

    return done(leak_audit.worst(verdicts) if verdicts else vc.PASS, reasons)


def render(report):
    """A human-readable barrier report that names DIGESTS, never paths. Safe to print and keep."""
    lines = ["  %s (tier=%s): %s" % (INSTRUMENT, report.get("tier", "?"),
                                     vc.name_of(report.get("verdict", vc.BLOCKED)))]
    lines.append("  commits scanned: %d   files scanned: %d   blocked: %d"
                 % (report.get("commits_scanned", 0), report.get("files_scanned", 0),
                    len(report.get("blocked", []))))
    for b in report.get("blocked", []):
        if b.get("commit"):
            where = "commit %s" % b["commit"]
        elif b.get("object"):
            where = "object %s" % b["object"]
        else:
            where = "a pushed ref"
        lines.append("    - refused %s%s in %s [%s]"
                     % (b.get("path_digest") or b.get("blob_digest") or b.get("ref_digest") or "-",
                        " (%s)" % b["where"] if b.get("where") else "", where, b.get("reason")))
    for d in report.get("detail", []):
        lines.append("    - %s" % d)
    return "\n".join(lines)


# ------------------------------------------------------------------ canary scoring
def score_canary(root, sealed_path, verdicts_path):
    """Grade a blind classifier against a sealed canary set (the able-to-fail half of the barrier).
    Delegates to alpaca/gates/canary_score; returns (verdict_code, summary)."""
    from alpaca.gates import canary_score
    return canary_score.score_files(sealed_path, verdicts_path)


# ------------------------------------------------------------------ install: hook + pinned copy
def hook_path(root):
    hooks = _git(root, "rev-parse", "--git-path", "hooks").strip()
    if not os.path.isabs(hooks):
        hooks = os.path.join(root, hooks)
    return os.path.join(hooks, HOOK_NAME)


def pin_dir(root):
    """Where `install` pins the barrier: a folder in the repository's common git directory, shared
    by every worktree, never part of any commit."""
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return os.path.join(common, PIN_NAME)


def hook_body():
    """The pre-push hook: it hands stdin (the candidate refs) to the PINNED barrier, which refuses
    on anything but PASS. It names no absolute path and no folder literal, so it is rename-safe:
    it finds the pinned copy through git. It runs under the install's own launcher when the repo has
    one, else python3; a missing pinned copy refuses the push rather than skipping the scan."""
    return ("#!/bin/sh\n"
            "# alpaca outbound barrier (M4.11): scan everything a push would send and refuse\n"
            "# mechanically on a protected path or a sealed term. The decision to push at all is\n"
            "# still a human review card (M3.5); this only stops what must not leave. It runs the\n"
            "# copy pinned in the git directory by `bin/alpaca barrier install`, never the\n"
            "# checked-out code; run the install again to update it.\n"
            'pin="$(git rev-parse --path-format=absolute --git-common-dir)/%s"\n'
            'ALPACA_BARRIER_ROOT="$(pwd -P)"\n'
            "export ALPACA_BARRIER_ROOT\n"
            'if [ ! -f "$pin/run.py" ]; then\n'
            '  echo "GATE outbound-barrier: BLOCKED"\n'
            '  echo "  reason: no pinned barrier in the git directory; run bin/alpaca barrier install"\n'
            "  exit 2\n"
            "fi\n"
            "if [ -x bin/alpaca-python ]; then\n"
            '  exec bin/alpaca-python "$pin/run.py" prepush "$@"\n'
            "fi\n"
            'exec python3 "$pin/run.py" prepush "$@"\n' % PIN_NAME)


_RUN_PY = '''"""The pinned alpaca outbound barrier, written by `alpaca barrier install`. Do not edit.

The pre-push hook runs this file, never the checked-out package, so a checkout of an older commit
or a project.yaml without the barrier block cannot switch the barrier off. Run
`bin/alpaca barrier install` again to update it."""
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from alpaca import barrier  # noqa: E402

sys.exit(barrier.pinned_main(HERE, sys.argv[1:]))
'''


def _package_dir():
    import alpaca
    return os.path.dirname(os.path.abspath(alpaca.__file__))


def _package_files(pkg):
    """(relative path, full path) of every code file of the package (Python, and the SQL schema the
    record module reads at import), without its tests and web assets, sorted."""
    out = []
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = sorted(d for d in dirnames if d not in ("tests", "__pycache__", "web"))
        for fn in sorted(filenames):
            if fn.endswith((".py", ".sql")):
                full = os.path.join(dirpath, fn)
                out.append((os.path.relpath(full, pkg).replace(os.sep, "/"), full))
    return out


def _code_digest(pkg):
    h = hashlib.sha256()
    for rel, full in _package_files(pkg):
        with open(full, "rb") as fh:
            h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


def _pinned_config(cfg):
    return {"tier": str(cfg.get("tier") or "public"), "barrier": cfg.get("barrier") or {}}


def _config_digest(pinned):
    return hashlib.sha256(json.dumps(pinned, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def install(root):
    """Write the pre-push hook into this repo's hooks dir and pin the barrier (this package's code
    and the barrier settings of project.yaml) in the git directory. Returns the hook path.
    Idempotent: it overwrites its own prior hook and pin, and refuses (raises) to clobber a foreign
    hook it did not write, or to pin settings it cannot read."""
    from alpaca import VERSION, util
    p = hook_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                existing = fh.read()
        except OSError:
            existing = ""
        if existing and "alpaca outbound barrier" not in existing:
            raise RuntimeError("%s already exists and was not written by alpaca barrier; refusing to "
                               "clobber a foreign hook" % HOOK_NAME)
    pinned = _pinned_config(_config(root))
    pin = pin_dir(root)
    tmp = tempfile.mkdtemp(prefix=".%s-" % PIN_NAME, dir=os.path.dirname(pin))
    try:
        for rel, full in _package_files(_package_dir()):
            dst = os.path.join(tmp, "alpaca", *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(full, dst)
        util.write_text(os.path.join(tmp, "run.py"), _RUN_PY)
        util.write_text(os.path.join(tmp, "config.json"),
                        json.dumps(pinned, indent=1, sort_keys=True, default=str) + "\n")
        try:
            source = _git(root, "rev-parse", "--verify", "--quiet", "HEAD").strip() or None
        except Exception:
            source = None
        stamp = {"stamp": 1, "version": VERSION, "installed": util.now_iso(), "source_commit": source,
                 "code_digest": _code_digest(os.path.join(tmp, "alpaca")),
                 "config_digest": _config_digest(pinned)}
        util.write_text(os.path.join(tmp, "STAMP.json"), json.dumps(stamp, indent=1, sort_keys=True) + "\n")
        old = None
        if os.path.exists(pin):
            old = "%s.old-%d" % (pin, os.getpid())
            os.rename(pin, old)
        os.rename(tmp, pin)
        if old:
            shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    util.write_text(p, hook_body())
    os.chmod(p, 0o755)
    return p


def _refuse_now(why):
    print(render({"verdict": vc.BLOCKED, "tier": "?", "detail": [why]}))
    return vc.emit_verdict(INSTRUMENT, vc.BLOCKED, why)


def pinned_main(pin, argv):
    """The entry of the pinned copy (run.py). It checks that the pinned code and settings are the
    ones the install wrote, notes where the checkout differs from them, and runs the hook scan with
    the pinned settings. The checkout never changes what is checked."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.realpath(here) != os.path.realpath(pin):
        return _refuse_now("the pinned barrier did not load its own code; run bin/alpaca barrier "
                           "install again")
    try:
        with open(os.path.join(pin, "STAMP.json"), encoding="utf-8") as fh:
            stamp = json.load(fh)
        with open(os.path.join(pin, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as e:
        return _refuse_now("the pinned barrier cannot be read (%s); run bin/alpaca barrier install "
                           "again" % type(e).__name__)
    if _code_digest(os.path.join(pin, "alpaca")) != stamp.get("code_digest") or \
            _config_digest(cfg) != stamp.get("config_digest"):
        return _refuse_now("%s: the pinned barrier changed after install; run bin/alpaca barrier "
                           "install again" % R_PIN)
    cwd = os.environ.get("ALPACA_BARRIER_ROOT") or os.getcwd()
    try:
        root = _git(cwd, "rev-parse", "--show-toplevel").strip()
    except Exception:
        root = _git(cwd, "rev-parse", "--absolute-git-dir").strip()
    notices = []
    checkout = os.path.join(root, "alpaca")
    if os.path.isfile(os.path.join(checkout, "barrier.py")) and \
            _code_digest(checkout) != stamp.get("code_digest"):
        notices.append("notice: the checked-out barrier code differs from the installed copy; the "
                       "installed copy ran (run bin/alpaca barrier install to update it)")
    try:
        mine = _config_digest(_pinned_config(project.load(root) or {}))
        if mine != stamp.get("config_digest"):
            notices.append("notice: the barrier settings in the checked-out project.yaml differ from "
                           "the installed ones; the installed settings were used (run bin/alpaca "
                           "barrier install to update them)")
    except Exception:
        notices.append("notice: the checked-out project.yaml could not be read; the installed "
                       "barrier settings were used")
    return prepush(argv, root=root, cfg=cfg, notices=notices)


# ------------------------------------------------------------------ the hook entry
def prepush(argv=None, root=None, cfg=None, notices=()):
    """The pre-push hook body. Reads the candidate refs from stdin, walks every object that would
    enter the remote, scans it with the pinned settings `cfg`, and returns an exit code: 0 lets
    git proceed, non-zero refuses the push."""
    import sys
    root = root or paths.root()
    data = sys.stdin.read() if not sys.stdin.isatty() else ""
    objects, seen, refs = [], set(), []
    for line in data.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = cols[:4]
        if set(local_sha) == {"0"}:
            continue  # a delete pushes no objects
        refs.append((local_ref, local_sha))
        if remote_ref != local_ref:
            refs.append((remote_ref, None))
        try:
            entering = objects_entering(root, local_sha, remote_sha)
        except Exception as e:
            return _refuse_now("%s: %s" % (type(e).__name__, e))
        for sha, path in entering:
            if sha not in seen:
                seen.add(sha)
                objects.append((sha, path))
    if not refs:
        return vc.PASS
    res = scan(root, [], refs=refs, objects=objects, cfg=cfg)
    for n in notices:
        res.report.setdefault("detail", []).append(n)
    print(render(res.report))
    if res.verdict == vc.PASS:
        why = "nothing protected or sealed is leaving"
        if R_TERMS_UNCHECKED in res.reasons:
            why += "; the sealed terms were NOT checked (no term list)"
        return vc.emit_verdict(INSTRUMENT, vc.PASS, why)
    return vc.emit_verdict(INSTRUMENT, res.verdict, "push refused mechanically (still a human decision)")


# ------------------------------------------------------------------ CLI: alpaca barrier install|scan
def _cmd_barrier(args):
    from alpaca import cli
    root = cli._root()
    if args.barrier_verb == "install":
        try:
            p = install(root)
        except Exception as e:
            return vc.emit_verdict("alpaca-barrier-install", vc.BLOCKED, "%s: %s" % (type(e).__name__, e))
        return vc.emit_verdict("alpaca-barrier-install", vc.PASS,
                               "pre-push hook installed; the barrier and its settings are pinned in "
                               "the git directory", evidence=[os.path.basename(p), PIN_NAME])
    if args.barrier_verb == "scan":
        remote_sha = args.since or _ZERO
        try:
            objs = objects_entering(root, args.rev, remote_sha)
        except Exception as e:
            return vc.emit_verdict("alpaca-barrier-scan", vc.BLOCKED,
                                   "%s: %s" % (type(e).__name__, e))
        res = scan(root, [], objects=objs)
        print(render(res.report))
        return vc.emit_verdict("alpaca-barrier-scan", res.verdict, "; ".join(res.reasons[:6]))
    print("GATE alpaca-barrier: BLOCKED (unknown barrier verb; use install or scan)")
    return vc.BLOCKED


def _parser(sub):
    b = sub.add_parser("barrier", help="the outbound push barrier: install the pre-push hook, or "
                                       "scan the objects entering a remote")
    bv = b.add_subparsers(dest="barrier_verb")
    bv.add_parser("install", help="write the pre-push hook and pin the barrier in the git directory")
    sc = bv.add_parser("scan", help="scan the objects reachable from a rev (not the working tree)")
    sc.add_argument("rev", help="the local ref/sha whose objects would enter the remote")
    sc.add_argument("--since", default=None, help="the remote sha already present (default: none, "
                                                  "so every object reachable from rev is scanned)")


def _register():
    from alpaca import cli
    cli.command("barrier")(_cmd_barrier)
    cli.register_parser("barrier", _parser)


_register()


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "prepush":
        return prepush(argv[1:])
    ap = vc.make_parser(name=INSTRUMENT, description="outbound push barrier")
    ap.add_argument("verb", nargs="?", choices=["prepush"], help="hook entry")
    ap.parse_args(argv)
    return prepush([])


if __name__ == "__main__":
    import sys
    sys.exit(main())
