"""Op-close package: a content-addressed archive of a closed op (M4.8).

An op is closed when its done_when is met, its rows are discharged and its wiki op page is
written. The package is a content-addressed archive of that op: a bundle, its digest and an
explicit restore line.

`close(conn, op, out_dir) -> {bundle, sha256, restore_cmd, ...}` emits, for one op:

  * a version-control BUNDLE of the whole tree (`git bundle create ... --all`), the single file
    from which the tree reconstructs anywhere with no network and no origin;
  * its SHA-256 DIGEST, taken over the bundle bytes, so the archive is content-addressed: the
    digest binds the exact bundle and a tampered bundle no longer matches it;
  * the op's RECORD SLICE as files: its obligation rows, its verdict rows, its decisions and its
    wiki op page, so the archive carries the record the bundle's commits do not;
  * a generated README naming the branch, the commit, the bundle name, the digest and the restore
    command (generated from the facts, never typed);
  * an explicit RESTORE LINE that clones the bundle into a fresh directory, reconstructing the
    tree; the digest and the restored HEAD are what a reader checks the archive against.

The package is EVIDENCE of a close, not a substitute for it: the close conditions (done_when met,
rows discharged, the wiki op page written) are judged by `alpaca op close` and are unchanged. This
module reads the record and the tree; it never mutates the record. `alpaca op close --package` calls it
additively, after the ordinary close has already succeeded.
"""
from __future__ import annotations

import json
import os
import subprocess

from alpaca import db, util


class PackageError(Exception):
    """A package that cannot be built: an unknown op, a root that is not a version-controlled
    tree, or a tree with no commit to bundle. Fail-closed; the caller reports it as a verdict."""


# --------------------------------------------------------------------------- git seam
def _git(root, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                          encoding="utf-8")


def _require_repo(root) -> None:
    r = _git(root, "rev-parse", "--is-inside-work-tree")
    if r.returncode != 0 or r.stdout.strip() != "true":
        raise PackageError("%s is not a version-controlled tree; cannot bundle it" % root)
    if _git(root, "rev-parse", "--verify", "HEAD").returncode != 0:
        raise PackageError("%s has no commit to bundle" % root)


def _root_from_conn(conn) -> str:
    """The project root the connection writes into: the parent of the `.alpaca` directory holding the
    database file. Keeps `close(conn, op, out_dir)` a three-argument call with no root threaded
    through, while a caller that knows the root can still pass it."""
    for _seq, _name, fpath in conn.execute("PRAGMA database_list").fetchall():
        if fpath:
            return os.path.dirname(os.path.dirname(os.path.abspath(fpath)))
    raise PackageError("this connection has no on-disk database, so no tree to package")


# --------------------------------------------------------------------------- record slice
def _op_rows(conn, op) -> list:
    return db.rows(conn, "rows", "op=? ORDER BY id", (op,))


def _op_verdicts(conn, op) -> list:
    return [e for e in db.events(conn, kind="verdict", limit=10 ** 9) if (e.get("op") or "") == op]


def _op_decisions(conn, op) -> list:
    return [e for e in db.events(conn, kind="decision", limit=10 ** 9) if (e.get("op") or "") == op]


def _op_page(conn, op) -> str:
    """The wiki op page: the op's record model rendered as markdown (plain hyphens, no em dash).

    Derived from `alpaca.historian.timeline`, so the page is a view of the record and never drifts from
    it. Falls back to the ops row when the historian is unavailable."""
    intent = done_when = None
    authority = judgment = None
    choices = []
    try:
        from alpaca import historian
        tl = historian.timeline(conn, op)
        intent, done_when = tl.get("intent"), tl.get("done_when")
        authority, judgment = tl.get("authority"), tl.get("judgment")
        choices = tl.get("choices") or []
    except Exception:
        rows = db.rows(conn, "ops", "id=?", (op,))
        if rows:
            intent, done_when = rows[0].get("intent"), rows[0].get("done_when")

    lines = ["# Op %s" % op, "",
             "- intent: %s" % (intent or "(none)"),
             "- done_when: %s" % (done_when or "(none)")]
    if authority:
        lines.append("- authority: %s (level %s, scope %s)"
                     % (authority.get("authority"), authority.get("level"),
                        authority.get("scope")))
    if judgment:
        lines.append("- judged by %s on basis: %s"
                     % (judgment.get("judge"), judgment.get("basis")))
    lines += ["", "## Choices log"]
    if choices:
        for c in choices:
            lines.append("- %s %s %s (%s) [%s]"
                         % (c.get("ts"), c.get("kind"), c.get("ref") or "-",
                            c.get("actor"), c.get("pointer")))
    else:
        lines.append("- (no notes on the record for this op)")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- README
