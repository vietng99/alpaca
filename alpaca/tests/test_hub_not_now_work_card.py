"""(class 3): the Cards layout of Work & evidence. The reason a task waits is t.deferral.reason;
t.reason is the task's WHY (work_record.py:106) and is dropped from the slim overview, so a line
copied from the blocked branch ('Blocked'+t.reason) shows the wrong text and changes after the
lazy text fetch. Nulls must not print as 'null', an unreadable deferral must say so, met is read
from the row (never computed in the browser), and free text is escaped."""
from alpaca.tests.hub_not_now_kit import MET, UNREADABLE, UNRECORDED, by_class, deferred, label, node


def delivery(tmp_path, t):
    html = node("const {renderWorkCards}=await import('./cockpit.js');out(renderWorkCards(DATA));", [t], tmp_path)
    return html, by_class(html, "work-delivery")[0].text()


def test_delivery_line_shows_the_deferral_not_the_task_why(tmp_path):
    t = deferred("t-2", reason="vendor API not out", when="after:t-1")
    t["reason"] = "TASK WHY TEXT"
    _, text = delivery(tmp_path, t)
    assert "vendor API not out" in text and "after:t-1" in text, text
    assert "TASK WHY TEXT" not in text, "the delivery line reads t.reason (the task's why): " + text
    assert "Not started" not in text, "a parked task reads 'Not started': " + text


def test_delivery_line_works_on_the_slim_row_without_reason(tmp_path):
    t = deferred("t-2", reason="vendor API not out", when="date:2026-11-01")
    t.pop("reason", None)
    _, text = delivery(tmp_path, t)
    assert "vendor API not out" in text and "date:2026-11-01" in text, text


def test_met_trigger_is_marked_from_the_row(tmp_path):
    _, text = delivery(tmp_path, deferred("t-2", when="after:t-1", met=True, met_by="t-1 is done"))
    assert MET in text.lower() and "t-1 is done" in text, text
    _, text = delivery(tmp_path, deferred("t-2", when="date:2020-01-01", met=False))
    assert MET not in text.lower(), "met computed in the browser from a past date: " + text


def test_null_reason_and_trigger_never_print_null(tmp_path):
    _, text = delivery(tmp_path, deferred("t-2", reason=None, when=None))
    assert "null" not in text and "undefined" not in text, text
    assert UNRECORDED in text.lower(), "no '(not recorded)' placeholder like CHECKLIST/RESUME: " + text


def test_missing_deferral_object_does_not_throw(tmp_path):
    t = deferred("t-2")
    t["deferral"] = None
    _, text = delivery(tmp_path, t)
    assert "null" not in text and "undefined" not in text and UNRECORDED in text.lower(), text


def test_unreadable_deferral_is_named(tmp_path):
    _, text = delivery(tmp_path, deferred("t-2", reason=None, when=None, error="ValueError"))
    assert UNREADABLE in text.lower(), text


def test_free_text_is_escaped(tmp_path):
    html, _ = delivery(tmp_path, deferred("t-2", reason='<img src=x onerror="window.x=1">', when='"><b>x</b>'))
    assert "<img" not in html and "<b>x</b>" not in html, "reason or trigger reaches the page unescaped"


def test_chip_says_the_word(tmp_path):
    html, _ = delivery(tmp_path, deferred("t-2"))
    assert any(e.text().strip() == label() for e in by_class(html, "status-chip")), "no chip labelled %r" % label()
