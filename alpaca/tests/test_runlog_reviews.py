"""Log reviews: quotes are checked against the log bytes, kept write-once, marked by events."""
import pytest

from alpaca import db
from alpaca.runlog import quotes, reviews
from test_runlog_quotes import LOG, RID, review

KIND, MARK_KIND = "log-review", "log-review-mark"


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    db.connect(str(root)).close()
    logs = root / "runs" / "logs"
    logs.mkdir(parents=True)
    (logs / (RID + ".log")).write_text(LOG)
    return root


def where(root):
    """The caller's locations: a log resolver and this reference's review folder."""
    return {"log": lambda ref: quotes.log_path(root / "runs" / "logs", ref),
            "folder": root / "runs" / "reviews" / RID}


def submit(root, data, **kw):
    return reviews.submit(root, RID, data, kind=KIND, **where(root), **kw)


def mark(root, ident, finding, verdict, **kw):
    return reviews.mark(root, RID, ident, finding, verdict, folder=where(root)["folder"],
                        kind=MARK_KIND, **kw)


def shown(root):
    return reviews.reviews(root, RID, folder=where(root)["folder"], kind=KIND, mark_kind=MARK_KIND)


def test_submit_keeps_a_checked_review_write_once_and_records_it(root):
    result = submit(root, review(), session="s-test")
    assert result["verdict"] == "PASS" and result["findings"] == 2
    path = root / result["path"]
    assert path.is_file() and not (path.stat().st_mode & 0o222)
    with pytest.raises(ValueError, match="already kept"):
        submit(root, review(), session="s-test")
    kept = shown(root)["reviews"]
    assert len(kept) == 1 and kept[0]["intact"] and kept[0]["findings"][0]["mark"] is None


def test_a_quote_not_in_its_lines_rejects_the_whole_review(root):
    bad = review()
    bad["findings"][1]["quote"] = "Error 1 was a network timeout"
    with pytest.raises(ValueError, match="finding 2: quote is not in lines 5 to 5"):
        submit(root, bad)
    moved = review()
    moved["findings"][0]["line"] = 2
    with pytest.raises(ValueError, match="finding 1"):
        quotes.check(where(root)["log"](RID), moved, ref=RID)
    assert not (root / "runs" / "reviews").exists()


def test_marks_append_and_the_newest_is_current(root):
    ident = submit(root, review())["review"]
    mark(root, ident, "f1", "disputed", note="wrong line", by="eng")
    mark(root, ident, "f1", "confirmed", note="rechecked", by="eng")
    with pytest.raises(ValueError, match="no finding f9"):
        mark(root, ident, "f9", "confirmed")
    f1 = shown(root)["reviews"][0]["findings"][0]
    assert f1["mark"]["mark"] == "confirmed" and f1["mark"]["note"] == "rechecked"
    conn = db.connect(str(root))
    try:
        kinds = [e["kind"] for e in db.events(conn, limit=20)]
    finally:
        conn.close()
    assert kinds.count(MARK_KIND) == 2 and kinds.count(KIND) == 1


def test_an_edited_kept_file_reads_as_not_intact(root):
    result = submit(root, review())
    path = root / result["path"]
    path.chmod(0o644)
    path.write_text(path.read_text().replace("internal assertion", "harmless note"))
    assert shown(root)["reviews"][0]["intact"] is False


def test_a_review_folder_outside_the_root_is_refused_before_anything_is_written(root, tmp_path):
    outside = tmp_path / "elsewhere"
    with pytest.raises(ValueError, match="not inside"):
        reviews.submit(root, RID, review(), kind=KIND, log=where(root)["log"], folder=outside)
    assert not outside.exists()
