"""Rebuild the rr-004 fixtures of alpaca/tests/test_research_calltime.py from the raw transcripts of
the control run (in-session-runtime.md section 18, the first paragraph after the title).

    python3 build_fixtures.py --sessions <driver session folder>/subagents --root <project root> \
        --tries <project root>/.alpaca/verify --out <this folder>

The tests never run this file. It needs the project's alpaca package on its path (run it with
PYTHONPATH=<project root>). It keeps the source path and the sha256 of every raw file in
MANIFEST.json (the sha256 is that of the raw source, before the changes below), so a reader can
see what each fixture was cut from. A fixture holds the records of the named calls unchanged: the
`tool_use` record and the `tool_result` record that answers it. Four things differ from the source
bytes, and all four are about what a shipped file may hold:

* the project root and the home folder are written as the placeholders `@ROOT@` and `@HOME@`
  and the loader puts the test project's root back (a shipped file never holds the home path);
  the folder that holds the host's pooled agent accounts (the parent of CLAUDE_CONFIG_DIR, or
  `--accounts`) is written as `@ACCOUNTS@`, which stays as it is (no test reads those paths);
* every sealed term of the project's term list (`barrier.terms` of project.yaml, read from
  `--root` at build time and never written in this file) and every address with the shape of an
  e-mail address is written as `SEALEDTERM` or `EMAILADDR`, so the release barrier finds nothing
  in the shipped fixtures. No test reads a replaced value: it was the owner's name, e-mail or a
  file name of the dev project, in text that the judge reads only for paths of the project,
  calls and receipts;
* the static context records of the whole transcripts (the skill listing, the owner's global and
  project instructions and memory, the agent and tool listings) are cut to a stub. They are not
  calls and no test reads them. The records stay, with their type and index;
* every transcript is JSON with `ensure_ascii`, so it holds only ASCII, and is gzip-compressed and then
  written as base64 text (the `.gz.b64` files, so that every shipped file is ASCII), as are the large
  `out/` files of the pair (the loader unpacks them).

The build ends by scanning every file it wrote (opening each packed file) with the barrier's own text
scan, and fails when one still holds a sealed term or an address.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import sys

ROOT, HOME, ACCOUNTS = [""], [""], [""]
#: the sealed-term list of the project (a TermList), loaded in main() from project.yaml
SEALED = [None]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
#: attachment types of a whole transcript that only carry the runtime's static context
CONTEXT_ATTACHMENTS = ("skill_listing", "session_context", "instructions", "mcp_instructions_delta",
                       "agent_listing_delta", "deferred_tools_delta")
STUB = "left out of the fixture, the static context of the runtime"
CUT = ("a84427dcadd1ac87b", "aa63cf2e2ead94ce0", "a05179e7708318396", "af8d86438e38dbbd8",
       "a660f66772906b0c2", "ad36c863082a96f89", "af6aa4839592eef38")
WHOLE = ("a84427dcadd1ac87b", "ad6eaccc971b5eec2")
#: tool names whose calls the replay reads (the job's tools)
TOOLS = ("Bash", "Read")
#: (folder under verify, agent id, files cited by the verdict that C4 reads) for the C4 and C14 tests
TRY_FILES = {
    "ad36c863082a96f89": ("t-193/vf-t-193-82943/v2/r0/t1",
                          ["tree/alpaca/research/types.py", "tree/alpaca/research/state.py"]),
    "af6aa4839592eef38": ("t-193/vf-t-193-82943/v2/r0/t2",
                          ["tree/alpaca/research/types.py", "tree/alpaca/research/state.py"]),
    "a660f66772906b0c2": ("t-194/vf-t-194-83138/v1/r0/t1", []),
}
PAIR_TRIES = {"a84427dcadd1ac87b": "t-193/vf-t-193-82943/v2/r0/t0",
              "ad6eaccc971b5eec2": "t-193/vf-t-193-82943/v1/r0/t0"}


def sha(path):
    h = hashlib.sha256()
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            h.update(name.encode("utf-8"))
            h.update(sha(os.path.join(path, name)).encode("ascii"))
        return h.hexdigest()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scrub(text):
    """The text without an e-mail address or a sealed term: `EMAILADDR` and `SEALEDTERM` stand in."""
    text = EMAIL.sub("EMAILADDR", text)
    tl = SEALED[0]
    if tl is not None:
        for term, _key in sorted(tl.terms, key=lambda t: -len(t[0])):
            text = re.sub(re.escape(term), "SEALEDTERM", text, flags=re.IGNORECASE)
        for _label, rx, _brx in tl.regexes:
            text = rx.sub("SEALEDTERM", text)
    return text


def holes(text, root, home):
    text = text.replace(root, "@ROOT@").replace(home, "@HOME@")
    if ACCOUNTS[0]:
        text = text.replace(ACCOUNTS[0] + "/", "@ACCOUNTS@/")
    return scrub(text)


def stubbed(value):
    """A context attachment with every string cut to the stub (keys and shape stay)."""
    if isinstance(value, str):
        return STUB
    if isinstance(value, list):
        return [stubbed(v) for v in value]
    if isinstance(value, dict):
        return {k: stubbed(v) for k, v in value.items()}
    return value


def blank_context(rec):
    att = rec.get("attachment")
    if rec.get("type") == "attachment" and isinstance(att, dict) \
            and att.get("type") in CONTEXT_ATTACHMENTS:
        out = dict(rec, attachment=dict(stubbed(att), type=att["type"]))
        if "rendered" in out:       # the runtime's own copy of the same text
            out["rendered"] = stubbed(out["rendered"])
        return out
    return rec


def pack(raw: bytes) -> bytes:
    """gzip (no timestamp) and then base64, so the shipped file is ASCII text. Base64 text can
    hold a sealed term by chance (a short term is a run of letters), so a result that the barrier's
    scan flags is packed again with a longer file-name field in the gzip header (one to four
    bytes), which moves every base64 group; the loader ignores the field."""
    import io
    for salt in range(5):
        buf = io.BytesIO()
        with gzip.GzipFile(filename="s" * salt, fileobj=buf, mode="wb", mtime=0) as fh:
            fh.write(raw)
        text = base64.encodebytes(buf.getvalue())
        if not packed_hits(text):
            return text
    raise SystemExit("no packing of this file is free of a sealed term")


def packed_hits(text: bytes) -> list:
    """The hits of the barrier's text scan in the base64 text of a packed file."""
    from alpaca.gates import leak_audit
    hits, _sup = leak_audit.scan_text(SEALED[0], text.decode("ascii"), "packed", shape_on=True)
    return hits


