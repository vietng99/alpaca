"""A review of one captured log, checked against the log bytes before it is kept.

An agent reads a log (or a packet cut from it), then writes a review: a summary and findings,
each finding naming a line range and quoting text from it. `check` accepts the review only when
the log still has the sha256 the agent read and every quote appears inside its named lines, so
an engineer can check each claim against the source by jumping to the line. Every finding that
does not hold is named in one ValueError; one bad finding rejects the whole review.

The caller owns where logs live and what a review is about: it passes the log file (`log_path`
resolves one inside a log folder and refuses links and anything outside it) and the reference
the review must name (`ref`, under the review key `ref_key`). `alpaca.runlog.reviews` keeps a
checked review write-once and records the engineer's marks on its findings.
"""
import hashlib
from pathlib import Path

SEVERITIES = ("error", "warning", "info")
OUTCOMES = ("matches-verdict", "disagrees-with-verdict", "inconclusive")
MAX_FINDINGS = 50
MAX_SPAN = 30          # a finding covers at most this many lines past its first


def log_path(logdir, name, suffix=".log"):
    """`<logdir>/<name><suffix>`, refusing links and anything outside the log folder."""
    logdir = Path(logdir).resolve()
    path = logdir / (str(name) + suffix)
    if path.is_symlink() or path.resolve().parent != logdir or not path.is_file():
        raise ValueError("no captured log for %s" % name)
    return path


def _text(value, name, lo, hi):
    if not isinstance(value, str) or not (lo <= len(value.strip()) <= hi):
        raise ValueError("%s must be text of %d to %d characters" % (name, lo, hi))
    return value.strip()


def check(log, review, *, ref, ref_key="receipt"):
    """The review as it will be kept, or ValueError naming every finding that does not hold.

    `log` is the log file the review quotes; `ref` is what the review must be about (a review
    that names another `ref_key` value is refused, one that names none is taken as about `ref`).
    """
    ref = str(ref)
    if not isinstance(review, dict):
        raise ValueError("review must be a JSON object")
    if str(review.get(ref_key, ref)) != ref:
        raise ValueError("review names %s %s, not %s" % (ref_key, review.get(ref_key), ref))
    raw = Path(log).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if review.get("log_sha256") != sha:
        raise ValueError("log_sha256 does not match the log now on disk (%s); read a fresh packet" % sha)
    lines = raw.decode("utf-8", "replace").split("\n")
    outcome = review.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError("outcome must be one of " + ", ".join(OUTCOMES))
    findings = review.get("findings")
    if not isinstance(findings, list) or not 1 <= len(findings) <= MAX_FINDINGS:
        raise ValueError("findings must be a list of 1 to %d items" % MAX_FINDINGS)
    kept, problems = [], []
    for i, f in enumerate(findings, 1):
        try:
            if not isinstance(f, dict):
                raise ValueError("not an object")
            line, end = f.get("line"), f.get("end_line", f.get("line"))
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (line, end)):
                raise ValueError("line and end_line must be integers")
            if not 1 <= line <= end <= min(len(lines), line + MAX_SPAN):
                raise ValueError("lines %s to %s are outside the log or span more than %d lines" % (line, end, MAX_SPAN))
            if f.get("severity") not in SEVERITIES:
                raise ValueError("severity must be one of " + ", ".join(SEVERITIES))
            quote = _text(f.get("quote"), "quote", 4, 400)
            block = "\n".join(lines[line - 1:end])
            if quote not in block and " ".join(quote.split()) not in " ".join(block.split()):
                raise ValueError("quote is not in lines %d to %d" % (line, end))
            item = {"id": "f%d" % i, "line": line, "end_line": end, "severity": f["severity"], "quote": quote,
                    "meaning": _text(f.get("meaning"), "meaning", 8, 1000)}
            if f.get("check"):
                item["check"] = _text(f["check"], "check", 4, 600)
            kept.append(item)
        except ValueError as exc:
            problems.append("finding %d: %s" % (i, exc))
    if problems:
        raise ValueError("review rejected; " + "; ".join(problems))
    return {ref_key: ref, "log_sha256": sha, "log_lines": len(lines) - (1 if lines and lines[-1] == "" else 0),
            "reviewer": _text(review.get("reviewer") or "unnamed agent", "reviewer", 1, 120),
            "summary": _text(review.get("summary"), "summary", 20, 3000), "outcome": outcome, "findings": kept}
