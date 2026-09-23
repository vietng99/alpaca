#!/usr/bin/env python3
"""Plugin check: every response in a result file carried one expected HTTP status.

Usage: status_codes.py <result.json> <status>

The result file holds {"status": {"<code>": <count>, ...}}. The check passes when at least one
response was counted and every counted response has <status>.

It follows the plugin check contract in docs/runbook-format.md: exit 0 PASS, 1 FAIL,
2 BLOCKED (the result file is missing or unreadable, so there is nothing to judge), and the last
line printed to stdout is the reason.
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
    total = sum(int(n) for n in counts.values() if isinstance(n, int) and n > 0)
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
