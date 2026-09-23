"""Following a growing log one byte window at a time, cut on whole UTF-8 characters."""
from alpaca.runlog import textwindow


def _write(path, data):
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def test_the_follow_returns_only_what_was_appended(tmp_path):
    path = _write(tmp_path / "job.log", "first line\nsecond line\n")
    first = textwindow.read_window(path, 0)
    assert first["text"] == "first line\nsecond line\n"
    assert first["offset"] == 0 and first["eof"] is True
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("third line\n")
    second = textwindow.read_window(path, first["next"])
    assert second["text"] == "third line\n", "the follow returns only what was appended"
    assert second["offset"] == first["next"] and second["eof"] is True


def test_one_window_is_capped_and_says_the_file_has_more(tmp_path):
    path = _write(tmp_path / "job.log", "x" * (textwindow.WINDOW + 4096))
    body = textwindow.read_window(path, 0)
    assert len(body["text"]) == textwindow.WINDOW
    assert body["next"] == textwindow.WINDOW
    assert body["eof"] is False, "the window stopped short of the end of the file"
    rest = textwindow.read_window(path, body["next"])
    assert len(rest["text"]) == 4096 and rest["eof"] is True


def test_an_offset_past_the_end_reads_nothing_rather_than_failing(tmp_path):
    path = _write(tmp_path / "job.log", "short\n")
    body = textwindow.read_window(path, 99999)
    assert body["text"] == "" and body["eof"] is True


def test_a_log_not_written_yet_reads_empty_and_not_as_an_error(tmp_path):
    body = textwindow.read_window(tmp_path / "absent.log", 0)
    assert body["text"] == "" and body["size"] == 0 and body["eof"] is True


def test_the_log_text_is_the_tool_text(tmp_path):
    written = "area 12 \u00b5m\u00b2 done\n"
    path = _write(tmp_path / "job.log", written)
    body = textwindow.read_window(path, 0)
    # the decoded text is what the tool wrote. A page writes it with textContent, so an entity
    # pass here would show the reader the literal characters `&#181;`.
    assert body["text"] == written
    assert "&#" not in body["text"], "the text is not entity-encoded"


def test_a_character_split_by_the_window_boundary_is_not_two_blots(tmp_path):
    # a two-byte character sits exactly across the 64 KiB window boundary: its lead byte is the
    # last byte of the first window and its continuation byte the first of the second.
    head = "x" * (textwindow.WINDOW - 1)
    tail = "\u00b5m\u00b2 later\n"
    path = _write(tmp_path / "job.log", head + tail)

    first = textwindow.read_window(path, 0)
    assert "\ufffd" not in first["text"], "the window stopped inside a character"
    assert first["text"] == head
    assert first["next"] == textwindow.WINDOW - 1, "the window backed off to the character boundary"
    assert first["eof"] is False

    second = textwindow.read_window(path, first["next"])
    assert "\ufffd" not in second["text"], "the next window opened inside a character"
    assert second["text"] == tail and second["eof"] is True
    assert first["text"] + second["text"] == head + tail


def test_bytes_that_are_not_utf8_at_the_end_of_the_file_still_get_through(tmp_path):
    # negative: the backoff must not stall the follow. A tool that wrote a byte no character
    # owns reaches the reader as a replacement character rather than never arriving.
    path = _write(tmp_path / "job.log", b"cell \xff")
    body = textwindow.read_window(path, 0)
    assert body["eof"] is True and body["next"] == 6, "the last byte was not held back"
    assert body["text"] == "cell \ufffd"


def test_whole_chars_cuts_back_only_an_incomplete_sequence():
    euro = "\u20ac".encode("utf-8")          # three bytes
    assert textwindow.whole_chars(b"ab") == 2
    assert textwindow.whole_chars(b"ab" + euro) == 5
    assert textwindow.whole_chars(b"ab" + euro[:2]) == 2
    assert textwindow.whole_chars(b"ab" + euro[:1]) == 2
    # negative: a stray continuation byte with no lead in reach is not cut.
    assert textwindow.whole_chars(b"\x80\x80\x80\x80") == 4
    assert textwindow.whole_chars(b"") == 0
