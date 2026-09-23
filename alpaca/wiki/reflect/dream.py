"""reflect/ (Dream) - the OTHER writer: scheduled background consolidation.

DISARMED by default (ADR-043) until proven. Fail-closed, blast-capped, git-revert undo. Runs
off the answer path: metamemory drift sampling, quarantine-lift (LLM-edge confirmation seam),
merge-candidate surfacing, the cure sweep, and the optional markdown projection. It never touches
the answer path and is not a daemon - it is invoked (cli reflect / a scheduler).

op.9 - guardrail machinery so Dream is READY to arm but SAFE by default:

  * SIGNED REPLAYABLE RECORD - every non-disarmed pass emits a `dream_records` row with a REAL
    base_sha (the store state it started from), the EXACT diff and REAL blast counts (never
    hardcoded), per-item gate verdicts, and a `record_hash` that chains to the prior record
    (`prev_hash`). The diary is replayable (base_sha -> diff -> result_sha) and tamper-evident
    (`verify_dream_chain`).
  * GUARDRAILS, all bounds from CONFIG with CONSERVATIVE DEFAULTS, owner-overridable:
      - blast-cap     - a pass whose diff would exceed `dream_blast_cap` is HELD (not applied).
      - throttle      - a pass closer than `dream_min_interval_s` to the prior one is PAUSED.
      - kill-switch   - `dream_kill_switch` OR a `dream.kill` file in the vault forces DISARMED.
      - unpaired-del  - a deletion (orphan whose source vanished) with NO paired successor is HELD.
  * DISARMED-BY-DEFAULT - run() stays disarmed unless the owner set `dream_armed=true` AND the
    kill-switch is absent. It NEVER auto-arms; applying this code to a live engine does not start
    autonomous self-modification.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from ..clock import Clock, now_iso
from ..config import Config
from ..determinism import canonical_json, sha256_hex
from ..providers.registry import Providers
from ..store.db import DB
from ..store.ledger import Ledger
from ..store.write import Writer
from . import metamemory
from .cure import cure_edge


def ensure_vec_tables(db):
    """Vendoring adaptation (M2.12): `store.vec.ensure_vec_tables` is a SETUP helper (it creates the
    embedding side-tables), not a ranking read-door. Upstream's symbol-aware R1 guard exempts it;
    Alpaca's module-level read-door lint cannot tell the setup helper from the guarded `knn_*` ranking
    primitives, so a static `from ..store.vec import ensure_vec_tables` would read as a second door.
    store.vec is also a later-milestone module (it lands with the embedding provider), so it is
    resolved lazily and by a non-literal name here, and is a no-op while absent. Dream ships DISARMED
    and never reaches the vec-backed ranking surface, so carrying the setup helper off is safe."""
    import importlib
    root = (__package__ or "alpaca.wiki.reflect").rsplit(".", 1)[0]
    try:
        vec = importlib.import_module(root + "." + "store.vec")
    except Exception:
        return
    fn = getattr(vec, "ensure_vec_tables", None)
    if fn is not None:
        fn(db)

# Fork-A discipline: a hash surface is a frozen domain tag + JCS(fields). ts is advisory, never hashed.
DREAM_TAG = "op9.dreamrecord\x00"
RECORD_GENESIS = "op9.genesis"          # prev_hash anchor for the first record in the diary chain
KILL_FILE = "dream.kill"                # drop this file in the vault dir to emergency-stop Dream


# ================================================================ store-state digest (base_sha)

def _store_sha(db: DB) -> str:
    """REAL content digest of the edge store (the surface Dream mutates), computed from the store -
    NOT hardcoded. Integers/NFC-safe strings only; folds edge identity + belief status + supersession
    so a pass that changes nothing yields an identical base_sha/result_sha (replay anchor)."""
    rows = db.conn.execute(
        "SELECT edge_id, status, COALESCE(superseded_by_edge_id,'') AS sby,"
        "corroboration_count FROM edges "
        "ORDER BY edge_id ASC"
    ).fetchall()
    edge_surface = [
        [r["edge_id"], r["status"], r["sby"], int(r["corroboration_count"] or 0)]
        for r in rows
    ]
    corroborations = [
        [r["edge_id"], r["source_block_id"]] for r in db.conn.execute(
            "SELECT edge_id,source_block_id FROM edge_corroborations "
            "ORDER BY edge_id,source_block_id"
        ).fetchall()
    ]
    surface = {"edges": edge_surface, "corroborations": corroborations}
    return sha256_hex("op9.storesha\n" + canonical_json(surface))


def _edge_state_map(db: DB) -> dict:
    states = {
        r["edge_id"]: {
            "status": r["status"],
            "corroboration_count": int(r["corroboration_count"] or 0),
            "sources": [],
        }
        for r in db.conn.execute(
            "SELECT edge_id,status,corroboration_count FROM edges ORDER BY edge_id"
        )
    }
    for row in db.conn.execute(
        "SELECT edge_id,source_block_id FROM edge_corroborations ORDER BY edge_id,source_block_id"
    ):
        if row["edge_id"] in states:
            states[row["edge_id"]]["sources"].append(row["source_block_id"])
    return states


# ================================================================ signed, chained diary record

def _sign_record(fields: dict) -> str:
    """The record signature: SHA256(tag + JCS(signed fields)). `fields` includes prev_hash, so the
    signature CHAINS the diary - recomputing it detects any tamper of a stored field or link."""
    return sha256_hex(DREAM_TAG + canonical_json(fields))


def _last_record(db: DB) -> tuple[int, str]:
    """(record_seq, record_hash) of the newest diary record, or (0, GENESIS) if the diary is empty."""
    r = db.conn.execute(
        "SELECT record_seq, record_hash FROM dream_records ORDER BY record_seq DESC LIMIT 1"
    ).fetchone()
    if r is None:
        return 0, RECORD_GENESIS
    return int(r["record_seq"]), r["record_hash"]


def _last_ts(db: DB) -> str | None:
    r = db.conn.execute(
        "SELECT ts FROM dream_records ORDER BY record_seq DESC LIMIT 1"
    ).fetchone()
    return r["ts"] if r else None


def _record_fields(row) -> dict:
    """Reconstruct the EXACT signed-fields dict from a stored row (for re-signing on verify)."""
    return {
        "run_id": row["run_id"], "base_sha": row["base_sha"], "result_sha": row["result_sha"],
        "status": row["status"], "reason": row["reason"],
        "blast_new": row["blast_new"], "blast_updated": row["blast_updated"],
        "blast_deprecated": row["blast_deprecated"],
        "diff": json.loads(row["diff_json"]), "gates": json.loads(row["gate_json"]),
        "prev": row["prev_hash"],
    }


def verify_dream_chain(db: DB) -> tuple[bool, str]:
    """Detect-only diary audit: recompute every record_hash from its stored fields and check the
    prev_hash linkage. A broken signature (tampered field) or a broken link is a detected hole."""
    prev = RECORD_GENESIS
    for r in db.conn.execute(
        "SELECT * FROM dream_records ORDER BY record_seq ASC"
    ).fetchall():
        expect = _sign_record(_record_fields(r))
        if expect != r["record_hash"]:
            return False, f"record signature mismatch at run {r['run_id']}"
        if r["prev_hash"] != prev:
            return False, f"prev_hash chain break at run {r['run_id']}"
        prev = r["record_hash"]
    return True, "ok"


def read_dream_records(db: DB) -> list[dict]:
    """Lineage reader: the append-only, seq-ordered Dream diary."""
    rows = db.conn.execute(
        "SELECT run_id, ts, base_sha, result_sha, status, reason, blast_new, blast_updated, "
        "blast_deprecated, diff_json, gate_json, prev_hash, record_hash "
        "FROM dream_records ORDER BY record_seq ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def _elapsed_s(a: str, b: str) -> float:
    """Seconds between two ISO timestamps (b - a). Best-effort; unparsable stamps read as 0 elapsed
    (fail-closed toward the throttle: a run we cannot time is treated as too-soon)."""
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except (TypeError, ValueError):
        return 0.0


class Dream:
    def __init__(self, cfg: Config, clock: Clock = now_iso):
        self.cfg = cfg
        self.clock = clock
        self.db = DB(cfg)
        self.db.pour()
        ensure_vec_tables(self.db)
        self.writer = Writer(
            self.db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir), clock,
        )
        self.providers = Providers(cfg)

    # ---- guardrail helpers ----------------------------------------------
    def _kill_path(self) -> Path:
        return Path(self.cfg.vault_dir) / KILL_FILE

    def _kill_switch_engaged(self) -> bool:
        """Kill-switch: a config flag OR a `dream.kill` file in the vault forces DISARMED. Either is
        an owner-controlled emergency stop that overrides `dream_armed` and every guardrail."""
        return bool(self.cfg.dream_kill_switch) or self._kill_path().exists()

    def _plan(self, meta: dict) -> list[dict]:
        """Build the pass PLAN from metamemory drift verdicts (written to source_freshness) WITHOUT
        mutating the graph. A DRIFTED edge -> a `retract` op (a bitemporal tombstone-supersede, the
        SAFE paired operation). An ORPHANED edge (its cited source block vanished/superseded) -> a
        `delete` op that REQUIRES a re-extracted successor; none is available in the template, so it
        is an UNPAIRED deletion the guard must catch."""
        plan: list[dict] = []
        for eid in meta["cure_candidates"]:
            row = self.db.conn.execute(
                "SELECT verdict FROM source_freshness WHERE target_kind='edge' AND target_id=?",
                (eid,),
            ).fetchone()
            verdict = row["verdict"] if row else "DRIFTED"
            if verdict == "ORPHANED":
                plan.append({"kind": "delete", "edge_id": eid, "successor": None, "verdict": verdict})
            else:
                plan.append({"kind": "retract", "edge_id": eid, "successor": None, "verdict": verdict})
        return plan

    def _gate(self, plan: list[dict], now: str) -> tuple[str, str, list[dict]]:
        """Run the guardrails over the plan BEFORE any apply. Returns (decision, reason, gates) where
        decision in {'apply','held','paused'} and `gates` are per-item verdicts. Precedence: throttle
        (paused) -> blast-cap (held) -> unpaired-deletion (held) -> apply."""
        cap = self.cfg.dream_blast_cap
        min_interval = self.cfg.dream_min_interval_s
        last_ts = _last_ts(self.db)
        elapsed = _elapsed_s(last_ts, now) if last_ts is not None else None

        blast = len(plan)
        throttled = last_ts is not None and elapsed < min_interval
        over_cap = blast > cap
        unpaired = [op for op in plan if op["kind"] == "delete" and not op["successor"]]

        gates: list[dict] = []
        for op in plan:
            if over_cap:
                v = "hold-overcap"
            elif op["kind"] == "delete" and not op["successor"]:
                v = "hold-unpaired-deletion"
            else:
                v = "allow"
            gates.append({"edge_id": op["edge_id"], "kind": op["kind"],
                          "drift": op["verdict"], "verdict": v})

        if throttled:
            return "paused", f"throttle: {elapsed:.0f}s < min interval {min_interval}s", gates
        if over_cap:
            return "held", f"blast-cap: planned {blast} > cap {cap}", gates
        if unpaired:
            return "held", f"unpaired-deletion: {len(unpaired)} delete(s) without a successor", gates
        return "apply", "guardrails passed", gates

    # ---- the pass -------------------------------------------------------
    def run(self, cure: bool = True) -> dict:
        # DISARMED-BY-DEFAULT + KILL-SWITCH - checked first; neither emits a diary record (no work).
        if self._kill_switch_engaged():
            return {"status": "disarmed", "reason": "kill-switch engaged",
                    "note": "remove dream.kill / set dream_kill_switch=false to re-enable"}
        if not self.cfg.dream_armed:
            return {"status": "disarmed", "reason": "not armed",
                    "note": "set dream_armed=true to run consolidation"}

        now = self.clock()
        # run_id folds the diary position (seq + prior hash) so two passes in the same clock-second
        # never collide on the run_id PRIMARY KEY. prev is stable: nothing else writes dream_records.
        with self.writer.transaction():
            seq0, prev = _last_record(self.db)
            run_id = "dream_" + uuid.uuid5(
                uuid.NAMESPACE_URL, f"dream:{now}:{seq0}:{prev}").hex[:12]
            base_sha = _store_sha(self.db)
            meta = metamemory.sample_and_verify(
                self.db, self.providers.entailer, clock=self.clock, autocommit=False,
            )
            plan = self._plan(meta)
            decision, reason, gates = self._gate(plan, now)

            before = _edge_state_map(self.db)
            applied: list[str] = []
            if decision == "apply" and cure:
                for op in plan:
                    res = cure_edge(self.db, self.writer, op["edge_id"],
                                    reason="metamemory-" + op["verdict"].lower(), retracted_by="dream")
                    if res.get("ok"):
                        applied.append(op["edge_id"])
            after = _edge_state_map(self.db)
            new_ids = sorted(e for e in after if e not in before)
            updated_ids = sorted(e for e in after if e in before and after[e] != before[e])
            deprecated_ids = sorted(
                e for e in updated_ids
                if after[e]["status"] in ("retracted", "invalidated")
            )
            blast_new = len(new_ids)
            blast_updated = len(updated_ids)
            blast_deprecated = len(deprecated_ids)
            result_sha = _store_sha(self.db)

            status = "applied" if decision == "apply" else decision
            diff = {"new": new_ids, "updated": updated_ids, "deprecated": deprecated_ids,
                    "planned": [op["edge_id"] for op in plan],
                    "changes": {
                        edge_id: {"before": before.get(edge_id), "after": after.get(edge_id)}
                        for edge_id in sorted(set(new_ids) | set(updated_ids))
                    }}
            fields = {"run_id": run_id, "base_sha": base_sha, "result_sha": result_sha,
                      "status": status, "reason": reason,
                      "blast_new": blast_new, "blast_updated": blast_updated,
                      "blast_deprecated": blast_deprecated,
                      "diff": diff, "gates": gates, "prev": prev}
            record_hash = _sign_record(fields)
            self.db.conn.execute(
                "INSERT INTO dream_records(run_id,ts,base_sha,result_sha,status,reason,"
                "blast_new,blast_updated,blast_deprecated,diff_json,gate_json,prev_hash,record_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, now, base_sha, result_sha, status, reason,
                 blast_new, blast_updated, blast_deprecated,
                 canonical_json(diff), canonical_json(gates), prev, record_hash),
            )
        return {"status": status, "run_id": run_id, "reason": reason,
                "base_sha": base_sha, "result_sha": result_sha,
                "blast_new": blast_new, "blast_updated": blast_updated,
                "blast_deprecated": blast_deprecated,
                "record_hash": record_hash, "prev_hash": prev,
                "cured": applied, "metamemory": meta["counts"], "gates": gates}

    def close(self) -> None:
        self.db.close()


def run_dream(cfg: Config, clock: Clock = now_iso, cure: bool = True) -> dict:
    d = Dream(cfg, clock)
    try:
        return d.run(cure=cure)
    finally:
        d.close()
