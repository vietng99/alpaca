"""M4.9 proof - neutralisation of externally sourced values at render time.

One hostile fixture value carrying a pipe, a newline, a `<script>` tag and a non-ASCII character
is pushed through every renderer, and every Done-when clause is a BYTE check against a baseline
render of the same record with a benign value in the same field:

  1. RESUME.md structure survives      - same row count and same `|` per row as the baseline.
  2. RESUME.md content round-trips     - reversing the declared cell escape yields the original.
  3. board.json / data.json stay JSON  - both parse, and the injected field equals the original.
  4. the analytics page carries no live markup from the value - no `<script` from the fixture,
     its angle brackets are `&lt;`/`&gt;`, and the page parses with a balanced tag stack.
  5. the analytics page is pure ASCII  - zero bytes above 0x7F, and the non-ASCII character is an
     entity whose unescaped value equals the original character.
  6. no renderer bypasses the boundary - the surface-wiring census names every surface writer and
     asserts each routes through render.cell / render.html_text / render.for_json; an unrouted
     writer FAILs.
  7. the release door consumes the countersign row (test_release_door_requires_countersign).

A FixedClock makes every render byte-stable but for the volatile generation stamp; a throwaway
root is used throughout, never the live record.
"""
import html as htmllib
import json
import os
from html.parser import HTMLParser

import pytest

from alpaca import board, cli, clock, db, export, pad, project, render, util
from alpaca.analytics import build_index
from alpaca.gates import verdict as vc
from alpaca.phase import doors
from alpaca.tests.conftest import REPO

# The one hostile fixture value: a pipe (splits a table column), a newline (splits a table row),
# a <script> tag (live markup), and a non-ASCII character (garbles on a phone that misreads the
# encoding). The non-ASCII byte is written as an ASCII escape so this source file stays ASCII.
NON_ASCII = chr(0x00E9)                             # a small latin e with acute; ord == 233
HOSTILE = "L|R\nrow2 <script>alert('x')</script> " + NON_ASCII + " end"
BENIGN = "a plain benign statement with no metacharacters"

FIELD_OP = "op-001"
FIELD_ROW = "r-1"


# --------------------------------------------------------------------------- record builders
def _seed(root, *, intent, statement, name):
    """Seed one op (its intent externally sourced), one obligation row (its statement externally
    sourced) and the project name, all set to the given values. Overwrites in place, so calling
    it twice on one root renders the SAME record with a different value in the same field."""
    conn = db.connect(root)
    db.upsert(conn, "ops", "id", {
        "id": FIELD_OP, "intent": intent, "done_when": "the board is green",
        "status": "open", "opened": util.now_iso(), "closed": None, "phases": None})
    db.upsert(conn, "rows", "id", {
        "id": FIELD_ROW, "kind": "item", "op": FIELD_OP, "phase": "build", "step": "s1",
        "statement": statement, "proof": "local:spec.md", "where_": "", "how": "",
        "when_": "", "why": "", "session": None, "operator": None, "status": "open",
        "tag": "Specced", "content_hash": util.sha256_hex("row/" + FIELD_ROW),
        "prev_hash": None, "supersedes": None, "superseded_by": None})
    project.save(root, {"name": name, "who": "v:owner", "what": "w"})
    return conn


@pytest.fixture
def proj(project):
    """An onboarded throwaway root under a fixed clock."""
    util.set_clock(clock.FixedClock("2026-04-01T00:00:00+00:00", step=0))
    cli.main(["init"])
    cli.main(["onboard", "--name", "seed", "--who", "v:owner", "--what", "w"])
    yield project
    util.set_clock(None)


def _render_resume(root, *, intent, statement, name):
    _seed(root, intent=intent, statement=statement, name=name)
    return pad.render(root)


def _render_board(root, *, intent, statement, name):
    conn = _seed(root, intent=intent, statement=statement, name=name)
    return board.render_json(conn)


def _render_data(root, *, intent, statement, name):
    _seed(root, intent=intent, statement=statement, name=name)
    return export.render(root)


def _render_page(root, *, intent, statement, name):
    _seed(root, intent=intent, statement=statement, name=name)
    return util.read_text(build_index.build(root))


# --------------------------------------------------------------- clause 1: RESUME structure
def _pipes_per_line(text):
    return [ln.count("|") for ln in text.splitlines()]


def test_clause1_resume_structure_survives(proj):
    hostile = _render_resume(proj, intent=HOSTILE, statement=BENIGN, name="seed")
    baseline = _render_resume(proj, intent=BENIGN, statement=BENIGN, name="seed")
    assert len(hostile.splitlines()) == len(baseline.splitlines()), \
        "the hostile pipe/newline created, split or merged no row"
    assert _pipes_per_line(hostile) == _pipes_per_line(baseline), \
        "the hostile pipe forged no column: same `|` count on every row"


# --------------------------------------------------------------- clause 2: RESUME round-trip
def _resume_intent_cell(text):
    """The op-intent cell rendered into the '## Current op' block: the text after 'op-001  '."""
    block = text.split("## Current op", 1)[1]
    for ln in block.splitlines():
        if ln.startswith(FIELD_OP + "  "):
            return ln[len(FIELD_OP + "  "):]
    raise AssertionError("no op-intent line in the Current op block")


def test_clause2_resume_content_round_trips(proj):
    hostile = _render_resume(proj, intent=HOSTILE, statement=BENIGN, name="seed")
    cell = _resume_intent_cell(hostile)
    # the escaped cell carries no raw pipe and no raw newline (clause 1's mechanism) ...
    assert "|" not in cell and "\n" not in cell
    # ... and reversing the declared escape yields the original value byte for byte.
    assert render.uncell(cell) == HOSTILE, "the cell escape is reversible and lossless"


