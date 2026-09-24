"""alpaca note: the inbox for raw pieces (docs/interview.md).

A note is kept as it was given, in input/notes/<UTC stamp>-<sha12>.md: a small front matter, then
the body byte for byte. It is never rewritten; the same bytes again are recognised and not written
twice. Each new note records one `note` event.
"""
import hashlib
import io
import json
import os
import re
import sys

import pytest

NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}\.md$")


@pytest.fixture
def clock(monkeypatch):
    from alpaca import util
    now = {"t": "2026-09-24T10:00:00+07:00"}
    monkeypatch.setattr(util, "_CLOCK", lambda: now["t"])
    return now


def _cli(argv, capsys):
    from alpaca import cli
    rc = cli.main(argv)
    out = capsys.readouterr().out
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, out


def _split(path):
    raw = open(path, "rb").read()
    assert raw.startswith(b"---\n"), raw[:40]
    end = raw.index(b"\n---\n", 4)
    meta = dict(line.split(": ", 1) for line in raw[4:end].decode("utf-8").split("\n"))
    return meta, raw[end + 5:]


def _notes(project):
    base = os.path.join(project, "input", "notes")
    return sorted(os.listdir(base)) if os.path.isdir(base) else []


def _events(project, kind="note"):
    from alpaca import db
    return db.events(db.connect(project), kind=kind)


def test_add_keeps_the_text_as_given_and_records_one_event(project, capsys, clock):
    text = "  the redirect felt slow\nmaybe 50ms?  \n\n"
    rc, out = _cli(["note", "add", text, "--json"], capsys)
    assert rc == 0, out
    names = _notes(project)
    assert len(names) == 1 and NAME.match(names[0]), names
    assert names[0].startswith("20260924T030000Z-")          # the stamp is UTC
    meta, body = _split(os.path.join(project, "input", "notes", names[0]))
    assert body == text.encode("utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert meta["sha256"] == sha and meta["from"] == "text" and meta["time"] == clock["t"]
    assert meta["by"]
    assert names[0].endswith("-%s.md" % sha[:12])
    assert out["file"] == "input/notes/%s" % names[0] and out["new"] is True
    ev = _events(project)
    assert len(ev) == 1
    assert ev[0]["data"]["file"] == out["file"]
    assert ev[0]["data"]["sha256"] == sha and ev[0]["data"]["bytes"] == len(text.encode("utf-8"))


def test_add_from_a_file_keeps_every_byte(project, capsys, clock):
    raw = b"caf\xc3\xa9 line one\r\nno newline at the end"
    src = os.path.join(project, "chat-export.txt")
    with open(src, "wb") as fh:
        fh.write(raw)
    rc, out = _cli(["note", "add", "--file", src, "--json"], capsys)
    assert rc == 0, out
    meta, body = _split(os.path.join(project, out["file"]))
    assert body == raw
    assert meta["from"] == "file:chat-export.txt"
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest()


def test_add_from_stdin(project, capsys, clock, monkeypatch):
    raw = b"pasted from a call\nwith two lines\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))
    rc, out = _cli(["note", "add", "-", "--json"], capsys)
    assert rc == 0, out
    meta, body = _split(os.path.join(project, out["file"]))
    assert body == raw and meta["from"] == "stdin"


def test_the_same_bytes_again_are_not_written_twice(project, capsys, clock):
    rc, first = _cli(["note", "add", "one piece", "--json"], capsys)
    assert rc == 0
    path = os.path.join(project, first["file"])
    before = open(path, "rb").read()
    clock["t"] = "2026-09-24T11:00:00+07:00"
    rc, out = _cli(["note", "add", "one piece"], capsys)
    assert rc == 0, out
    assert "already have %s" % first["file"] in out
    assert _notes(project) == [os.path.basename(path)]
    assert open(path, "rb").read() == before                 # never rewritten
    assert len(_events(project)) == 1                         # no second event
    rc, other = _cli(["note", "add", "one piece.", "--json"], capsys)
    assert rc == 0 and other["new"] is True and other["file"] != first["file"]
    assert len(_notes(project)) == 2 and len(_events(project)) == 2


def test_an_empty_note_is_refused(project, capsys, clock):
    for text in ("", "   \n\t\n"):
        rc, out = _cli(["note", "add", text], capsys)
        assert rc == 1, (text, out)
        assert "empty" in out
    empty = os.path.join(project, "empty.txt")
    open(empty, "w").close()
    rc, out = _cli(["note", "add", "--file", empty], capsys)
    assert rc == 1, out
    assert _notes(project) == []
    assert _events(project) == []


def test_text_and_file_together_is_a_usage_error(project, capsys, clock):
    src = os.path.join(project, "a.txt")
    with open(src, "w") as fh:
        fh.write("x")
    rc, _ = _cli(["note", "add", "text", "--file", src], capsys)
    assert rc == 64
    rc, _ = _cli(["note", "add"], capsys)
    assert rc == 64
    assert _notes(project) == []


def test_list_shows_the_notes_in_time_order_with_their_first_line(project, capsys, clock):
    clock["t"] = "2026-09-24T12:00:00+00:00"
    _cli(["note", "add", "\n\nsecond piece, first line\nmore"], capsys)
    clock["t"] = "2026-09-24T09:00:00+00:00"
    _cli(["note", "add", "first piece"], capsys)
    rc, out = _cli(["note", "list", "--json"], capsys)
    assert rc == 0, out
    assert [n["first_line"] for n in out["notes"]] == ["first piece", "second piece, first line"]
    assert [n["time"] for n in out["notes"]] == ["2026-09-24T09:00:00+00:00", "2026-09-24T12:00:00+00:00"]
    for n in out["notes"]:
        assert n["file"].startswith("input/notes/") and n["sha256"] and n["bytes"] > 0
    rc, text = _cli(["note", "list"], capsys)
    assert rc == 0
    assert text.index("first piece") < text.index("second piece, first line")


def test_list_with_no_notes(project, capsys):
    rc, out = _cli(["note", "list", "--json"], capsys)
    assert rc == 0 and out["notes"] == []


def test_adding_a_note_file_again_finds_the_note(project, capsys, clock):
    """`alpaca note add --file input/notes/<note>.md` (a kept note, front matter and all) is the
    note already kept: `already have`, and no note that wraps a note."""
    rc, first = _cli(["note", "add", "raw idea: a link shortener", "--json"], capsys)
    assert rc == 0, first
    rc, again = _cli(["note", "add", "--file", os.path.join(project, first["file"]), "--json"], capsys)
    assert rc == 0, again
    assert again["new"] is False and again["file"] == first["file"]
    assert len(_notes(project)) == 1
