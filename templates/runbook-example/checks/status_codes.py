#!/usr/bin/env python3
"""Plugin check: every response in a result file carried one expected HTTP status.

Usage: status_codes.py <result.json> <status>

The result file holds {"status": {"<code>": <count>, ...}}. The check passes when at least one
response was counted and every counted response has <status>.

It follows the plugin check contract of the runbook format (docs/runbook-format.md, or
FORMAT.md in the partner runbook kit): exit 0 PASS, 1 FAIL, 2 BLOCKED (the result file is
missing or unreadable, or a count is not a whole number, so there is nothing to judge), and the
last line printed to stdout is the reason.
"""
import json
import sys


def main(argv):
    if len(argv) != 2:
        print("usage: status_codes.py <result.json> <status>")
        return 2
    path, want = argv
    try:
        with open(path, encoding="utf-8") as fh:
            counts = json.load(fh).get("status")
    except (OSError, ValueError, AttributeError) as exc:
        print("cannot read %s: %s" % (path, exc))
        return 2
    if not isinstance(counts, dict):
        print("%s has no status object" % path)
        return 2
    for code, n in sorted(counts.items()):
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            # a count this script cannot read is nothing to judge: BLOCKED, never a FAIL or a PASS
            print("%s: the count for status %s is %r, not a whole number of 0 or more" % (path, code, n))
            return 2
    total = sum(counts.values())
    if total == 0:
        print("%s counted no responses" % path)
        return 1
    other = {code: n for code, n in counts.items() if str(code) != want and n}
    if other:
        print("%d of %d responses were not %s: %s" % (
            sum(other.values()), total, want,
            ", ".join("%s x%d" % (c, n) for c, n in sorted(other.items()))))
        return 1
    print("all %d responses were %s" % (total, want))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