# --------------------------------------------------- clause 3: board.json / data.json stay JSON
def _card_statement(parsed):
    for card in parsed["cards"]:
        if card["row_id"] == FIELD_ROW:
            return card["statement"]
    raise AssertionError("no card for the injected row")


def test_clause3_board_json_stays_valid_and_equal(proj):
    text = _render_board(proj, intent=BENIGN, statement=HOSTILE, name="seed")
    parsed = json.loads(text)                            # no exception: still valid JSON
    assert _card_statement(parsed) == HOSTILE, "the parsed statement equals the original bytes"


def test_clause3_data_json_stays_valid_and_equal(proj):
    text = _render_data(proj, intent=BENIGN, statement=HOSTILE, name="seed")
    parsed = json.loads(text)                            # no exception: still valid JSON
    assert _card_statement(parsed["board"]) == HOSTILE, \
        "the parsed statement in data.json equals the original bytes"


# ------------------------------------------------ clause 4: the analytics page carries no markup
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "keygen",
        "link", "meta", "param", "source", "track", "wbr"}


class _Stack(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.balanced = True

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.balanced = False
        else:
            self.stack.pop()


def test_clause4_page_carries_no_live_markup(proj):
    hostile = _render_page(proj, intent=BENIGN, statement=BENIGN, name=HOSTILE)
    baseline = _render_page(proj, intent=BENIGN, statement=BENIGN, name=BENIGN)
    # the fixture contributes no `<script` open: the count is exactly the template's own.
    assert hostile.count("<script") == baseline.count("<script"), \
        "the value opened no <script tag on the page"
    # its angle brackets appear as entities, not live markup.
    assert "&lt;script&gt;" in hostile and "&lt;/script&gt;" in hostile
    # the page parses with a balanced tag stack.
    p = _Stack()
    p.feed(hostile)
    p.close()
    assert p.balanced and p.stack == [], "the page parses with a balanced tag stack"


# ------------------------------------------------------- clause 5: the analytics page is ASCII
def test_clause5_page_is_pure_ascii(proj):
    hostile = _render_page(proj, intent=BENIGN, statement=BENIGN, name=HOSTILE)
    over = [c for c in hostile if ord(c) > 0x7F]
    assert over == [], "every byte of the page is <= 0x7F (phone-safe)"
    entity = "&#%d;" % ord(NON_ASCII)
    assert entity in hostile, "the non-ASCII character appears as a numeric entity"
    assert htmllib.unescape(entity) == NON_ASCII, "the entity unescapes to the original character"


# ---------------------------------------------- clause 6: no renderer bypasses the boundary
def test_clause6_every_surface_writer_routes_through_the_boundary(proj):
    rows = render.surface_wiring(REPO)
    assert {r["module"] for r in rows} == set(render.SURFACE_WRITERS), \
        "the census names every surface writer"
    unrouted = [r["module"] for r in rows if not r["routed"]]
    assert unrouted == [], "every surface writer routes through render: %r" % unrouted


def test_clause6_an_unrouted_writer_fails_the_census():
    # a synthetic writer that touches no boundary reads as unrouted (the negative path) ...
    bypass = render.census({"alpaca/pad.py": "def render(root):\n    return 'RESUME'\n"})
    assert bypass[0]["routed"] is False, "a writer that calls no boundary is a bypass"
    # ... an import with no call is still not routed ...
    imp_only = render.census({"alpaca/pad.py": "from alpaca import render\n\ndef r():\n    return 1\n"})
    assert imp_only[0]["routed"] is False, "importing the boundary is not calling it"
    # ... and an import plus a call is routed.
    routed = render.census({"alpaca/pad.py": "from alpaca import render\n\ndef r(v):\n"
                                         "    return render.cell(v)\n"})
    assert routed[0]["routed"] is True


# ------------------------------------------- clause 7: the release door consumes the countersign
def test_release_door_requires_countersign(proj):
    conn = db.connect(proj)
    # with no owner-countersign row whose ref is render-neutralisation, the door pauses (exit 3)
    # and the recorded reason names the missing ref.
    absent = doors.release_door(conn, FIELD_OP, root=proj)
    assert absent == vc.PAUSED, "the release door exits 3 with no countersign row"
    # record the owner-countersign for render-neutralisation ...
    doors.record_countersign(conn, "render-neutralisation",
                             ["RESUME.md", "analytics/index.html"],
                             pointer="journal:the owner read both surfaces", root=proj)
    # ... and the door now returns the verdict its other links fold to (here: PASS).
    present = doors.release_door(conn, FIELD_OP, root=proj)
    assert present == vc.PASS, "with the countersign row present the door folds its other links"


def test_release_door_folds_its_other_links_when_present(proj):
    conn = db.connect(proj)
    doors.record_countersign(conn, "render-neutralisation", ["RESUME.md"],
                             pointer="journal:read", root=proj)
    # the countersign is PASS, so the door returns the fold of its OTHER links.
    assert doors.release_door(conn, FIELD_OP, root=proj,
                              extra_links=[("packaging", vc.PASS)]) == vc.PASS
    assert doors.release_door(conn, FIELD_OP, root=proj,
                              extra_links=[("packaging", vc.FAIL)]) == vc.FAIL


def test_release_door_reads_only_its_own_ref(proj):
    conn = db.connect(proj)
    # the OTHER countersign ref (manual-phone-read, M4.16) does not discharge this door.
    doors.record_countersign(conn, "manual-phone-read", ["docs/manual.html"],
                             pointer="journal:phone read", root=proj)
    assert doors.release_door(conn, FIELD_OP, root=proj) == vc.PAUSED, \
        "manual-phone-read does not stand in for render-neutralisation"