def _render_readme(op, branch, commit, bundle_name, digest, restore_cmd) -> str:
    """The package README, generated from the facts rather than typed. Plain hyphens only."""
    return "\n".join([
        "# Op-close package: %s" % op,
        "",
        "A content-addressed archive of op %s: a version-control bundle, its digest, the op's" % op,
        "record slice and the line that reconstructs the tree into a fresh directory.",
        "",
        "- branch: %s" % branch,
        "- commit: %s" % commit,
        "- bundle: %s" % bundle_name,
        "- sha256: %s" % digest,
        "",
        "## Restore",
        "",
        "Reconstruct the tree into a fresh directory, then confirm HEAD is %s:" % commit,
        "",
        "    %s" % restore_cmd,
        "",
        "The digest above is taken over the bundle bytes: re-hash the bundle and compare before you",
        "trust it. A bundle whose sha256 does not match this line has been altered.",
        "",
        "## Record slice",
        "",
        "- records/rows.json      the op's obligation rows",
        "- records/verdicts.json  the verdict rows bound to those obligations",
        "- records/decisions.json the op's decision events",
        "- records/op-page.md     the wiki op page (intent, bar, authority, choices log)",
        "",
    ])


# --------------------------------------------------------------------------- close
def close(conn, op, out_dir, *, root=None) -> dict:
    """Build the op-close package for `op` under `out_dir`. Returns the bundle path, its sha256
    digest, the restore command and the paths of the README and the record-slice files.

    Refuses (PackageError) an unknown op, a root that is not a version-controlled tree, or a tree
    with no commit. Reads the record and the tree; never writes the record."""
    if not db.rows(conn, "ops", "id=?", (op,)):
        raise PackageError("no op %s on the record; nothing to package" % op)
    root = root or _root_from_conn(conn)
    _require_repo(root)

    out_dir = os.path.abspath(out_dir)
    records = os.path.join(out_dir, "records")
    os.makedirs(records, exist_ok=True)

    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()

    bundle = os.path.join(out_dir, "%s.bundle" % op)
    r = _git(root, "bundle", "create", bundle, "--all")
    if r.returncode != 0 or not os.path.isfile(bundle):
        raise PackageError("git bundle failed for %s: %s" % (root, r.stderr.strip()))

    digest = util.sha256_hex(open(bundle, "rb").read())

    # the record slice
    rows_p = os.path.join(records, "rows.json")
    verdicts_p = os.path.join(records, "verdicts.json")
    decisions_p = os.path.join(records, "decisions.json")
    page_p = os.path.join(records, "op-page.md")
    util.write_text(rows_p, json.dumps(_op_rows(conn, op), indent=2, sort_keys=True) + "\n")
    util.write_text(verdicts_p, json.dumps(_op_verdicts(conn, op), indent=2, sort_keys=True) + "\n")
    util.write_text(decisions_p, json.dumps(_op_decisions(conn, op), indent=2, sort_keys=True) + "\n")
    util.write_text(page_p, _op_page(conn, op))

    # the restore line: clone the bundle into a fresh directory beside the package.
    restore_dir = os.path.join(out_dir, "%s-restore" % op)
    restore_cmd = "git clone %s %s" % (bundle, restore_dir)

    readme_p = os.path.join(out_dir, "README.md")
    util.write_text(readme_p, _render_readme(
        op, branch, commit, os.path.basename(bundle), digest, restore_cmd))

    return {
        "op": op,
        "bundle": bundle,
        "sha256": digest,
        "restore_cmd": restore_cmd,
        "restore_dir": restore_dir,
        "readme": readme_p,
        "branch": branch,
        "commit": commit,
        "files": {"rows": rows_p, "verdicts": verdicts_p,
                  "decisions": decisions_p, "op_page": page_p},
    }
