"""The label rule of alpaca/aarview.py (op-057 t-625): every combination of owed, op status, seal and
report gives one of the four labels or none, and the words never say "not owed"."""
import itertools

from alpaca import aarview

TRIG = [{"id": 905, "trigger": "aar owe", "text": "rollback took 40 min"}]


def view(due, status, seal, holds, report):
    ans = {"seal": seal, "holds": holds, "note": "the sealed report changed or is gone since event:%d" % seal if seal and not holds else "",
           "report": ".alpaca/aar/op-1.md" if report else None}
    return aarview.state({"id": "op-1", "status": status}, TRIG if due else [], ans)


def test_label_table():
    for due, status, seal, holds, report in itertools.product((0, 1), ("open", "closed"), (None, 812), (True, False), (True, False)):
        if seal is None and holds:
            continue
        s = view(due, status, seal, holds, report)
        if due:
            assert s["badge"] == "owed" and s["label"] == "AAR owed" and s["triggers"][0]["id"] == 905
            assert s.get("blocks_close") == (True if status == "open" else None)
            assert ("A draft report is on disk." in s["lines"]) == bool(report and seal is None)
        elif seal and holds:
            assert s["badge"] == "sealed"
        elif seal:
            assert s["badge"] == "changed" and "event:812" in " ".join(s["lines"])
        elif report:
            assert s["badge"] == "draft"
        else:
            assert s is None
        text = " ".join(s["lines"]) if s and "lines" in s else ""
        assert "not owed" not in text.lower()
        assert bool(s and s["label"] == "AAR sealed") == (not due and bool(seal and holds))


def test_overdue_count_and_marks():
    assert aarview.state({"id": "op-1", "status": "open"}, [], {"seal": None, "holds": False, "note": "", "report": None}, 2) == {"overdue": 2}
    assert aarview.overdue_mark({"source": "op-1", "due_by": "2026-10-01", "days_late": 1})["label"] == "Overdue 1 day, due 2026-10-01"
    m = aarview.overdue_mark({"source": "op-1", "due_by": None, "days_late": None})
    assert m["label"] == "Due not readable (see its contract)"


def test_failed_names_the_reader_on_one_line():
    text = aarview.failed("AAR state", ValueError("a\nb"))
    assert text == "AAR state could not be read: ValueError: a b"
