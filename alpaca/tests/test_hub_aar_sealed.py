"""(cause C1, C2) "AAR sealed" means the latest seal still holds: the report on disk hashes to it
(aar._seal_cut). Ways this fails after it lands:

  * the hub reads `aar.latest_seal(conn, op)` non-None as sealed, so a report edited or deleted after
    its seal still shows "AAR sealed" while `op close` and `aar owed` say owed
  * one if/elif label hides a fact: owed with an older sealed report drops the report link
  * the "changed since event:N" note that op close prints is lost, so the owner cannot tell a new
    trigger from an edited report
"""
from alpaca.tests.hub_aar_kit import (DRAFT, OP, OWED, SEALED, ev, op_view, owe, page, report, report_refs, seal, setup, text,
                     view_text)


def test_a_holding_seal_shows_sealed_and_links_the_report(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    seal(conn, project)
    html = op_view(page(project), OP, tmp_path)
    t = text(html)
    assert SEALED.search(t), "a sealed AAR is not shown as AAR sealed:\n" + t
    assert not OWED.search(t) and not DRAFT.search(t), t
    assert report_refs(html, OP), "the op view has no link to the sealed report"


def test_a_seal_that_answered_a_trigger_shows_sealed(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    owe(conn)
    seal(conn, project)
    t = view_text(project, OP, tmp_path)
    assert SEALED.search(t) and not OWED.search(t), t


def test_an_edit_after_the_seal_owes_again_with_the_note(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    owe(conn)
    sid = seal(conn, project)
    with open(report(project), "a", encoding="utf-8") as fh:
        fh.write("\nedited after the seal\n")
    t = view_text(project, OP, tmp_path)
    assert OWED.search(t), "the report changed after its seal, aar owed lists the op again, the hub does not:\n" + t
    assert not SEALED.search(t), "the hub still says AAR sealed for a report that no longer hashes"
    assert "changed" in t.lower() and ev(t, sid), "the note 'the sealed report changed or is gone since event:%d' is lost" % sid


def test_an_edit_with_no_trigger_is_not_sealed(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    seal(conn, project)
    with open(report(project), "a", encoding="utf-8") as fh:
        fh.write("\nedited after the seal\n")
    assert not SEALED.search(view_text(project, OP, tmp_path))


def test_a_deleted_report_is_not_sealed(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    owe(conn)
    seal(conn, project)
    report(project).unlink()
    t = view_text(project, OP, tmp_path)
    assert not SEALED.search(t) and OWED.search(t), t


def test_owed_after_a_seal_keeps_the_older_report_linked(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    owe(conn)
    seal(conn, project)
    owe(conn, reason="a second gap found after the seal")
    html = op_view(page(project), OP, tmp_path)
    assert OWED.search(text(html)), text(html)
    assert report_refs(html, OP), "owed again hides the link to the report that is still sealed on disk"
