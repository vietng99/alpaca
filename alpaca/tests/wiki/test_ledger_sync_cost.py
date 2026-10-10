"""t-453: the ledger sync of a write transaction costs what changed, not the whole history.

Every `Writer.transaction` syncs the `events.jsonl` mirror before it begins and after it commits
(`Writer.sync_ledger`). On f15a17101 each sync decoded every `ingest_event` row and every ledger
line and recomputed the whole hash chain twice (the store's and the mirror's), so one collector
pass of 200 event notes spent 806 chain checks over the X build's 970 events, more than half of
the pass (`.alpaca/proofs/op-008/t-453.logs/`). A sync now checks the rows and lines it has not
proved yet, and proves the rest unchanged by their bytes; the checks themselves are the same, so
every change to a stored event or a ledger line is still caught at the next transaction.
"""
import shutil

import pytest

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.store import ledger as ledger_mod
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.ledger import Ledger, LedgerDivergence, verify_chain
from alpaca.wiki.store.write import Writer

T0 = "2020-01-01T00:00:00+00:00"


def _writer(vault):
    vault.mkdir(parents=True, exist_ok=True)
    cfg = Config.for_vault(vault)
    store = DB(cfg)
    store.pour()
    return cfg, store, Writer(store, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir),
                              clock=FixedClock(start=T0, step=1))


def _grow(writer, n, prefix="n"):
    for i in range(n):
        with writer.transaction():
            writer.upsert_node("%s%04d" % (prefix, i), "org", "Node %d" % i)


def _events(store):
    return store.conn.execute("SELECT COUNT(*) FROM ingest_event").fetchone()[0]


def _ledger_ok(cfg, store):
    rows = Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir).read_all()
    db_rows = store.conn.execute("SELECT seq, checksum FROM ingest_event ORDER BY seq").fetchall()
    return verify_chain(rows)[0] and [(r["seq"], r["checksum"]) for r in rows] == [tuple(r) for r in db_rows]


@pytest.fixture
def vault(tmp_path):
    yield tmp_path / "vault"
    shutil.rmtree(tmp_path / "vault", ignore_errors=True)


def test_a_transaction_checks_only_the_events_it_has_not_proved(vault, monkeypatch):
    cfg, store, writer = _writer(vault)
    _grow(writer, 300)
    assert _events(store) == 300
    calls = []
    real = ledger_mod.event_checksum
    monkeypatch.setattr(ledger_mod, "event_checksum", lambda *a: calls.append(a[0]) or real(*a))
    with writer.transaction():
        writer.upsert_node("late", "org", "Late")
    assert _events(store) == 301
    # f15a17101: two syncs, each recomputing both chains over every event (about 1,200 checks)
    assert len(calls) <= 4, "the sync recomputed %d checksums for one new event" % len(calls)
    assert set(calls) <= {301}
    assert _ledger_ok(cfg, store)


def test_a_changed_stored_event_is_caught_at_the_next_transaction(vault):
    cfg, store, writer = _writer(vault)
    _grow(writer, 5)
    store.conn.execute("UPDATE ingest_event SET payload=? WHERE seq=2",
                       ('{"node_id":"forged","node_type":"org"}',))
    store.conn.commit()
    with pytest.raises(LedgerDivergence, match="DB event chain invalid: checksum mismatch at seq 2"):
        with writer.transaction():
            writer.upsert_node("after", "org", "After")


def test_a_changed_ledger_line_is_caught_at_the_next_transaction(vault):
    cfg, store, writer = _writer(vault)
    _grow(writer, 5)
    lines = cfg.ledger_path.read_bytes().splitlines(keepends=True)
    lines[1] = lines[1].replace(b'"node_type":"org"', b'"node_type":"orc"')
    cfg.ledger_path.write_bytes(b"".join(lines))
    with pytest.raises(LedgerDivergence, match="JSONL event chain invalid: checksum mismatch at seq 2"):
        with writer.transaction():
            writer.upsert_node("after", "org", "After")


def test_a_ledger_replaced_by_another_valid_chain_is_caught(vault, tmp_path):
    cfg, store, writer = _writer(vault)
    _grow(writer, 5)
    other_cfg, other_store, other_writer = _writer(tmp_path / "other")
    _grow(other_writer, 5, prefix="o")
    other_store.close()
    shutil.copyfile(other_cfg.ledger_path, cfg.ledger_path)
    with pytest.raises(LedgerDivergence, match="DB/JSONL divergence at seq 1"):
        with writer.transaction():
            writer.upsert_node("after", "org", "After")


