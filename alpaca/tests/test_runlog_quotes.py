"""Log reviews: quotes are checked against the log bytes and the log sha256 before they are kept."""
import hashlib

import pytest

from alpaca.runlog import quotes

RID = "b" * 32
LOG = ("1. Reading the source tree.\n"
       "Warning: Replacing cache entry with a fresh copy.\n"
       "3.8. Running the optimizer (performing simple passes).\n"
       "ERROR: Assert `cache_keys.count(x)' failed in core/store.cc:2642.\n"
       "make[1]: *** [Makefile:297: do-build] Error 1\n")


@pytest.fixture
def logs(tmp_path):
    logs = tmp_path / "project" / "logs"
    logs.mkdir(parents=True)
    (logs / (RID + ".log")).write_text(LOG)
    return logs


def review(**over):
    data = {"receipt": RID, "log_sha256": hashlib.sha256(LOG.encode()).hexdigest(), "reviewer": "test-agent",
            "summary": "The build stopped on an internal assertion at line 4 during the optimizer; make then failed.",
            "outcome": "matches-verdict",
            "findings": [{"line": 4, "severity": "error", "quote": "Assert `cache_keys.count(x)' failed",
                          "meaning": "The build hit an internal assertion while cleaning the cache.",
                          "check": "Confirm the cache key list"},
                         {"line": 5, "end_line": 5, "severity": "error", "quote": "do-build] Error 1",
                          "meaning": "make stopped because the build step failed."}]}
    data.update(over)
    return data


def test_a_checked_review_keeps_every_finding_with_the_log_hash(logs):
    kept = quotes.check(quotes.log_path(logs, RID), review(), ref=RID)
    assert kept["receipt"] == RID
    assert kept["log_sha256"] == hashlib.sha256(LOG.encode()).hexdigest()
    assert kept["log_lines"] == 5 and kept["outcome"] == "matches-verdict"
    assert [f["id"] for f in kept["findings"]] == ["f1", "f2"]
    assert kept["findings"][0]["check"] == "Confirm the cache key list"
    assert "check" not in kept["findings"][1]


def test_a_changed_log_or_bad_shape_is_refused(logs):
    log = quotes.log_path(logs, RID)
    with pytest.raises(ValueError, match="log_sha256"):
        quotes.check(log, review(log_sha256="0" * 64), ref=RID)
    with pytest.raises(ValueError, match="outcome"):
        quotes.check(log, review(outcome="pass"), ref=RID)
    far = review()
    far["findings"][0]["end_line"] = 99
    with pytest.raises(ValueError, match="outside the log"):
        quotes.check(log, far, ref=RID)
    with pytest.raises(ValueError, match="no captured log"):
        quotes.log_path(logs, "../../etc/passwd")
    (logs / "linked.log").symlink_to(log)
    with pytest.raises(ValueError, match="no captured log"):
        quotes.log_path(logs, "linked")


def test_a_review_of_another_reference_is_refused(logs):
    log = quotes.log_path(logs, RID)
    with pytest.raises(ValueError, match="review names receipt"):
        quotes.check(log, review(receipt="c" * 32), ref=RID)
    # negative: a review that names no reference is taken as about the one checked.
    unnamed = review()
    del unnamed["receipt"]
    assert quotes.check(log, unnamed, ref=RID)["receipt"] == RID
    # the reference key is the caller's.
    assert quotes.check(log, review(job="j1"), ref="j1", ref_key="job")["job"] == "j1"
