"""A byte window of a growing log, cut on whole UTF-8 characters.

A reader that follows a log a job is still writing asks for one window at a time from an
offset. `read_window` returns the decoded text of one window, where to ask next and whether the
window reached the end of the file as it stood. A log that is still being written simply grows,
so `eof` is not final. The caller owns which log is read (the path is passed in) and how the
answer is served.
"""
import os

WINDOW = 64 * 1024        # bytes of a log returned per window


def whole_chars(chunk):
    """How much of `chunk` ends on a UTF-8 character boundary.

    A window that stops in the middle of a multi-byte sequence decodes its tail as a replacement
    character, and the next window decodes its head as a second one, so one character on the seam
    reads as two blots and neither window can show it. Cutting the window back to the last whole
    character hands the split sequence to the next request instead. A continuation byte is
    `10xxxxxx`; a lead byte says how many bytes follow it, and the longest sequence is four, so
    at most three bytes are ever looked at.
    """
    n = len(chunk)
    for back in range(1, min(4, n) + 1):
        byte = chunk[n - back]
        if byte < 0x80:                       # a plain ASCII byte ends the window cleanly
            return n
        if byte >= 0xC0:                      # a lead byte: its sequence wants `want` bytes
            want = 2 if byte < 0xE0 else (3 if byte < 0xF0 else 4)
            return n if back >= want else n - back
        # 0x80..0xBF is a continuation byte; keep looking back for the lead that owns it
    return n


def read_window(path, offset, limit=WINDOW):
    """One window of the log at `path` from `offset`: {"offset", "next", "size", "text", "eof"}.

    A log that is absent or unreadable reads as an empty window at the end, not as an error.
    The text is the decoded log and nothing else: no entity pass, so a page that writes it with
    `textContent` shows the tool's own characters.
    """
    try:
        size = os.path.getsize(path)
        start = max(0, min(int(offset), size))
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(limit)
    except (OSError, ValueError):
        return {"offset": 0, "next": 0, "text": "", "eof": True, "size": 0}
    nxt = start + len(chunk)
    if nxt < size:
        # more bytes follow, so a split sequence is a seam and the next window carries it. At the
        # end of the file the bytes are all there is: a tool that wrote something that is not
        # UTF-8 must still get through rather than stall the follow.
        cut = whole_chars(chunk)
        if 0 < cut < len(chunk):
            chunk, nxt = chunk[:cut], start + cut
    return {"offset": start, "next": nxt, "size": size,
            "text": chunk.decode("utf-8", "replace"),
            "eof": nxt >= size}