def test_an_incomplete_last_line_is_repaired_and_the_mirror_completed(vault):
    cfg, store, writer = _writer(vault)
    _grow(writer, 5)
    with open(cfg.ledger_path, "ab") as handle:
        handle.write(b'{"seq":6,"op":"upse')
    with writer.transaction():
        writer.upsert_node("after", "org", "After")
    assert _events(store) == 6 and _ledger_ok(cfg, store)


def test_a_lost_ledger_is_written_again_from_the_store(vault):
    cfg, store, writer = _writer(vault)
    _grow(writer, 5)
    cfg.ledger_path.unlink()
    with writer.transaction():
        writer.upsert_node("after", "org", "After")
    assert _events(store) == 6 and _ledger_ok(cfg, store)


def test_another_writer_between_two_transactions_is_followed(vault):
    cfg, store, writer = _writer(vault)
    _grow(writer, 3)
    _cfg2, store2, writer2 = _writer(vault)
    _grow(writer2, 4, prefix="w")
    store2.close()
    with writer.transaction():
        writer.upsert_node("after", "org", "After")
    assert _events(store) == 8 and _ledger_ok(cfg, store)


def test_a_ledger_ahead_of_the_store_is_refused(vault):
    cfg, store, writer = _writer(vault)
    _grow(writer, 3)
    full = cfg.ledger_path.read_bytes()
    store.conn.execute("DELETE FROM ingest_event WHERE seq=3")
    store.conn.commit()
    assert cfg.ledger_path.read_bytes() == full
    with pytest.raises(LedgerDivergence, match="ledger ahead of committed DB: ledger=3 db=2"):
        writer.sync_ledger()


def test_a_retier_of_an_unchanged_graph_reuses_its_ranking(vault, monkeypatch):
    """`maintain_indexes` re-tiers after every absorbed document; PPR over the whole graph ran each
    time, though most documents change no node or edge. Its result is reused while the graph read
    is equal, and the bands written are the ones a fresh run gives."""
    from alpaca.wiki.store import write
    cfg, store, writer = _writer(vault)
    with writer.transaction():
        for i in range(6):
            writer.upsert_node("n%d" % i, "org", "Node %d" % i)
    calls = []
    real = write.ppr
    monkeypatch.setattr(write, "ppr", lambda *a, **k: calls.append(1) or real(*a, **k))
    with writer.transaction():
        first = write.retier_nodes(store, autocommit=False)
        again = write.retier_nodes(store, autocommit=False)
    assert again == first and len(calls) == 1
    with writer.transaction():
        writer.upsert_node("n9", "org", "Node 9")
        changed = write.retier_nodes(store, autocommit=False)
    assert len(calls) == 2 and set(changed) == set(first) | {"n9"}
    store._retier_memo = None
    with writer.transaction():
        fresh = write.retier_nodes(store, autocommit=False)
    assert fresh == changed and len(calls) == 3
    bands = dict(store.conn.execute("SELECT node_id, page_band FROM nodes").fetchall())
    assert bands == fresh


def _normalize_reference(text):
    """`determinism.normalize_text` as f15a17101 had it, for the equivalence check below."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFC", text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def test_normalize_text_substitutes_only_where_the_text_changes(monkeypatch):
    """The echo index normalizes every active block of the store in each collector process; the
    space substitution matched every single space. It now runs only on a text holding a tab or two
    adjacent spaces (the blank-line one only on two adjacent newlines), and gives the same text."""
    import random
    from alpaca.wiki import determinism

    class Counting:
        def __init__(self, pattern):
            self.pattern, self.calls = pattern, 0

        def sub(self, *a):
            self.calls += 1
            return self.pattern.sub(*a)
    spaces, blanks = Counting(determinism._SPACES), Counting(determinism._BLANK_LINES)
    monkeypatch.setattr(determinism, "_SPACES", spaces)
    monkeypatch.setattr(determinism, "_BLANK_LINES", blanks)
    assert determinism.normalize_text("user: the lantern rota\nts: 2026-10-08 note one") == \
        "user: the lantern rota\nts: 2026-10-08 note one"
    assert (spaces.calls, blanks.calls) == (0, 0)
    alphabet = [" ", "  ", "\t", "\n", "\r", "\r\n", "a", "b", "\u00a0", "e\u0301", "\u00e9", "x y", "\n\n"]
    rng = random.Random(453)
    cases = ["", " ", "\t", "\n\n", " a ", "a \t b", "a\t\tb", "a\n \nb", "a\r\n\r\nb", "\u00a0 a\u00a0\u00a0"]
    cases += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24))) for _ in range(5000)]
    for case in cases:
        assert determinism.normalize_text(case) == _normalize_reference(case), repr(case)
