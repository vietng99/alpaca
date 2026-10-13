"""(cause C1, C2) "AAR draft" is a report file on disk with no seal that holds; the aar-new event
is history, not state. Ways this fails after it lands:

  * draft is keyed on the aar-new event: a deleted draft still shows draft (and links a missing
    file), a report written by hand shows nothing
  * owed and draft are one if/elif label: an op mid-work (triggers recorded, `aar new` run) shows
    only one of the two facts, hiding either that op close is blocked or that work started
  * a draft with no trigger (someone ran `aar new` on their own) is shown as owed
"""
from alpaca.tests.hub_aar_kit import DRAFT, OP, OTHER, OWED, SEALED, draft, op_view, owe, page, report, report_refs, setup, text, view_text


def test_aar_new_with_no_trigger_is_a_draft_not_owed(project, monkeypatch, tmp_path):
    setup(project, monkeypatch)
    draft()
    html = op_view(page(project), OP, tmp_path)
    t = text(html)
    assert DRAFT.search(t), "aar new ran, the hub does not show AAR draft:\n" + t
    assert not OWED.search(t) and not SEALED.search(t), t
    assert report_refs(html, OP), "the draft is not linked"


def test_a_report_written_by_hand_is_a_draft(project, monkeypatch, tmp_path):
    setup(project, monkeypatch)
    path = report(project, OTHER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# AAR op-002\n\n## At a glance\n\nwritten by hand\n", encoding="utf-8")
    assert DRAFT.search(view_text(project, OTHER, tmp_path)), "a report on disk with no aar-new event is not a draft"


def test_a_deleted_draft_is_not_a_draft(project, monkeypatch, tmp_path):
    setup(project, monkeypatch)
    draft()
    report(project).unlink()
    html = op_view(page(project), OP, tmp_path)
    assert not DRAFT.search(text(html)), "the draft file is gone, the hub still shows AAR draft (keyed on aar-new)"
    assert not report_refs(html, OP), "the view links a report that is not on disk"


def test_owed_and_draft_both_show(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    owe(conn)
    draft()
    t = view_text(project, OP, tmp_path)
    assert OWED.search(t), "owed is hidden by the draft:\n" + t
    assert "draft" in t.lower(), "the draft is hidden by owed:\n" + t
