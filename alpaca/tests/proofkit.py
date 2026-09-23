"""Test support: write a complete proof report for an id and seal it.

`alpaca task move <id> done` and `alpaca board move <row> done` ask for a sealed proof report
(alpaca/proof.py), so every test that closes a task or a row needs one. This writes it:

    ptr = proofkit.seal_for(project, "t-001")
    cli.main(["task", "move", "t-001", "done", "--proof", ptr])

`seal_for` scaffolds the report, replaces each `TODO(agent):` line with real prose, points the
Evidence list at a small file it writes inside the project, seals it, and returns the `local:`
pointer the move must carry. `evidence_file` and `report_file` name the two files a test can
tamper with to prove the seal notices, and `kept_paths` names the copies the seal keeps under
`.alpaca/proofs/<op>/<id>.evidence/`, which a test tampers with to prove the fallback notices too.

This is a helper module, not a conftest: a test imports it by name, so nothing here runs for a
test that does not ask for it.
"""
import os

from alpaca import db, proof, util

#: the prose that stands in for a section a person would write. Comfortably over the seal's
#: non-space floor, and it says something a reader could act on.
FILLER = ("I changed the module, wired the change into the caller and ran the suite over it. "
          "The values below are the ones that run printed, read back from the record.")

#: where `evidence_file` writes, relative to the project root.
EVIDENCE_DIR = "proofwork"


def evidence_file(root, ident, body=None) -> str:
    """Write a small evidence file for `ident` in the project and return its `local:` pointer."""
    rel = "%s/%s.txt" % (EVIDENCE_DIR, str(ident).replace("/", "-"))
    util.write_text(os.path.join(root, rel.replace("/", os.sep)),
                    body or "run output for %s\nexit 0\n" % ident)
    return "local:%s" % rel


def evidence_path(root, ident) -> str:
    """The on-disk path `evidence_file` wrote, for a test that edits or deletes it."""
    return os.path.join(root, EVIDENCE_DIR, "%s.txt" % str(ident).replace("/", "-"))


def kept_paths(root, ident, *, conn=None) -> list:
    """The on-disk copies the latest seal for `ident` kept, in Evidence order.

    An entry with no copy (a cap, or a pointer that is not `local:`) is left out, so the list is
    exactly the files a test can read back or tamper with."""
    own = conn is None
    conn = conn or db.connect(root)
    try:
        ev = proof.latest_seal(conn, ident)
        data = ev["data"] if ev and isinstance(ev.get("data"), dict) else {}
        return [os.path.join(root, str(i["kept"]).replace("/", os.sep))
                for i in (data.get("evidence") or [])
                if isinstance(i, dict) and i.get("kept")]
    finally:
        if own:
            conn.close()


def kept_path(root, ident, *, conn=None) -> str:
    """The first kept copy, which is the one `seal_for` writes for its single evidence file."""
    return kept_paths(root, ident, conn=conn)[0]


def report_file(root, ident, *, conn=None) -> str:
    """The on-disk path of the report for `ident`, for a test that edits it after the seal."""
    own = conn is None
    conn = conn or db.connect(root)
    try:
        return proof.report_path(root, proof.subject(conn, ident))
    finally:
        if own:
            conn.close()


def fill(text, evidence_lines, filler=None) -> str:
    """Replace every `TODO(agent):` line: the Evidence one becomes the pointer list, the rest
    become prose. Everything else in the scaffold, header and appendix included, is kept."""
    out, in_evidence = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            in_evidence = line[3:].strip() == proof.EVIDENCE
        if line.strip().startswith(proof.TODO_MARK):
            if in_evidence:
                out.extend("- %s" % p for p in evidence_lines)
            else:
                out.append(filler or FILLER)
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def write_report(root, ident, *, conn=None, evidence=None, filler=None) -> str:
    """Scaffold the report for `ident` and fill every section. Returns the report path."""
    own = conn is None
    conn = conn or db.connect(root)
    try:
        res = proof.scaffold(conn, root, ident, force=True)
        lines = list(evidence) if evidence is not None else [
            "%s - the run output this task produced" % evidence_file(root, ident)]
        util.write_text(res["path"], fill(util.read_text(res["path"]), lines, filler))
        return res["path"]
    finally:
        if own:
            conn.close()


def seal_for(root, ident, *, conn=None, evidence=None, filler=None, session="proofkit") -> str:
    """Write a complete report for `ident`, seal it, and return the `--proof` pointer."""
    own = conn is None
    conn = conn or db.connect(root)
    try:
        write_report(root, ident, conn=conn, evidence=evidence, filler=filler)
        payload = proof.seal(conn, root, ident, session=session)
        return "local:%s" % payload["path"]
    finally:
        if own:
            conn.close()