def dump_gz(path, text):
    with open(path + ".b64", "wb") as fh:
        fh.write(pack(text.encode("ascii")))


def jsonl(records, root, home):
    out = []
    for rec in records:
        line = holes(json.dumps(blank_context(rec), ensure_ascii=False), root, home)
        out.append(json.dumps(json.loads(line), ensure_ascii=True))
    return "\n".join(out) + "\n"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(raw) for raw in fh if raw.strip()]


def cut(records):
    """The first record, and every Bash or Read call with its result."""
    keep, wanted = [], set()
    for i, rec in enumerate(records):
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        if i == 0 and rec.get("type") == "user":
            keep.append(rec)
            continue
        if rec.get("type") == "assistant" and isinstance(content, list):
            ids = [c["id"] for c in content if isinstance(c, dict) and c.get("type") == "tool_use"
                   and c.get("name") in TOOLS]
            if ids:
                wanted.update(ids)
                keep.append(rec)
        elif rec.get("type") == "user" and isinstance(content, list):
            if any(isinstance(c, dict) and c.get("type") == "tool_result"
                   and c.get("tool_use_id") in wanted for c in content):
                keep.append(rec)
    return keep


def ledger_of(folder):
    """The driver's served ledger of a try, as verifier.serve left it in requests/ and receipts/."""
    led = {"requests": {}, "receipts": {}}
    for name in sorted(os.listdir(os.path.join(folder, "requests")),
                       key=lambda x: int(x[:-5]) if x[:-5].isdigit() else 0):
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        n = int(name[:-5])
        with open(os.path.join(folder, "requests", name), encoding="utf-8") as fh:
            body = json.load(fh)
        with open(os.path.join(folder, "receipts", "%d.answer" % n), encoding="utf-8") as fh:
            ans = json.load(fh)
        argv = list(body.get("argv") or [])
        entry = {"argv": argv, "line": ans.get("line"), "rc": ans.get("rc")}
        line = str(ans.get("line") or "")
        if argv[:2] == ["--submit", "-"]:
            entry.update(kind="submit", sha256=line.split()[-1],
                         body=holes(str(body.get("body")), ROOT[0], HOME[0]))
            led["verdict_sha256"], led["verdict_body"] = entry["sha256"], entry["body"]
        elif line.startswith("rerun: receipt "):
            rid, digest = line.split()[2], line.split()[3]
            entry.update(kind="receipt", id=rid, sha256=digest)
            led["receipts"][rid] = digest
        else:
            entry.update(kind="refused")
        led["requests"][str(n)] = entry
    return led


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--tries", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--accounts", default=None,
                    help="the folder of the host's pooled agent accounts (default: the parent of "
                         "CLAUDE_CONFIG_DIR when it is set)")
    a = ap.parse_args()
    root = a.root.rstrip("/")
    home = os.path.expanduser("~")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").rstrip("/")
    accounts = (a.accounts or (os.path.dirname(config_dir) if config_dir else "")).rstrip("/")
    ROOT[0], HOME[0], ACCOUNTS[0] = root, home, accounts if accounts not in ("", "/") else ""
    SEALED[0] = sealed_terms(root)
    manifest = {"note": "see build_fixtures.py: the records are scrubbed of sealed terms, e-mail "
                        "addresses and the runtime's static context; each source_sha256 is that of the "
                        "raw source", "files": []}

    def note(rel, source, extra=None):
        entry = {"path": rel, "source": holes(os.path.abspath(source).replace(root, "@ROOT@").replace(home, "~"), root, home),
                 "source_sha256": sha(source)}
        entry.update(extra or {})
        manifest["files"].append(entry)

    for aid in sorted(set(CUT) | set(WHOLE)):
        src = os.path.join(a.sessions, "agent-%s.jsonl" % aid)
        recs = read(src)
        if aid in WHOLE:
            dump_gz(os.path.join(a.out, "%s.whole.jsonl.gz" % aid), jsonl(recs, root, home))
            note("%s.whole.jsonl.gz.b64" % aid, src, {"records": len(recs)})
        if aid in CUT:
            kept = cut(recs)
            dump_gz(os.path.join(a.out, "%s.jsonl.gz" % aid), jsonl(kept, root, home))
            note("%s.jsonl.gz.b64" % aid, src, {"records": len(kept), "of": len(recs)})
    for aid, (rel, files) in TRY_FILES.items():
        folder = os.path.join(a.tries, rel)
        dst = os.path.join(a.out, aid)
        os.makedirs(dst, exist_ok=True)
        # the receipts the driver wrote (the verdict copy is the ledger's submitted body) and the
        # packet's brief, with the placeholders in; their bytes change with the root's length, so
        # the loader recomputes the hashes that name them
        os.makedirs(os.path.join(dst, "receipts"), exist_ok=True)
        for n in sorted(os.listdir(os.path.join(folder, "receipts"))):
            if n.endswith(".json") and n != "verdict.json":
                src = os.path.join(folder, "receipts", n)
                with open(src, encoding="utf-8") as fh:
                    text = holes(fh.read(), root, home)
                with open(os.path.join(dst, "receipts", n), "w", encoding="ascii") as fh:
                    fh.write(json.dumps(json.loads(text), ensure_ascii=True, sort_keys=True))
                note("%s/receipts/%s" % (aid, n), src)
        os.makedirs(os.path.join(dst, "packet"), exist_ok=True)
        src = os.path.join(folder, "packet", "brief.json")
        with open(src, encoding="utf-8") as fh:
            text = holes(fh.read(), root, home)
        with open(os.path.join(dst, "packet", "brief.json"), "w", encoding="ascii") as fh:
            fh.write(json.dumps(json.loads(text), ensure_ascii=True, sort_keys=True))
        note("%s/packet/brief.json" % aid, src)
        # every file the verdict reads (evidence of kind `read`) that the try folder holds
        with open(os.path.join(folder, "receipts", "verdict.json"), encoding="utf-8") as fh:
            verdict = json.load(fh)
        cited = sorted({e["read"] for c in verdict["claims"] for e in c["evidence"]
                        if isinstance(e, dict) and e.get("read")})
        for f in cited:
            src = os.path.join(folder, f)
            if f.split("/")[0] not in ("packet", "tree") or not os.path.isfile(src):
                manifest["files"].append({"path": "%s/%s" % (aid, f), "absent": True,
                                          "note": "cited by the verdict, not copied"})
                continue
            os.makedirs(os.path.dirname(os.path.join(dst, f)), exist_ok=True)
            with open(src, encoding="utf-8") as fh:
                text = holes(fh.read(), root, home)
            if f.startswith("packet/"):
                # evidence copies hold text that a scan of the tracked files reads as stale paths
                dump_gz(os.path.join(dst, f + ".gz"), text)
                note("%s/%s.gz.b64" % (aid, f), src, {"gzip": True})
            else:
                with open(os.path.join(dst, f), "w", encoding="ascii") as fh:
                    fh.write(text)
                note("%s/%s" % (aid, f), src)
        led = ledger_of(folder)
        with open(os.path.join(dst, "ledger.json"), "w", encoding="ascii") as fh:
            json.dump(led, fh, ensure_ascii=True, sort_keys=True)
        note("%s/ledger.json" % aid, os.path.join(folder, "requests"),
             {"derived": "requests/ and receipts/*.answer"})
    for aid, rel in PAIR_TRIES.items():
        folder = os.path.join(a.tries, rel, "out")
        dst = os.path.join(a.out, aid, "out")
        if os.path.isdir(folder):
            os.makedirs(dst, exist_ok=True)
            for n in sorted(os.listdir(folder)):
                src = os.path.join(folder, n)
                if os.path.isfile(src):
                    with open(src, "rb") as fh:
                        raw = fh.read()
                    try:
                        raw = scrub(raw.decode("utf-8")).encode("utf-8")
                    except UnicodeDecodeError:
                        raise SystemExit("out file %s is not text; scrub it by hand" % src)
                    with open(os.path.join(dst, n + ".gz.b64"), "wb") as gz:
                        gz.write(pack(raw))
                    note("%s/out/%s.gz.b64" % (aid, n), src, {"gzip": True})
    with open(os.path.join(a.out, "MANIFEST.json"), "w", encoding="ascii") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    left = sealed_left(a.out)
    if left:
        raise SystemExit("a sealed term is still in: %s" % ", ".join(left))


def sealed_terms(root):
    """The project's sealed-term list, as the release barrier reads it."""
    from alpaca import project
    from alpaca.gates import leak_audit
    tl, reasons, _detail = leak_audit.load_terms_from_project(root, cfg=project.load(root) or {},
                                                              base=root)
    if tl is None or not tl.terms:
        raise SystemExit("no sealed-term list for %s (%s); a build without it would not scrub"
                         % (root, ", ".join(reasons)))
    return tl


def sealed_left(folder):
    """The files under `folder` in which the barrier's text scan still finds a sealed term (a packed
    file is decoded and opened first)."""
    from alpaca.gates import leak_audit
    bad = []
    for dirpath, _dirs, files in os.walk(folder):
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                raw = fh.read()
            if path.endswith(".gz.b64"):
                raw = gzip.decompress(base64.b64decode(raw))
            text = raw.decode("utf-8")
            hits, _sup = leak_audit.scan_text(SEALED[0], text, path, shape_on=True)
            if hits:
                bad.append(os.path.relpath(path, folder))
    return bad


if __name__ == "__main__":
    main()
