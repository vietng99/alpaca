"""engine/loop - the single retrieval controller (§5): plan -> traverse -> verify -> refine ->
answer/abstain. All R1 guards live on this one path: the as-of clause in the SQL builder, the
guarded retrieval surface (only retrieve imports store.read), the code-as-verify oracle, and the
unskippable ledger write. Workers, /query, and sub-agents all re-enter this exact function.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from time import perf_counter

from ..config import Config
from ..determinism import determinism_hash, sha256_hex
from ..store.asof import AsOf
from ..store.db import DB
from ..store import query as q
from ..store.textnorm import norm_surface
from ..types import Answer, CurrencyStamp
from .oracle import entailment_gate, evaluate
from .ledger import persist
# Alpaca M2.11: the Engine's RANKING leg is restored THROUGH the read door. This orchestrator is the
# ORCHESTRATORS carve-out in alpaca/wiki/guards.py: it reaches the ENGINE ranking modules (plan, fuse,
# refine, retrieve) but never the raw STORE primitives (store.fts, store.vec), which stay guarded
# against it. The vector arm's table/extension setup is carried through the door by
# retrieve.ensure_vector_index, so this controller never imports store.vec directly.
from . import fuse, refine, retrieve
from .plan import classify
from ..project.freshness import assert_current_snapshot, assert_fresh
# The provider registry is deliberately NOT imported here. The engine stays BLIND to providers
# (M2.16 asserts no module under alpaca/wiki/engine imports a provider module), so the Engine is handed
# its provider bundle from OUTSIDE alpaca/wiki/engine at the answer-door seam: cfg.providers(). The
# freshness gate above is NOT a provider, so importing it does not touch that blindness.


@dataclass
class _Hit:
    """A minimal cited-block stand-in (block_id + text) for the r1.3 successor-follow re-selection."""
    block_id: str
    text: str


@dataclass
class Assembled:
    answer_text: str
    claims: list[dict]
    cited_blocks: dict[str, str]
    used_edge_ids: list[str]
    read_edge_ids: set = field(default_factory=set)
    answer_nodes: list[str] = field(default_factory=list)
    source_trust: str = "evidence-only"


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg)
        self.db.pour()                         # idempotent; safe if already poured
        # Vector arm OFF under the NARROW default: only a vector/graph profile sets up store.vec, and
        # even then it is reached THROUGH the read door (retrieve.ensure_vector_index), never here.
        if str(self.cfg.meta.get("retrieval_profile", "narrow")) in ("hybrid", "full"):
            retrieve.ensure_vector_index(self.db)
        self.providers = cfg.providers()

    def close(self) -> None:
        self.db.close()

    # ---- THE ONE DOOR ---------------------------------------------------
    def answer(self, question: str, as_of: str | None = None,
               knowledge_as_of: str | None = None, wiki_blind: bool = False) -> Answer:
        """`knowledge_as_of` (F8) pins the TRANSACTION axis at an explicit knowledge-time K, making
        the audit/delta read reachable through the one door (default None = latest-knowledge)."""
        t0 = perf_counter()
        # ---- freshness.dm9/dm6 ANSWER-DOOR fail-closed gate -------------------------------------
        # Never answer off a drifted store: if this vault carries compiled artifacts, they MUST match
        # the DB (assert_fresh) and the snapshot MUST be current (assert_current_snapshot). A stale or
        # hand-edited projection FAILS CLOSED here (raise) rather than silently backing a served
        # answer. A vault with no compiled projection has nothing to drift, so the gate stands down.
        if self.cfg.projection_dir.exists():
            assert_current_snapshot(self.cfg)
            assert_fresh(self.cfg)
        plan = classify(question, as_of)
        K = knowledge_as_of or plan.knowledge_time
        asof = AsOf(plan.as_of, K=K)
        currency = CurrencyStamp(as_of=plan.as_of, txn_axis=("latest" if K is None else K),
                                 resolved_from=plan.resolved_from)
        rrf_k = int(self.cfg.meta.get("rrf_k", 60))
        authority_unit = self._authority_unit()
        retrieval_profile = str(self.cfg.meta.get("retrieval_profile", "narrow"))
        if retrieval_profile not in ("narrow", "hybrid", "full"):
            raise ValueError("retrieval_profile must be narrow, hybrid, or full")
        use_vector = retrieval_profile in ("hybrid", "full")
        use_graph = retrieval_profile == "full"
        steps = refine.schedule(self.cfg.refine_budget)

        def run(retr):
            ranked = fuse.fuse(retr.hits, retr.channel_lists, self.providers.reranker, question,
                               rrf_k, authority_unit=authority_unit)
            served = [h.block_id for h in ranked[:8]]        # F6: floor checked vs the SERVED window
            asm = self._assemble(
                plan, ranked, retr, asof, pull_raw=False,
                graph_authoritative=use_graph,
            )
            required_subclaims = plan.subclaims
            if (len(required_subclaims) == 1
                    and len(re.findall(r"[^\W_]+", required_subclaims[0], re.UNICODE)) <= 1):
                required_subclaims = None
            ores = evaluate(self.db, self.providers.entailer, asof, plan.templates,
                            asm.claims, asm.cited_blocks, asm.used_edge_ids, asm.read_edge_ids,
                            asm.answer_nodes, retr.floor_ids, served,
                            required_subclaims=required_subclaims)
            return ores, asm, [h.block_id for h in ranked]

        best = None
        fast = False
        pass_no = 0
        # FAST-PATH (F13): single-hop lexical tries WITHOUT PPR first; if it grounds, PPR is skipped.
        if plan.qtype == "single-hop-lexical":
            retr = retrieve.gather(self.db, self.providers, question, asof, k=steps[0].k,
                                   skip_ppr=True, domain=plan.domain, mode=plan.mode,
                                   wiki_blind=wiki_blind, use_vector=use_vector)
            ores, asm, final_ids = run(retr)
            if ores.verdict == "grounded" and retr.floor_ids:
                best, fast = (ores, asm, retr, final_ids), True

        if best is None:
            for i, step in enumerate(steps):
                retr = retrieve.gather(self.db, self.providers, question, asof,
                                       k=step.k, alpha=step.alpha, domain=plan.domain,
                                       mode=plan.mode, wiki_blind=wiki_blind,
                                       skip_ppr=not use_graph, use_vector=use_vector)
                ores, asm, final_ids = run(retr)
                best = (ores, asm, retr, final_ids)
                pass_no = i
                if ores.verdict in ("grounded", "conflict"):
                    break

        ores, asm, retr, final_ids = best
        verdict = "abstained" if ores.verdict in ("insufficient", "abstained") else ores.verdict

        # ---- retrieval-one-door.1 AMBIGUITY VERDICT --------------------------------------------
        # When a single bare surface in the question resolves to >=2 distinct canonical entities,
        # each carrying a definition/claim floor block, and the question does not disambiguate, we
        # MUST NOT confidently ground on one arbitrary entity. Keep the frozen Answer contract (three
        # verdicts) but route to abstain and surface both entities in extra['ambiguous_entities'].
        ambiguous = self._detect_ambiguity(question)
        if ambiguous:
            verdict = "abstained"

        answer_text = asm.answer_text if verdict in ("grounded", "conflict") else None

        # ---- oracle-safety.3 ENTAILMENT DOOR: refuse-to-invent on the DELIVERED sentence ---------
        # Every sentence we are about to SERVE must be entailed by at least one CITED WINNER text
        # (the block(s) the answer cites). An un-entailed sentence is DROPPED, never delivered as
        # grounded; if nothing survives, we FAIL CLOSED to abstain rather than serve an ungrounded
        # answer. This is the answer-side twin of the per-claim oracle check.
        entail_flagged: list[str] = []
        if answer_text is not None:
            winner_texts = list(asm.cited_blocks.values())
            kept, entail_flagged = entailment_gate(answer_text, winner_texts, self.providers.entailer.entail)
            if entail_flagged:
                answer_text = " ".join(kept) if kept else None
                if answer_text is None:
                    verdict = "abstained"

        channels = {}
        for h in retr.hits:
            for ch in h.channels:
                channels[ch] = channels.get(ch, 0) + 1
        model_versions = {"embed": self.providers.embedder.name,
                          "rerank": self.providers.reranker.name,
                          "entail": self.providers.entailer.name}
        # F11: fold a STABLE currency token, never the wall-clock now. A question with no explicit
        # date resolves as_of to "now"; hashing that raw timestamp made the hash flip across a second
        # boundary. Hash the resolution KIND for now-queries; the literal T only when it is explicit.
        as_of_token = "NOW" if plan.resolved_from == "now" else plan.as_of
        dhash = determinism_hash({
            "answer_text": answer_text, "as_of": as_of_token, "knowledge_as_of": K or "latest",
            "verdict": verdict, "final_ids": final_ids[:20],
            "citations": [(c.source_block_id, c.source_edge_id, c.verdict) for c in ores.citations],
        })
        # ---- knowledge-model.5 AS-OF GATE LEDGER COLUMNS ---------------------------------------
        # Record whether the as-of gate EXCLUDED a superseded/expired row on the answer's nodes
        # (asof_gated) and whether any SERVED claim was itself stale (stale_serve - must stay 0).
        asof_gated, stale_serve = self._asof_gate_flags(asm.answer_nodes, asm.used_edge_ids, asof)

        # r1.2 mode-tagged replay trace {qtype, mode, pass, candidates, complete, ...}
        retrieval_trace = {
            "qtype": plan.qtype, "mode": plan.mode, "pass": pass_no,
            "candidates": final_ids[:20], "complete": verdict in ("grounded", "conflict"),
            "ladder": retr.ladder or [], "raw_fallthrough": retr.raw_fallthrough,
            "final_ids": final_ids[:20], "floor": retr.floor_ids, "linked": retr.linked_nodes,
            "wiki_blind": bool(wiki_blind),
            "retrieval_profile": retrieval_profile,
        }
        extra = {"qtype": plan.qtype, "mode": plan.mode, "reason": ores.reason,
                 "conflict_note": ores.conflict_note, "fast_path": fast,
                 "unmet_predicate": ores.completeness.unmet_predicate,
                 "successor_read": ores.completeness.successor_read,
                 "raw_fallthrough": retr.raw_fallthrough,
                 "asof_gated": asof_gated, "determinism_hash": dhash,
                 "source_trust": asm.source_trust}
        if ambiguous:
            extra["ambiguous_entities"] = ambiguous
            extra["reason"] = "ambiguous entity: query resolves to >=2 distinct canonical entities"
        ans = Answer(
            question=question, verdict=verdict, answer_text=answer_text,
            currency_stamp=currency, completeness=ores.completeness,
            citations=tuple(ores.citations),
            extra=extra,
        )
        latency_ms = int((perf_counter() - t0) * 1000)
        persist(self.db, ans, qtype=plan.qtype, query_hash=sha256_hex(question),
                dag=ores.dag, channels=channels, model_versions=model_versions,
                determinism_hash=dhash, fast_path=fast, latency_ms=latency_ms,
                retrieval_trace=retrieval_trace, asof_gated=asof_gated, stale_serve=stale_serve)
        return ans

    # ---- C12 authority prior knob ----------------------------------------------------------
    def _authority_unit(self) -> float:
        """The scale of the bounded additive authority prior, read from THIS vault's meta.

        DEFAULT 0.0, and the default is the compatibility contract: a vault that never pins the
        knob fuses exactly as the pre-change engine did, so no existing store's determinism hash
        moves. `authority_unit` is deliberately absent from config.META_DEFAULTS - seeding it there
        would write a new `meta` row into every stock vault at pour, which is a change to vault
        bytes, not a no-op.

        cfg.meta is consulted first (the `rrf_k` idiom above, so a rune.toml `[meta]` override
        keeps working), then the DB's own meta table - because a brain vault pins the knob ON DISK
        at pour, and a caller that opened that vault with a freshly built bare Config would
        otherwise serve with the prior silently switched off. A guard that can be disabled without
        anyone noticing is the failure this layer exists to prevent, so the on-disk pin is honoured
        rather than shadowed by an unset default.

        A malformed value raises here instead of degrading to 0.0: the same posture as the rrf_k
        read, and a loud stop beats a silently un-prioritised serve.
        """
        raw = self.cfg.meta.get("authority_unit")
        if raw is None:
            raw = self.db.get_meta("authority_unit")
        value = float(raw) if raw is not None else 0.0
        if not math.isfinite(value) or value < 0:
            raise ValueError("authority_unit must be a finite non-negative number")
        return value

    # ---- retrieval-one-door.1 ambiguity detector -------------------------------------------
    def _detect_ambiguity(self, question: str) -> list[str] | None:
        """A bare surface in the question that resolves to >=2 distinct canonical entities, each
        carrying a definition/claim floor block, with no disambiguator => ambiguous. Uses the shared
        alias table (the wave-1 km.2 resolver's identity surface); never auto-picks one entity."""
        nq = norm_surface(question)
        tokens = set(nq.split())
        rows = self.db.conn.execute(
            "SELECT norm_surface, node_id FROM aliases WHERE status='bound'").fetchall()
        by_surface: dict[str, set] = {}
        for r in rows:
            ns = r["norm_surface"]
            if ns and (ns in nq or ns in tokens):
                by_surface.setdefault(ns, set()).add(q.resolve_canonical(self.db, r["node_id"]))
        # a LONGER surface present in the question that resolves to exactly one node disambiguates.
        disambiguators = {next(iter(nodes)) for s, nodes in by_surface.items() if len(nodes) == 1}
        for ns in sorted(by_surface):
            nodes = by_surface[ns]
            qualified = sorted(n for n in nodes if self._has_floor_block(n))
            if len(qualified) >= 2 and not (set(qualified) & disambiguators):
                return qualified
        return None

    def _has_floor_block(self, node_id: str) -> bool:
        r = self.db.conn.execute(
            "SELECT 1 FROM node_blocks nb JOIN blocks b ON b.block_id=nb.block_id "
            "WHERE nb.node_id=? AND nb.role IN ('definition','claim') AND b.status='active' LIMIT 1",
            (node_id,)).fetchone()
        return r is not None

    # ---- knowledge-model.5 as-of gate flags ------------------------------------------------
    def _asof_gate_flags(self, answer_nodes: list[str], used_edge_ids: list[str],
                         asof: AsOf) -> tuple[int, int]:
        """asof_gated=1 iff the as-of gate excluded a superseded/expired/invalidated edge on the
        answer's nodes; stale_serve=1 iff a SERVED (cited) edge is itself stale - must stay 0."""
        asof_gated = 0
        for n in answer_nodes:
            row = self.db.conn.execute(
                "SELECT COUNT(*) AS c FROM edges WHERE (subj_node=? OR obj_node=?) AND "
                "(superseded_at IS NOT NULL OR status<>'active' OR "
                " (expires_at IS NOT NULL AND expires_at <= ?))",
                (n, n, asof.T)).fetchone()
            if row and row["c"] > 0:
                asof_gated = 1
                break
        stale_serve = 0
        for eid in used_edge_ids:
            e = q.get_edge(self.db, eid)
            if e and (e.get("superseded_at") or e.get("status") != "active" or
                      (e.get("expires_at") and e["expires_at"] <= asof.T)):
                stale_serve = 1
                break
        return asof_gated, stale_serve

    # ---- retrieval-one-door.3 successor-follow helper --------------------------------------
    def _follow_active_successor(self, top, asof: AsOf):
        """For each top block, read its edges UNFILTERED; if one is superseded, walk superseded_by
        to the ACTIVE tip and return (successor_edge, its_block). Returns (None, None) if none."""
        for h in top:
            rows = self.db.conn.execute(
                "SELECT * FROM edges WHERE source_block_id=?", (h.block_id,)).fetchall()
            for e in (dict(r) for r in rows):
                if not e.get("superseded_by_edge_id"):
                    continue
                successor_id = e["superseded_by_edge_id"]
                seen: set[str] = set()
                while successor_id and successor_id not in seen and len(seen) < 64:
                    seen.add(successor_id)
                    succ = q.get_edge(self.db, successor_id)
                    if not succ:
                        break
                    if self._edge_current(succ, asof):
                        sb = q.get_block(self.db, succ["source_block_id"])
                        if sb:
                            return succ, _Hit(sb["block_id"], sb["text"])
                    successor_id = succ.get("superseded_by_edge_id")
        return None, None

    def _edge_current(self, e: dict, asof: AsOf) -> bool:
        """Apply domain, transaction, status, and expiry currency to one edge."""
        T = asof.T
        domain_ok = ((e.get("valid_from") or "") <= T
                     and (not e.get("valid_until") or T < e["valid_until"]))
        expiry_ok = not e.get("expires_at") or T < e["expires_at"]
        if asof.K is None:
            txn_ok = not e.get("superseded_at") and e.get("status") == "active"
        else:
            txn_ok = ((e.get("recorded_at") or "") <= asof.K
                      and (not e.get("superseded_at") or asof.K < e["superseded_at"])
                      and e.get("status") != "retracted")
        return domain_ok and txn_ok and expiry_ok

    # ---- assembly (deterministic, extractive; the LLM writes no scalar) -
    def _assemble(self, plan, ranked, retr, asof: AsOf, pull_raw: bool,
                  graph_authoritative: bool = True) -> Assembled:
        """Build the DRAFT answer and decompose it into claims verified against their cited block.

        Claims come from the DRAFT, not the raw question (§5 STEP 3). An edge-backed answer claims
        the edge's verbatim source_quote (entailed by its own block); an extractive answer claims
        the cited block's own text. Relevance is enforced by RANKING; the oracle enforces
        grounded-to-source + no-omission. Empty retrieval -> no claims -> abstain (§6).
        """
        # relevance gate (§6 ABSTENTION: empty *relevant* retrieval -> abstain). A block is relevant
        # if it is an exact-floor hit or shares a query content term. This stops a stub embedder's
        # spurious cosine from grounding an unknown question on an unrelated block.
        stop = {
            "what", "when", "where", "which", "who", "why", "how", "does", "did",
            "the", "and", "for", "from", "with", "about", "tell", "please",
            "cái", "gì", "khi", "nào", "đâu", "sao", "cho", "với", "của", "và",
        }
        qterms = {term for term in plan.terms if term not in stop}
        def _relevant(h) -> bool:
            if h.exact_floor:
                return True
            htoks = set(re.findall(r"[^\W_]+", h.text.lower(), re.UNICODE))
            shared = qterms & htoks
            required = 1 if len(qterms) <= 1 else 2
            return len(shared) >= required

        top = [h for h in ranked[:8] if _relevant(h)]
        if not top:
            return Assembled("", [], {}, [], set(), list(retr.linked_nodes))
        top_ids = [h.block_id for h in top]

        read_edges: set = set()
        answer_nodes = list(retr.linked_nodes)
        for n in retr.linked_nodes:
            for e in q.active_edges_for_node(self.db, n, asof):
                read_edges.add(e["edge_id"])
        temporal_routing = plan.qtype in ("as-of-currency", "delta")
        block_edges = q.edges_for_blocks(self.db, top_ids, asof) \
            if graph_authoritative or temporal_routing else []
        edge_by_block: dict[str, dict] = {}
        for e in block_edges:
            read_edges.add(e["edge_id"])
            answer_nodes.append(e["subj_node"])
            if e["obj_node"]:
                answer_nodes.append(e["obj_node"])
            edge_by_block.setdefault(e["source_block_id"], e)
        answer_nodes = sorted(set(answer_nodes))

        # pick the answer block: prefer a top block that carries an edge touching a linked node,
        # else the highest-ranked block that carries any edge, else the top block (extractive).
        linked = set(retr.linked_nodes)
        chosen_h, chosen_e = None, None
        for h in top:
            e = edge_by_block.get(h.block_id)
            if e and (not linked or e["subj_node"] in linked or e["obj_node"] in linked):
                chosen_h = h
                chosen_e = e if graph_authoritative else None
                break
        if chosen_h is None:
            for h in top:
                if h.block_id in edge_by_block:
                    chosen_h = h
                    chosen_e = edge_by_block[h.block_id] if graph_authoritative else None
                    break

        # ---- retrieval-one-door.3 ACTIVE-SUCCESSOR HOP (NORTH STAR) ---------------------------
        # A status query may surface only the SUPERSEDED block (as-of gate hid its stale edge, so no
        # active edge was anchored). Rather than abstain, follow superseded_by to the ACTIVE
        # successor and RE-SELECT the answer from the current non-superseded edge - never serve the
        # superseded edge as current. successor_read then legitimately passes (serve GREEN).
        if chosen_e is None:
            hop_e, hop_block = self._follow_active_successor(top, asof)
            if hop_e is not None:
                chosen_h = hop_block
                if graph_authoritative:
                    chosen_e = hop_e
                read_edges.add(hop_e["edge_id"])
                answer_nodes = sorted(set(answer_nodes) | {hop_e["subj_node"]} |
                                      ({hop_e["obj_node"]} if hop_e["obj_node"] else set()))

        if chosen_h is None:
            chosen_h = top[0]

        cited_blocks = {chosen_h.block_id: chosen_h.text}
        used_edge_ids: list[str] = []
        if chosen_e:
            obj = chosen_e["obj_node"] or chosen_e["obj_literal"]
            kind = "date" if chosen_e["obj_datatype"] == "date" else \
                   "number" if chosen_e["obj_datatype"] == "number" else "prose"
            claims = [{"text": chosen_e["source_quote"], "kind": kind,
                       "source_block_id": chosen_h.block_id, "source_edge_id": chosen_e["edge_id"]}]
            used_edge_ids = [chosen_e["edge_id"]]
            answer_text = f"{chosen_e['subj_node']} {chosen_e['predicate']} {obj}"
        else:
            snippet = chosen_h.text.strip()
            claims = [{"text": snippet[:300], "kind": "prose",
                       "source_block_id": chosen_h.block_id, "source_edge_id": None}]
            answer_text = snippet[:400]
        return Assembled(answer_text=answer_text, claims=claims, cited_blocks=cited_blocks,
                         used_edge_ids=used_edge_ids, read_edge_ids=read_edges,
                         answer_nodes=answer_nodes, source_trust=chosen_h.source_trust)
