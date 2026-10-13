"""Shared helper for tests that seal an after-action report: the t-624 review a session records
before `aar seal`, through the real verbs. `aar review` writes the packet, the review file is
written the way the fresh reviewer subagent answers it (every check of aar.REVIEW_CHECKS judged,
each with a note and a quote copied from the report), and the main session records `aar accept`.
The seal gate (aar.review_gate) stays in force; this only walks the flow it asks for. Not a test
file."""
import hashlib
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout

from alpaca import aar, cli

MAIN, REVIEWER = "main-1", "rev-1"
WHY = "1. Why did it fail?"
NOTE = "Read each why against the one before it."


def call(*argv, session=MAIN):
    """(exit code, stdout+stderr) of one verb run as `session`."""
    out = io.StringIO()
    argv = (["--session", session] if session else []) + [str(a) for a in argv]
    with redirect_stdout(out), redirect_stderr(out):
        code = cli.main(argv)
    return code, out.getvalue()


def review_doc(root, op, reviewer=REVIEWER, results=None, drop=(), sha=None, needle=WHY):
    """The review file a reviewer writes for the report of `op` as it is now: every check but
    `drop`, each "pass" unless `results` names another result, each quoting the line that holds
    `needle` (the first line that says something when no line holds it)."""
    with open(aar.report_path(root, op), "rb") as fh:
        data = fh.read()
    lines = data.decode("utf-8").split("\n")
    n = next((i for i, l in enumerate(lines, 1) if needle in l), None) \
        or next(i for i, l in enumerate(lines, 1) if len(l.strip()) >= 4)
    doc = {"op": op, "report_sha256": sha or hashlib.sha256(data).hexdigest(), "reviewer": reviewer,
           "checks": {}, "findings": []}
    for c in aar.REVIEW_CHECKS:
        if c not in drop:
            doc["checks"][c] = {"result": (results or {}).get(c, "pass"), "note": NOTE,
                                "quote": {"line": n, "text": lines[n - 1][:400]}}
    return doc


def accept(root, op, folder, *, session=MAIN, reviewer=REVIEWER, name=None):
    """aar review, the reviewer's file in `folder`, then aar accept by `session`: (code, output,
    review file path). A refused review comes back as it is (the report fails the content rules,
    which aar seal names too); a refused accept is a fault of this helper and fails the test."""
    code, out = call("aar", "review", op, "--reviewer", reviewer, session=session)
    if code != cli.PASS:
        return code, out, None
    doc = review_doc(root, op, reviewer=reviewer)
    path = os.path.join(folder, name or "review-%s-%s.json" % (op, doc["report_sha256"][:12]))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc))
    code, got = call("aar", "accept", op, "--review", path, session=session)
    assert code == cli.PASS, out + got
    return code, out + got, path
