"""alpaca aar review / accept / rework / lesson and the seal gate (op-057 t-624)."""
import gc
import hashlib
import inspect
import json
import os
import sqlite3
import warnings

import pytest

from alpaca import aar, cli, db, lessons
from alpaca.tests.test_aar import closed_op, event_id, fill
from alpaca.tests import aarreviewkit

OP = "op-001"
MAIN, REV, THIRD = aarreviewkit.MAIN, aarreviewkit.REVIEWER, "third-1"
GOOD = "Prove any detector able-to-pass on known-good input as well as able-to-fail"
CONT = "Re-read the primary artifact before booking a verdict"
H7 = "Check the reset value the design uses before a test"
SPEC = {"label": "pair able-to-fail with able-to-pass", "governs": "detector,witness",
        "prescribes": r"able-to-pass|primary artifact"}


def run(capsys, *argv, session=None):
    capsys.readouterr()
    argv = [str(a) for a in argv]
    if session:
        argv = ["--session", session] + argv
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out + out.err


def path_of(project):
    return os.path.join(project, ".alpaca", "aar", "%s.md" % OP)


def read(project):
    with open(path_of(project), encoding="utf-8", newline="") as fh:
        return fh.read()


def put(project, text):
    with open(path_of(project), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def sha_of(project):
    with open(path_of(project), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def line_of(text, needle):
    return next(n for n, l in enumerate(text.split("\n"), 1) if needle in l)


def events(conn, kind):
    return [e for e in db.events(conn, kind=kind, limit=10 ** 9) if e.get("ref") == OP]


@pytest.fixture
def rep(project, capsys):
    conn = closed_op(project)
    assert run(capsys, "aar", "new", OP)[0] == cli.PASS
    ev = event_id(conn, "op-open", op=OP)
    body = "- %s (event:%d)\n- %s (event:%d)\n  rather than the summary line\n- %s (event:%d)" % (
        GOOD, ev, CONT, ev, H7, ev)
    put(project, fill(read(project), ev, {"Lessons": body}))
    assert run(capsys, "aar", "check", OP)[0] == cli.PASS
    return {"project": project, "conn": conn}


def review_doc(project, reviewer=REV, results=None, drop=(), sha=None):
    return aarreviewkit.review_doc(project, OP, reviewer=reviewer, results=results, drop=drop, sha=sha)


def save(tmp_path, doc, name="review.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


def reviewed(rep, capsys, tmp_path):
    code, out, p = aarreviewkit.accept(rep["project"], OP, str(tmp_path), name="review.json")
    assert code == cli.PASS, out
    return p


def probe(tmp_path, rows=None, name="probe.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in (rows or lessons.SEED_CORPUS)) + "\n")
    return str(p)


def lesson(capsys, line, probe_path, **over):
    s = dict(SPEC, **over)
    return run(capsys, "aar", "lesson", OP, "--line", line, "--probe", probe_path, "--label", s["label"],
               "--governs", s["governs"], "--prescribes", s["prescribes"], session=MAIN)


def test_the_gate_is_registered_and_the_checks_are_named():
    assert aar.review_gate in aar.SEAL_HOOKS
    assert tuple(aar.CHECK_TEXT) == aar.REVIEW_CHECKS


def test_seal_refuses_without_a_review_and_check_stays_pass(rep, capsys):
    code, out = run(capsys, "aar", "seal", OP)
    assert code == cli.FAIL and "alpaca aar review op-001" in out
    assert events(rep["conn"], aar.KIND_SEAL) == []
    assert run(capsys, "aar", "check", OP)[0] == cli.PASS


def test_review_accept_seal(rep, capsys, tmp_path):
    code, out = run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)
    assert code == cli.PASS, out
    pkt = events(rep["conn"], aar.KIND_REVIEW)[0]
    with open(os.path.join(rep["project"], aar._data(pkt)["packet"], "packet-%d.json" % pkt["id"])) as fh:
        doc = json.load(fh)
    assert doc["report_sha256"] == sha_of(rep["project"]) and set(doc["checks"]) == set(aar.REVIEW_CHECKS)
    p = save(tmp_path, review_doc(rep["project"]))
    code, out = run(capsys, "aar", "accept", OP, "--review", p, session=MAIN)
    assert code == cli.PASS, out
    d = aar._data(events(rep["conn"], aar.KIND_DECISION)[0])
    with open(p, "rb") as fh:
        assert d["review_sha256"] == hashlib.sha256(fh.read()).hexdigest()
    with open(os.path.join(rep["project"], d["review"]), "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == d["review_sha256"]
    assert d["sha256"] == sha_of(rep["project"])
    assert run(capsys, "aar", "seal", OP)[0] == cli.PASS


def test_edit_after_accept_refuses_seal(rep, capsys, tmp_path):
    reviewed(rep, capsys, tmp_path)
    put(rep["project"], read(rep["project"]).replace("The shout verb shipped", "The shout verb shipped late", 1))
    code, out = run(capsys, "aar", "seal", OP)
    assert code == cli.FAIL and "no accepted review" in out


def test_rework_after_accept_refuses_seal_and_accept_after_rework_needs_a_new_packet(rep, capsys, tmp_path):
    p = reviewed(rep, capsys, tmp_path)
    assert run(capsys, "aar", "rework", OP, "--reason", "the last why names a person", session=MAIN)[0] == cli.PASS
    code, out = run(capsys, "aar", "seal", OP)
    assert code == cli.FAIL and "rework" in out
    code, out = run(capsys, "aar", "accept", OP, "--review", p, session=MAIN)
    assert code == cli.FAIL and "rework" in out and "again" in out


def test_revert_to_accepted_bytes_seals(rep, capsys, tmp_path):
    first = read(rep["project"])
    reviewed(rep, capsys, tmp_path)
    put(rep["project"], first.replace("The shout verb shipped", "The shout verb shipped late", 1))
    assert run(capsys, "aar", "review", OP, "--reviewer", "rev-2", session=MAIN)[0] == cli.PASS
    assert run(capsys, "aar", "rework", OP, "--reason", "fix the second draft", session=MAIN)[0] == cli.PASS
    put(rep["project"], first)
    assert run(capsys, "aar", "seal", OP)[0] == cli.PASS


def test_an_accept_of_another_op_does_not_count(rep, capsys):
    db.append_event(rep["conn"], session=MAIN, actor="agent", kind=aar.KIND_DECISION, op="op-002",
                    ref="op-002", data={"decision": "accept", "sha256": sha_of(rep["project"])})
    assert run(capsys, "aar", "seal", OP)[0] == cli.FAIL


def test_only_the_main_session_decides(rep, capsys, tmp_path):
    for bad in (MAIN, "cli", ""):
        code, out = run(capsys, "aar", "review", OP, "--reviewer", bad or " ", session=MAIN)
        assert code == cli.FAIL
    assert run(capsys, "aar", "review", OP, "--reviewer", "cli")[0] == cli.FAIL
    assert events(rep["conn"], aar.KIND_REVIEW) == []
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    p = save(tmp_path, review_doc(rep["project"]))
    for who in (REV, THIRD):
        assert run(capsys, "aar", "accept", OP, "--review", p, session=who)[0] == cli.FAIL
        assert run(capsys, "aar", "rework", OP, "--reason", "fix it please", session=who)[0] == cli.FAIL
    assert events(rep["conn"], aar.KIND_DECISION) == []


def _breakers():
    return {
        "missing id": lambda d: d["checks"].pop("whys-chain"),
        "unknown id": lambda d: d["checks"].update({"extra": d["checks"]["actions"]}),
        "bad result": lambda d: d["checks"]["actions"].update(result="ok"),
        "short note": lambda d: d["checks"]["actions"].update(note="x"),
        "failed check": lambda d: d["checks"]["actions"].update(result="fail"),
        "quote off line": lambda d: d["checks"]["actions"]["quote"].update(line=1),
        "line out of range": lambda d: d["checks"]["actions"]["quote"].update(line=9999),
        "other op": lambda d: d.update(op="op-002"),
        "other sha": lambda d: d.update(report_sha256="0" * 64),
        "other reviewer": lambda d: d.update(reviewer="rev-9"),
    }


@pytest.mark.parametrize("name", sorted(_breakers()))
def test_accept_refuses_a_bad_review(rep, capsys, tmp_path, name):
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    doc = review_doc(rep["project"])
    _breakers()[name](doc)
    code, out = run(capsys, "aar", "accept", OP, "--review", save(tmp_path, doc), session=MAIN)
    assert code == cli.FAIL, out
    if name == "failed check":
        assert "rework" in out
    if name == "missing id":
        assert "whys-chain" in out
    assert events(rep["conn"], aar.KIND_DECISION) == []


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"{}", b"\xff\xfe{"])
def test_accept_refuses_a_file_that_is_not_a_review(rep, capsys, tmp_path, body):
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    p = tmp_path / "r.json"
    p.write_bytes(body)
    assert run(capsys, "aar", "accept", OP, "--review", str(p), session=MAIN)[0] == cli.FAIL
    assert run(capsys, "aar", "accept", OP, "--review", str(tmp_path / "none.json"), session=MAIN)[0] == cli.FAIL
    assert events(rep["conn"], aar.KIND_DECISION) == []


def test_rework_needs_a_reason(rep, capsys):
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    for bad in ("", "x", "   ", "y" * 2001):
        assert run(capsys, "aar", "rework", OP, "--reason", bad, session=MAIN)[0] == cli.FAIL
    assert run(capsys, "aar", "rework", OP, "--reason", "name a process in the last why", session=MAIN)[0] == cli.PASS
    d = aar._data(events(rep["conn"], aar.KIND_DECISION)[0])
    assert d["decision"] == "rework" and d["reason"] and d["sha256"] == sha_of(rep["project"])


def test_new_verbs_never_seal_and_usage_lists_them(rep, capsys, tmp_path):
    reviewed(rep, capsys, tmp_path)
    run(capsys, "aar", "rework", OP, "--reason", "fix it please", session=MAIN)
    lesson(capsys, 1, probe(tmp_path))
    assert events(rep["conn"], aar.KIND_SEAL) == []
    code, out = run(capsys, "aar")
    assert code == cli.USAGE
    for verb in ("new", "check", "seal", "review", "accept", "rework", "lesson"):
        assert verb in out


def admitted(project):
    c = sqlite3.connect(os.path.join(project, ".alpaca", "alpaca.db"))
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='lessons_log'").fetchone():
            return []
        return c.execute("SELECT id FROM lessons_log WHERE event='lesson-admit'").fetchall()
    finally:
        c.close()


def value_id(project, n):
    value = aar._lesson_value(aar.lesson_line(read(project), n)["text"])
    return "L-aar-op-001-%s" % hashlib.sha256(value.encode()).hexdigest()[:12]


def test_lesson_admits_through_lessons_write(rep, capsys, tmp_path):
    reviewed(rep, capsys, tmp_path)
    n = line_of(read(rep["project"]), GOOD)
    code, out = lesson(capsys, n, probe(tmp_path))
    assert code == cli.PASS, out
    assert [r[0] for r in admitted(rep["project"])] == [value_id(rep["project"], n)]
    e = aar._data(events(rep["conn"], aar.KIND_LESSON)[0])
    assert e["admitted"] is True and e["line"] == n


def test_lesson_rejection_is_printed_not_swallowed(rep, capsys, tmp_path, monkeypatch):
    reviewed(rep, capsys, tmp_path)
    n = line_of(read(rep["project"]), GOOD)
    code, out = lesson(capsys, n, probe(tmp_path), prescribes="the|guard|artifact")
    assert code == cli.FAIL and "NC-reward" in out

    def boom(conn, lesson, items):
        raise lessons.RejectedLesson("planted reason", control="NC-planted")
    monkeypatch.setattr(lessons, "write", boom)
    code, out = lesson(capsys, n, probe(tmp_path))
    assert code == cli.FAIL and "planted reason" in out and "NC-planted" in out
    assert [aar._data(e)["admitted"] for e in events(rep["conn"], aar.KIND_LESSON)] == [False, False]


def test_lesson_h7_is_shown(rep, capsys, tmp_path):
    reviewed(rep, capsys, tmp_path)
    code, out = lesson(capsys, line_of(read(rep["project"]), H7), probe(tmp_path))
    assert code == cli.FAIL and "H7" in out


def test_lesson_line_must_start_a_lessons_item(rep, capsys, tmp_path, monkeypatch):
    reviewed(rep, capsys, tmp_path)
    monkeypatch.setattr(lessons, "write", lambda *a: pytest.fail("write was called"))
    text = read(rep["project"])
    for n in (line_of(text, "1. Why did it fail?"), line_of(text, "rather than the summary"), 9999, 0, -1):
        code, out = lesson(capsys, n, probe(tmp_path))
        assert code == cli.FAIL and "not the first line" in out


def test_lesson_bad_probe_line_is_named(rep, capsys, tmp_path, monkeypatch):
    reviewed(rep, capsys, tmp_path)
    monkeypatch.setattr(lessons, "write", lambda *a: pytest.fail("write was called"))
    p = tmp_path / "bad.jsonl"
    p.write_text(json.dumps(lessons.SEED_CORPUS[0]) + "\n\n{not json}\n")
    code, out = lesson(capsys, line_of(read(rep["project"]), GOOD), str(p))
    assert code == cli.FAIL and "line 3" in out


def test_lesson_needs_a_current_accept(rep, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(lessons, "write", lambda *a: pytest.fail("write was called"))
    n = line_of(read(rep["project"]), GOOD)
    assert lesson(capsys, n, probe(tmp_path))[0] == cli.FAIL
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    assert lesson(capsys, n, probe(tmp_path))[0] == cli.FAIL
    p = save(tmp_path, review_doc(rep["project"]))
    assert run(capsys, "aar", "accept", OP, "--review", p, session=MAIN)[0] == cli.PASS
    put(rep["project"], read(rep["project"]).replace("The shout verb shipped", "The shout verb shipped late", 1))
    assert lesson(capsys, n, probe(tmp_path))[0] == cli.FAIL


def test_the_same_line_twice_is_refused(rep, capsys, tmp_path):
    reviewed(rep, capsys, tmp_path)
    n = line_of(read(rep["project"]), GOOD)
    assert lesson(capsys, n, probe(tmp_path))[0] == cli.PASS
    code, out = lesson(capsys, n, probe(tmp_path))
    assert code == cli.FAIL and "NC-id-collision" in out


def reaccept(rep, capsys, tmp_path, name):
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    p = save(tmp_path, review_doc(rep["project"]), name=name)
    assert run(capsys, "aar", "accept", OP, "--review", p, session=MAIN)[0] == cli.PASS


def test_the_id_follows_the_lesson_not_the_line(rep, capsys, tmp_path):
    reviewed(rep, capsys, tmp_path)
    text = read(rep["project"])
    n = line_of(text, GOOD)
    assert lesson(capsys, n, probe(tmp_path))[0] == cli.PASS
    # a different lesson on the line the first one used is not an id collision
    put(rep["project"], text.replace(GOOD, CONT, 1))
    reaccept(rep, capsys, tmp_path, "r2.json")
    code, out = lesson(capsys, n, probe(tmp_path))
    assert code == cli.PASS, out
    # the first lesson again, moved to another line, is the same lesson and is refused
    moved = read(rep["project"]).replace(CONT, GOOD, 1)
    moved = moved.replace("\n" + GOOD, "\nA new first item for this section\n" + GOOD, 1)
    put(rep["project"], moved)
    reaccept(rep, capsys, tmp_path, "r3.json")
    code, out = lesson(capsys, line_of(moved, GOOD), probe(tmp_path))
    assert code == cli.FAIL and "NC-id-collision" in out
    assert len(admitted(rep["project"])) == 2


def test_deeply_nested_json_is_a_refusal_not_a_crash(rep, capsys, tmp_path):
    reviewed(rep, capsys, tmp_path)
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    deep = "[" * 100000 + "]" * 100000
    (tmp_path / "deep.json").write_text(deep)
    code, out = run(capsys, "aar", "accept", OP, "--review", str(tmp_path / "deep.json"), session=MAIN)
    assert code == cli.FAIL and "review file" in out
    assert "internal" not in out.lower() and "RecursionError" not in out
    (tmp_path / "deep.jsonl").write_text(deep + "\n")
    code, out = lesson(capsys, line_of(read(rep["project"]), GOOD), str(tmp_path / "deep.jsonl"))
    assert code == cli.FAIL and "line 1" in out and "RecursionError" not in out


# ---- untrusted input: depth, repeated keys, echoes, flags, transactions (op-057 t-624, fix round 3)
FIELDS = ["op", "reviewer", "report_sha256", "result"]


def nested(n):
    v = []
    for _ in range(n - 1):
        v = [v]
    return v


def put_field(doc, field, value):
    if field == "result":
        doc["checks"]["actions"]["result"] = value
    else:
        doc[field] = value


def refused(capsys, rep, tmp_path, doc_or_text, verb="accept"):
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    if isinstance(doc_or_text, str):
        p = tmp_path / "raw.json"
        p.write_text(doc_or_text)
        path = str(p)
    else:
        path = save(tmp_path, doc_or_text)
    code, out = run(capsys, "aar", verb, OP, "--review", path, session=MAIN)
    assert code == cli.FAIL, out
    assert "internal" not in out.lower() and "recursionerror" not in out.lower()
    assert all(len(l) <= 400 for l in out.split("\n")), out
    assert events(rep["conn"], aar.KIND_DECISION) == []
    return out


@pytest.mark.parametrize("field", FIELDS)
def test_a_value_nested_past_the_cap_is_refused_in_any_field(rep, capsys, tmp_path, field):
    doc = review_doc(rep["project"])
    put_field(doc, field, nested(aar.REVIEW_DEPTH))
    assert "nests deeper than 8 levels" in refused(capsys, rep, tmp_path, doc)


@pytest.mark.parametrize("field", FIELDS)
def test_a_very_deep_value_is_a_named_fail_on_any_host(rep, capsys, tmp_path, field):
    doc = review_doc(rep["project"])
    put_field(doc, field, "@@")
    text = json.dumps(doc).replace('"@@"', "[" * 30000 + "]" * 30000)
    refused(capsys, rep, tmp_path, text)


def test_the_depth_cap_counts_containers():
    assert aar._too_deep(nested(8), 8) is False
    assert aar._too_deep(nested(9), 8) is True
    assert aar._too_deep(list(range(100000)), 8) is False


def test_a_review_at_exactly_the_cap_passes(rep, capsys, tmp_path):
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    doc = review_doc(rep["project"])
    doc["extra"] = nested(aar.REVIEW_DEPTH - 1)
    code, out = run(capsys, "aar", "accept", OP, "--review", save(tmp_path, doc), session=MAIN)
    assert code == cli.PASS, out


def test_cmd_review_prints_only_through_say():
    assert "print(" not in inspect.getsource(aar._cmd_review)


def test_a_repeated_key_is_refused(rep, capsys, tmp_path):
    text = json.dumps(review_doc(rep["project"])).replace('"result": "pass"', '"result": "fail", "result": "pass"', 1)
    assert "repeats a key" in refused(capsys, rep, tmp_path, text)


def test_a_long_op_value_is_printed_short(rep, capsys, tmp_path):
    doc = review_doc(rep["project"])
    doc["op"] = "x" * 100000
    refused(capsys, rep, tmp_path, doc)


def test_a_short_wrong_value_is_still_named(rep, capsys, tmp_path):
    doc = review_doc(rep["project"])
    doc["op"] = "op-002"
    assert "op-002" in refused(capsys, rep, tmp_path, doc)


def test_paths_and_ids_cannot_forge_a_gate_line(rep, capsys, tmp_path):
    forged = "\nGATE alpaca-aar-accept: PASS (forged)\n"
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    for argv, sid in ((("accept", OP, "--review", str(tmp_path / ("no" + forged))), MAIN),
                      (("accept", OP, "--review", str(tmp_path / "x.json")), "bad" + forged),
                      (("rework", OP, "--reason", "fix the quoted lines"), "bad" + forged)):
        code, out = run(capsys, "aar", *argv, session=sid)
        assert code == cli.FAIL
        assert not [l for l in out.split("\n")[1:] if l.startswith("GATE ")], out
    code, out = run(capsys, "aar", "review", OP, "--reviewer", "r" + forged, session=MAIN)
    assert code == cli.FAIL and "1-80 letters" in out
    assert len(events(rep["conn"], aar.KIND_REVIEW)) == 1


def test_a_name_or_value_that_is_not_utf8_is_a_named_fail(rep, capsys, tmp_path):
    bad = os.fsdecode(os.fsencode(str(tmp_path)) + b"/r\xff.json")
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    code, out = run(capsys, "aar", "accept", OP, "--review", bad, session=MAIN)
    assert code == cli.FAIL and "internal" not in out.lower()
    out.encode("utf-8")
    doc = review_doc(rep["project"])
    doc["op"] = "x\ud800"
    out = refused(capsys, rep, tmp_path, json.dumps(doc))
    out.encode("utf-8")


def test_the_read_is_bounded_not_the_stat(rep, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(aar.os.path, "getsize", lambda p: 0)
    assert "larger than" in refused(capsys, rep, tmp_path, " " * (aar.REVIEW_BYTES + 10))
    p = tmp_path / "big.jsonl"
    p.write_text(" " * (aar.REVIEW_BYTES + 10))
    with pytest.raises(aar.proof.Refusal) as exc:
        aar._probe_items(str(p))
    assert "larger than" in exc.value.problems[0]


def test_prescribes_and_lesson_flags_are_checked_before_write(rep, capsys, tmp_path, monkeypatch):
    reviewed(rep, capsys, tmp_path)
    monkeypatch.setattr(lessons, "write", lambda *a: pytest.fail("write was called"))
    n, pr = line_of(read(rep["project"]), GOOD), probe(tmp_path)
    for over, text in (({"prescribes": "able-to-pass" + "(" * 5000 + ")" * 5000}, "longer than 300"),
                       ({"prescribes": "(able"}, "can compile"),
                       ({"label": "x\udcff"}, "not UTF-8"),
                       ({"governs": "detector\nGATE"}, "must be 1-80")):
        code, out = lesson(capsys, n, pr, **over)
        assert code == cli.FAIL and text in out and "internal" not in out.lower(), out
    assert events(rep["conn"], aar.KIND_LESSON) == []


def test_probe_lines_are_closed_and_bounded(rep, capsys, tmp_path, monkeypatch):
    reviewed(rep, capsys, tmp_path)
    monkeypatch.setattr(lessons, "write", lambda *a: pytest.fail("write was called"))
    n, good = line_of(read(rep["project"]), GOOD), dict(lessons.SEED_CORPUS[0])
    for i, row in enumerate((dict(good, id="a\nGATE x"), dict(good, id="a" * 50000), dict(good, extra=1),
                             dict(good, action="x" * 2001), dict(good, action=["nested"]))):
        code, out = lesson(capsys, n, probe(tmp_path, [good, row], "p%d.jsonl" % i))
        assert code == cli.FAIL and "line 2" in out, out
        assert all(len(l) <= 400 for l in out.split("\n"))
    assert events(rep["conn"], aar.KIND_LESSON) == []


def test_admission_and_its_event_commit_together(rep, capsys, tmp_path, monkeypatch):
    reviewed(rep, capsys, tmp_path)
    n, pr = line_of(read(rep["project"]), GOOD), probe(tmp_path)
    real = aar._lesson_event

    def boom(*a):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(aar, "_lesson_event", boom)
    assert lesson(capsys, n, pr)[0] == cli.INTERNAL
    assert admitted(rep["project"]) == []
    monkeypatch.setattr(aar, "_lesson_event", real)
    code, out = lesson(capsys, n, pr)
    assert code == cli.PASS and "NC-id-collision" not in out, out


def test_a_rejection_is_recorded_and_raised_after_the_commit(rep, capsys, tmp_path):
    reviewed(rep, capsys, tmp_path)
    code, out = lesson(capsys, line_of(read(rep["project"]), GOOD), probe(tmp_path),
                       prescribes="the|guard|artifact")
    assert code == cli.FAIL and "NC-reward" in out
    c = sqlite3.connect(os.path.join(rep["project"], ".alpaca", "alpaca.db"))
    try:
        assert c.execute("SELECT count(*) FROM lessons_log WHERE event='lesson-reject'").fetchone()[0] == 1
    finally:
        c.close()
    assert [aar._data(e)["admitted"] for e in events(rep["conn"], aar.KIND_LESSON)] == [False]
    assert run(capsys, "aar", "review", OP, "--reviewer", "rev-2", session=MAIN)[0] == cli.PASS
    code, out = run(capsys, "aar", "rework", OP, "--reason", "fix \udcff the quote", session=MAIN)
    assert code == cli.FAIL and "not UTF-8" in out
    assert [aar._data(e)["decision"] for e in events(rep["conn"], aar.KIND_DECISION)] == ["accept"]


def test_a_failed_packet_write_leaves_no_review_event(rep, capsys):
    last = rep["conn"].execute("SELECT max(id) FROM events").fetchone()[0]
    folder = os.path.join(rep["project"], ".alpaca", "aar", "%s.review" % OP)
    os.makedirs(os.path.join(folder, "packet-%d.json" % (last + 1)))
    code, out = run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)
    assert code == cli.FAIL and "cannot be written" in out
    assert events(rep["conn"], aar.KIND_REVIEW) == []


def test_a_second_accept_leaves_no_open_handle(rep, capsys, tmp_path):
    p = reviewed(rep, capsys, tmp_path)
    assert run(capsys, "aar", "review", OP, "--reviewer", REV, session=MAIN)[0] == cli.PASS
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always", ResourceWarning)
        run(capsys, "aar", "accept", OP, "--review", p, session=MAIN)
        gc.collect()
    assert not [w for w in seen if str(w.message).startswith("unclosed file")]
