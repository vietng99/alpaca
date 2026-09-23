"""Checked log reviews kept write-once, and an engineer's marks on their findings.

`submit` runs `quotes.check`, then keeps the review as a read-only JSON file named by the first
16 hex characters of its sha256 and records one event with the file's sha256. The engineer then
confirms or disputes each finding; every mark is an appended event, and the latest mark per
finding is the current one. `reviews` reads a reference's kept reviews back with their current
marks and says when a kept file no longer matches the sha256 its event recorded.

A review never changes a verdict. The caller owns every location and name: `log` (the log
file, or a resolver called with the reference), `folder` (the directory that holds this
reference's reviews, inside `root`) and the record event kinds (`kind` for a kept review,
`mark_kind` for a mark). Events go through `db.append_event` of the record at `root`.
"""
import hashlib
import json
import os
from pathlib import Path
import re

from alpaca import db, util
from alpaca.runlog import quotes

_REVIEW = re.compile(r"[a-f0-9]{16}")
MARKS = ("confirmed", "disputed")
ACTOR = "alpaca-runlog"


def _log(log, ref):
    return log(ref) if callable(log) else log


def _inside(root, folder):
    """`folder` resolved, refused when it is not inside `root` (kept paths are root-relative)."""
    root, folder = Path(root).resolve(), Path(folder).resolve()
    if folder != root and root not in folder.parents:
        raise ValueError("review folder %s is not inside %s" % (folder, root))
    return root, folder


def _event(root, session, kind, ref, data, *, actor=ACTOR, op=None):
    conn = db.connect(str(Path(root).resolve()))
    try:
        sid = session or os.environ.get("ALPACA_SESSION_ID") or actor
        with db.transaction(conn):
            if not db.rows(conn, "sessions", "sid=?", (sid,)):
                db.upsert(conn, "sessions", "sid", {"sid": sid, "started": util.now_iso(), "cwd": str(root)})
            return db.append_event(conn, session=sid, actor=actor, kind=kind, op=op,
                                   ref=ref, data=data, conn_in_txn=True)
    finally:
        conn.close()


def submit(root, ref, review, *, log, folder, kind, ref_key="receipt", session=None,
           actor=ACTOR, op=None):
    """Check and keep one review write-once; returns where it lives and its id."""
    root, folder = _inside(root, folder)
    kept = quotes.check(_log(log, ref), review, ref=ref, ref_key=ref_key)
    body = json.dumps(kept, indent=1, sort_keys=True, ensure_ascii=True) + "\n"
    ident = hashlib.sha256(body.encode()).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (ident + ".json")
    record = dict(kept, id=ident, submitted_at=util.now_iso())
    text = json.dumps(record, indent=1, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        raise ValueError("this review is already kept as %s" % ident)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
    rel = path.relative_to(root).as_posix()
    _event(root, session, kind, kept[ref_key], {
        ref_key: kept[ref_key], "review": ident, "path": rel,
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "log_sha256": kept["log_sha256"],
        "reviewer": kept["reviewer"], "outcome": kept["outcome"], "findings": len(kept["findings"])},
        actor=actor, op=op)
    return {"verdict": "PASS", "review": ident, "path": rel, "findings": len(kept["findings"])}


def _load(folder, ref, ident):
    if not _REVIEW.fullmatch(str(ident or "")):
        raise ValueError("review id must be 16 hex characters")
    path = Path(folder) / (ident + ".json")
    if path.is_symlink() or not path.is_file():
        raise ValueError("no review %s for %s" % (ident, ref))
    return json.loads(path.read_text())


def mark(root, ref, ident, finding, verdict, *, folder, kind, ref_key="receipt", note="", by=None,
         session=None, actor=ACTOR, op=None):
    """Append an engineer's confirm or dispute for one finding; the newest mark is current."""
    ref = str(ref)
    if verdict not in MARKS:
        raise ValueError("mark must be confirmed or disputed")
    review = _load(folder, ref, ident)
    if finding not in {f["id"] for f in review["findings"]}:
        raise ValueError("review %s has no finding %s" % (ident, finding))
    by = by or os.environ.get("USER") or "engineer"
    data = {ref_key: ref, "review": ident, "finding": finding, "mark": verdict, "by": by,
            "note": (note or "").strip()[:600]}
    event = _event(root, session, kind, ref, data, actor=actor, op=op)
    return {"verdict": "PASS", "review": ident, "finding": finding, "mark": verdict, "event": event.get("id")}


def reviews(root, ref, *, folder, kind, mark_kind, ref_key="receipt"):
    """Every kept review of one reference with its current marks, newest review first.

    Reads the record without writing. A kept file that no longer matches its sha256 in the
    record is returned with `intact: false` so the page can say so instead of trusting it.
    """
    ref = str(ref)
    root = Path(root).resolve()
    conn = db.connect_readonly(str(root))
    try:
        kept = {}
        for r in conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id", (kind, ref)):
            d = json.loads(r["data"])
            kept[d["review"]] = d
        marks = {}
        for r in conn.execute("SELECT ts,data FROM events WHERE kind=? AND ref=? ORDER BY id", (mark_kind, ref)):
            d = json.loads(r["data"])
            marks[(d["review"], d["finding"])] = dict(d, ts=r["ts"])
    finally:
        conn.close()
    out = []
    folder = Path(folder)
    for ident, event in kept.items():
        path = folder / (ident + ".json")
        try:
            text = path.read_bytes() if not path.is_symlink() else b""
            data = json.loads(text)
        except (OSError, ValueError):
            out.append({"id": ident, "intact": False, "path": event.get("path"), "findings": []})
            continue
        data["intact"] = hashlib.sha256(text).hexdigest() == event.get("sha256")
        data["path"] = event.get("path")
        for f in data.get("findings", []):
            m = marks.get((ident, f["id"]))
            f["mark"] = {k: m[k] for k in ("mark", "by", "note", "ts")} if m else None
        out.append(data)
    out.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    return {ref_key: ref, "reviews": out}
