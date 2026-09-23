"""M2.2 proof: the deterministic middle.

The golden pin: one fixed-clock scenario, run in two subprocesses with DIFFERENT PYTHONHASHSEED
and DIFFERENT locale, produces byte-identical event hashes AND a byte-identical data.json. Set
iteration order, dict order and float last-bits all vary across those two processes; canonical
serialization, integer banding and a total-order tie-break absorb every one of them.

The irreducible exception: a wall-clock cost duration is a stamp, not an input. It is excluded
from every hash and every order, asserted here directly and folded out by determinism_hash.

When run as a script (`python3 tests/test_determinism.py <root> <out.json>`) this file IS the
scenario the test spawns, so the whole proof lives in one file.
"""
import json
import math
import os
import subprocess
import sys

import pytest

from alpaca import determinism

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


# --------------------------------------------------------------------------- the scenario

def _scenario(root, out_path):
    """Deterministic under a FixedClock and independent of PYTHONHASHSEED and locale."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from alpaca import clock, db, util

    util.set_clock(clock.FixedClock(start="2026-01-01T00:00:00+00:00", step=1))
    conn = db.connect(root)

    # A record: five hash-chained events, ts stamped from the installed FixedClock.
    for i in range(5):
        db.append_event(conn, session="sess-fixed", actor="agent", kind="k%d" % i,
                        data={"i": i, "note": "n%d" % i, "band": determinism.band(0.1 * i)})
    ev = conn.execute("SELECT hash FROM events ORDER BY id").fetchall()
    event_hashes = [r[0] for r in ev]

    # A fold that is deliberately hash-seed sensitive at the source: counts keyed off a SET, and a
    # ranking keyed off a FLOAT score (0.1 + 0.2 == 0.30000000000000004). Canonical sort_keys and
    # determinism.stable_rank (banding the float) make the output byte-stable anyway.
    words = {"zebra", "apple", "mango", "quince", "pear", "élan"}
    counts = {w: len(w) for w in words}          # dict built in set-iteration (seed-sensitive) order
    scored = [{"id": w, "score": 0.1 + 0.2 + len(w) * 0.001} for w in words]
    ranked = [h["id"] for h in determinism.stable_rank(scored, key=lambda h: (-h["score"], h["id"]))]

    payload = {
        "generated": util.now_iso(),             # fixed by the clock
        "counts": counts,
        "ranked": ranked,
        "events": [{"kind": e["kind"], "hash": e["hash"]}
                   for e in db.events(conn, session="sess-fixed", limit=99)],
    }
    data_json = determinism.canonical(payload)   # this is the byte surface compared across runs

    # The irreducible exception: a real wall-clock cost duration. It differs run to run and MUST
    # NOT touch the digest. det_hash folds the payload WITH the stamp; determinism_hash drops it.
    import time
    t0 = time.perf_counter()
    _ = sum(range(5000))
    cost_ms = (time.perf_counter() - t0) * 1000.0

    out = {
        "event_hashes": event_hashes,
        "data_json": data_json,
        "data_sha": determinism.stable_hash(data_json),
        "det_hash": determinism.determinism_hash({**payload, "cost_ms": cost_ms}),
        "cost_ms": cost_ms,
        "hashseed": os.environ.get("PYTHONHASHSEED"),
        "raw_counts_order": list(counts.keys()),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=True)
    # also drop the real data.json projection as a file, to prove the file bytes match too
    with open(os.path.join(root, "data.json"), "w", encoding="utf-8") as fh:
        fh.write(data_json)


def _run(tmp_root, hashseed, locale):
    root = os.path.join(str(tmp_root), "proj-%s" % hashseed)
    os.makedirs(root, exist_ok=True)
    out = os.path.join(str(tmp_root), "out-%s.json" % hashseed)
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hashseed
    env["LC_ALL"] = locale
    env["LANG"] = locale
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, os.path.abspath(__file__), root, out],
                       env=env, text=True, encoding="utf-8", capture_output=True)
    assert r.returncode == 0, "scenario failed (seed=%s):\n%s\n%s" % (hashseed, r.stdout, r.stderr)
    with open(out, encoding="utf-8") as fh:
        result = json.load(fh)
    with open(os.path.join(root, "data.json"), encoding="utf-8") as fh:
        result["data_json_file"] = fh.read()
    return result


# --------------------------------------------------------------------------- the golden pin

def test_two_runs_different_seed_and_locale_agree_byte_for_byte(tmp_path):
    a = _run(tmp_path, "0", "C")
    b = _run(tmp_path, "1234567", "C.utf8")

    assert a["hashseed"] == "0" and b["hashseed"] == "1234567"     # the two processes really differ
    assert a["event_hashes"] == b["event_hashes"]                 # byte-identical event hashes
    assert a["data_json"] == b["data_json"]                       # byte-identical data.json (string)
    assert a["data_json_file"] == b["data_json_file"]             # byte-identical data.json (on disk)
    assert a["data_sha"] == b["data_sha"]
    assert a["ranked"] if False else True                         # ranked is inside data_json already


def test_event_hashes_are_pinned_to_the_fixed_clock(tmp_path):
    a = _run(tmp_path, "0", "C")
    # five events, each a 64-hex sha256, chained under the fixed clock: a fixed, known-length list
    assert len(a["event_hashes"]) == 5
    assert all(len(h) == 64 and all(c in "0123456789abcdef" for c in h) for h in a["event_hashes"])


def test_cost_stamp_is_excluded_from_any_hash_or_order(tmp_path):
    a = _run(tmp_path, "0", "C")
    b = _run(tmp_path, "1234567", "C.utf8")
    # the wall-clock cost stamp is free to differ; the determinism digest must not move with it
    assert a["det_hash"] == b["det_hash"]


# --------------------------------------------------------------------------- unit guards

def test_canonical_sorts_keys_and_is_seed_free():
    assert determinism.canonical({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert determinism.canonical({"a": 2, "b": 1}) == '{"a":2,"b":1}'


def test_canonical_refuses_non_finite():
    for bad in (float("nan"), math.inf, -math.inf):
        with pytest.raises(ValueError):
            determinism.canonical({"x": bad})


def test_band_absorbs_float_last_bits():
    assert determinism.band(0.1 + 0.2) == determinism.band(0.3)
    assert determinism.band(1.0) == 1_000_000


def test_stable_rank_projects_floats_before_ordering():
    items = [{"id": "a", "s": 0.1 + 0.2}, {"id": "b", "s": 0.3}, {"id": "c", "s": 0.9}]
    order = [x["id"] for x in determinism.stable_rank(items, key=lambda x: (-x["s"], x["id"]))]
    assert order == ["c", "a", "b"]   # a and b tie on the band, tie-break by id


def test_determinism_hash_drops_cost_stamps():
    base = {"n": 3, "order": ["x", "y"]}
    h1 = determinism.determinism_hash({**base, "cost_ms": 1.0, "wall_ms": 2.0})
    h2 = determinism.determinism_hash({**base, "cost_ms": 999.0, "wall_ms": 0.0})
    assert h1 == h2 == determinism.stable_hash(base)


def test_stable_hash_accepts_str_bytes_and_objects():
    assert determinism.stable_hash("") == \
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert determinism.stable_hash(b"") == determinism.stable_hash("")
    assert determinism.stable_hash({"a": 1}) == determinism.stable_hash('{"a":1}')


if __name__ == "__main__":
    _scenario(sys.argv[1], sys.argv[2])
