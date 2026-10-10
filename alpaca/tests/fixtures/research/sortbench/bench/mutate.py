#!/usr/bin/env python3
"""The sortbench pack's mutation operators for the random-edit control: flip one comparison
operator in one file of src/sortlib/, chosen from the seed. Only the checker opens this file in the
fixture; it is here so `research.controls.mutate` names a script that exists and is executable."""
import random
import re
import sys


def main(argv):
    path, seed = argv[1], int(argv[2])
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    spots = [m.start() for m in re.finditer(r"<=|>=|<|>", text)]
    if not spots:
        return 1
    at = random.Random(seed).choice(spots)
    swap = {"<=": ">", ">=": "<", "<": ">=", ">": "<="}
    op = re.match(r"<=|>=|<|>", text[at:]).group(0)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text[:at] + swap[op] + text[at + len(op):])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
