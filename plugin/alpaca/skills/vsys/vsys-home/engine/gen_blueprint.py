#!/usr/bin/env python3
# VSYS generic blueprint builder.
#
# Turns a traced dataset (data/<target>.json) + a Profile (profiles/<domain>.json) into:
#   - <target>.md   : the EXHAUSTIVE source of truth (every component, every evidence pointer)
#   - <target>.html : the drawable blueprint (every node wired, semantic zoom) via the Engine
#
# This is the DRAW half of VSYS. It is target-agnostic: all domain vocabulary (area names, node
# types, the optional benchmark axes) lives in the Profile, never here. See TRACE-CONVENTION.md.
#
# Usage:
#   gen_blueprint.py <traced.json> <profile.json> <outdir> [engine.html] [buckets.json] \
#                    [blockconn.json] [design2out.json]
#
# traced.json schema (produced by the tracer, TRACE-CONVENTION.md sec 3):
#   { "areas":  [ {"area": id, "nodes":[...], "edges":[...], "coverage":[...]} , ... ],
#     "cross":  {"edges":[...]},              # optional inter-area wires
#     "critic": {"misses":[...], "uncovered_dims":[...], "verdict": "..."},   # optional
#     "stats":  {...},                        # optional
#     "dvflow": {"steps":[...], "edges":[...]},   # optional flow-lane area (steps -> nodes)
#     "dropin": {"nodes":[...], "edges":[...], "coverage":[...]} }  # optional extra area
#
# Node fields (raw, per convention): id,label,what,raw_type,shape,state,who,file,evidence,dims,
#   cluster,notes.  Edge fields: from,to,what,raw_kind,evidence,dash.

import json, sys, pathlib
from collections import Counter

TRACED   = sys.argv[1] if len(sys.argv) > 1 else "traced.json"
PROFILE  = sys.argv[2] if len(sys.argv) > 2 else "profile.json"
OUTDIR   = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else pathlib.Path(".")
TEMPLATE = sys.argv[4] if len(sys.argv) > 4 else "blueprint-engine.html"
BUCKETS  = sys.argv[5] if len(sys.argv) > 5 else None
BLOCKCONN = sys.argv[6] if len(sys.argv) > 6 else None
DESIGN2  = sys.argv[7] if len(sys.argv) > 7 else None

d  = json.load(open(TRACED, encoding="utf-8"))
pf = json.load(open(PROFILE, encoding="utf-8"))

TARGET = pf.get("target") or pf.get("name") or "system"
TITLE  = pf.get("blueprintTitle") or (pf.get("title", "System") + " Blueprint")
SUBTITLE = pf.get("blueprintSubtitle") or "traced system &middot; dimension flow"
REPO   = (pf.get("repoRoot") or "").rstrip("/")
REPO   = (REPO + "/") if REPO else ""

# ---- profile-driven area vocabulary ----
# profile.areas: [{id, name, type, note?}] in display order. Areas present in the trace but not in
# the profile are appended (logged), never dropped.
PAREAS = pf.get("areas", [])
AREA_NAME = {a["id"]: a.get("name", a["id"]) for a in PAREAS}
AREA_TYPE = {a["id"]: a.get("type", a.get("color", "flow")) for a in PAREAS}
AREA_WHAT = {a["id"]: a.get("note", "") for a in PAREAS}
ORDER     = [a["id"] for a in PAREAS]

# optional benchmark axes (a larger target may ship many; a plain target ships none)
BENCH = [(b["n"], b["title"], b.get("wt", 0)) for b in pf.get("bench", [])]
BTITLE = {n: t for n, t, _ in BENCH}

def rel(p):
    p = (p or "").strip()
    return p[len(REPO):] if (REPO and p.startswith(REPO)) else p

def norm_nodes(nodes):
    out = []
    for n in nodes or []:
        rt = n.get("raw_type") or n.get("type") or "flow"
        out.append({
            "id": n["id"], "title": n.get("label") or n.get("title") or n["id"],
            "type": rt, "shape": n.get("shape") or ("diamond" if rt == "gate" else "card"),
            "state": n.get("state") or "specced", "who": n.get("who") or "gear",
            "file": rel(n.get("file", "")), "evidence": rel(n.get("evidence", "")),
            "dims": sorted(set(n.get("dims") or [])), "role": (rt or "").capitalize(),
            "what": (n.get("what", "") or "").strip(), "notes": (n.get("notes", "") or "").strip(),
        })
    return out

def norm_edges(edges):
    return [{"from": e["from"], "to": e["to"], "what": e.get("what") or e.get("label") or "",
             "raw_kind": e.get("raw_kind", ""), "dash": bool(e.get("dash"))} for e in (edges or [])]

# index the traced areas by id
areas_in = {a["area"]: a for a in d.get("areas", []) if a.get("area")}

# ---- build FULL areas (exhaustive, for MD + HTML) ----
full = []
placed = set()

def add_area(aid, nodes, edges, coverage):
    full.append({"id": aid, "name": AREA_NAME.get(aid, aid), "type": AREA_TYPE.get(aid, "flow"),
                 "what": AREA_WHAT.get(aid, ""), "nodes": nodes, "edges": edges, "coverage": coverage})
    placed.add(aid)

# 1) areas declared in the profile, in order
for aid in ORDER:
    a = areas_in.get(aid)
    if not a:
        continue
    add_area(aid, norm_nodes(a.get("nodes")), norm_edges(a.get("edges")), a.get("coverage", []))

# 2) any traced area not in the profile (append, logged)
extra_areas = [aid for aid in areas_in if aid not in placed]
for aid in extra_areas:
    a = areas_in[aid]
    add_area(aid, norm_nodes(a.get("nodes")), norm_edges(a.get("edges")), a.get("coverage", []))

# 3) optional legacy top-level "dropin" area (extra domain package)
dropin = d.get("dropin") or {}
if dropin.get("nodes"):
    dn = norm_nodes(dropin.get("nodes"))
    for n in dn:
        if not n["id"].startswith("dropin-"):
            n["id"] = "dropin-" + n["id"]
    def _fx(x): return x if x.startswith("dropin-") else "dropin-" + x
    de = [{"from": _fx(e["from"]), "to": _fx(e["to"]), "what": e["what"],
           "raw_kind": e["raw_kind"], "dash": e["dash"]} for e in norm_edges(dropin.get("edges"))]
    add_area("dropin", dn, de, dropin.get("coverage", []))

# 4) optional legacy top-level "dvflow" flow-lane (ordered steps -> nodes)
dv = d.get("dvflow") or {}
if dv.get("steps"):
    ACT = {"dv-engineer": ("flow", "person"), "agent": ("agent", "gear"),
           "gate": ("gate", "gear"), "oracle": ("instrument", "gear")}
    dv_nodes = []
    for s in dv["steps"]:
        t, who = ACT.get(s.get("actor", "agent"), ("flow", "gear"))
        nid = s["id"] if s["id"].startswith("dv-") else "dv-" + s["id"]
        extra = []
        if s.get("artifact"): extra.append("Produces: " + s["artifact"])
        if s.get("hooks_into"): extra.append("Drives: " + ", ".join(s["hooks_into"]))
        dv_nodes.append({"id": nid, "title": s.get("label") or s["id"], "type": t,
            "shape": "diamond" if t == "gate" else "card", "state": s.get("state", "built"),
            "who": who, "file": rel(s.get("file", "")), "evidence": rel(s.get("evidence", "")),
            "dims": sorted(set(s.get("dims") or [])), "role": (s.get("actor", "") or "").replace("-", " ").title(),
            "what": (s.get("what", "") + ("  " + "  ".join(extra) if extra else "")).strip(),
            "notes": "Hooks into: " + ", ".join(s.get("hooks_into", []))})
    def _dv(x): return x if x.startswith("dv-") else "dv-" + x
    dv_edges = [{"from": _dv(e["from"]), "to": _dv(e["to"]), "what": e.get("what", ""),
                 "raw_kind": "flow", "dash": bool(e.get("dash"))} for e in dv.get("edges", [])]
    add_area("dvflow", dv_nodes, dv_edges, [])

# per-area dim rollup
for a in full:
    a["dims"] = sorted({x for n in a["nodes"] for x in n["dims"]})

allnodes = [n for a in full for n in a["nodes"]]
byid = {n["id"]: n for n in allnodes}
cross = [e for e in norm_edges((d.get("cross") or {}).get("edges", [])) if e["from"] in byid and e["to"] in byid]

# ---- HTML draws EVERY node, wired (semantic zoom handles altitude) ----
drawn = {n["id"] for n in allnodes}
draw_areas = [{"id": a["id"], "name": a["name"], "type": a["type"], "dims": a["dims"],
               "what": a["what"], "nodes": a["nodes"],
               "edges": [e for e in a["edges"] if e["from"] in drawn and e["to"] in drawn]} for a in full]
draw_cross = [e for e in cross if e["from"] in drawn and e["to"] in drawn]

# optional block-to-block connections (drawn zoomed-out)
blockedges = []
if BLOCKCONN and pathlib.Path(BLOCKCONN).exists():
    bc = json.load(open(BLOCKCONN, encoding="utf-8"))
    valid = {a["id"] for a in draw_areas}
    for e in bc.get("edges", []):
        if e.get("from") in valid and e.get("to") in valid and e["from"] != e["to"]:
            blockedges.append({"from": e["from"], "to": e["to"], "kind": e.get("kind", ""),
                               "what": e.get("what", ""), "dash": bool(e.get("dash"))})

# optional agentic block layout + per-dimension recaps
blocklayout = None
recaps = {}
if DESIGN2 and pathlib.Path(DESIGN2).exists():
    dz = json.load(open(DESIGN2, encoding="utf-8"))
    lay = dz.get("layout") or {}
    validb = {a["id"] for a in draw_areas}
    pl = {p["id"]: {"col": int(p["col"]), "row": int(p["row"]), "size": float(p.get("size", 1))}
          for p in lay.get("placement", []) if p.get("id") in validb}
    if pl:
        blocklayout = {"cols": int(lay.get("cols", 3)), "placement": pl}
    for rc in (dz.get("recap") or {}).get("recaps", []):
        recaps[str(rc.get("n"))] = {"tldr": rc.get("tldr", ""), "mechanism": rc.get("mechanism", "")}

DATA = {"meta": {"title": TITLE, "subtitle": SUBTITLE},
        "bench": [{"n": n, "title": t, "wt": w} for n, t, w in BENCH],
        "areas": draw_areas, "cross": draw_cross,
        "blockedges": blockedges, "blocklayout": blocklayout, "recaps": recaps}

tpl = open(TEMPLATE, encoding="utf-8").read()
data_json = json.dumps(DATA, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
(OUTDIR / f"{TARGET}.html").write_text(tpl.replace("__DATA_JSON__", data_json), encoding="utf-8")

# ---- honest coverage vs the full in-scope file set (buckets) ----
allfiles = []
FILE_AREA = {}
if BUCKETS and pathlib.Path(BUCKETS).exists():
    buckets = json.load(open(BUCKETS, encoding="utf-8"))
    if isinstance(buckets, dict) and "buckets" in buckets:
        buckets = buckets["buckets"]
    allfiles = sorted({f for v in buckets.values() for f in v})
    FILE_AREA = {f: aid for aid, fs in buckets.items() for f in fs}

disp = {}
for a in full:
    for c in a.get("coverage", []):
        f = rel(c.get("file", ""))
        if f:
            disp.setdefault(f, c.get("disposition", ""))
noded = [f for f in allfiles if f in disp]
folded = [f for f in allfiles if f not in disp]
for f in folded:
    disp[f] = "folded:" + FILE_AREA.get(f, "?")
fold_by = Counter(FILE_AREA.get(f, "?") for f in folded)

# ---- MD source of truth ----
def esc(s): return str(s).replace("|", "\\|").replace("\n", " ").strip()
def w(s=""): L.append(s)
L = []
covered_dims = sorted({x for n in allnodes for x in n["dims"]})
uncov = [n for n, _, _ in BENCH if n not in covered_dims]
critic = d.get("critic") or {}
stats = d.get("stats") or {}

w(f"# {pf.get('title', TARGET)} Blueprint - traced architecture" + (" + benchmark proof-index" if BENCH else ""))
w()
w("> **This Markdown is the source of truth.** The interactive VSYS blueprint "
  f"(`{TARGET}.html`) is drawn from the same traced dataset. Every component below carries a "
  "repo-relative file pointer, a `file:line` evidence pointer, and an honestly-read record state "
  "(Specced / Built / Verified / Overridden).")
w()
w("## 1. Method - how this map was produced")
w()
w("Traced with the VSYS convention (`TRACE-CONVENTION.md`), executed as a background workflow fan-out:")
w()
w(f"1. **Scope** - every in-scope file bucketed into {len(full)} subsystem areas; exclusions logged.")
w("2. **Sweep (BFS)** - a haiku tracer fleet (one per file-chunk) read the real files and captured "
  f"every component as an evidence-pointered node. Over-capture by design: {len(allnodes)} components.")
w("3. **Verify (DFS)** - a fresh adversarial wave per area re-resolved every citation and downgraded "
  "any overclaimed state.")
w("4. **Cross-link** - inter-area wires captured with the full node set known.")
w("5. **Coverage critic** - an independent miss-hunt; its verdict is reproduced in section "
  + ("6" if BENCH else "5") + ".")
w()
w(f"**Totals:** {len(allnodes)} components across {len(full)} areas, {len(cross)} cross-area wires."
  + (f" Coverage: **{len(allfiles)}** in-scope files - **{len(noded)}** given their own node, "
     f"**{len(folded)}** folded into their area (logged). Zero silent drops." if allfiles else ""))
w()

sec = 2
if BENCH:
    w(f"## {sec}. Benchmark proof-index")
    w()
    w("For each benchmark dimension: the components that are its evidence, with file:line pointers.")
    w()
    for n, t, ww in BENCH:
        hits = [x for x in allnodes if n in x["dims"]]
        w(f"### Dim {n} - {t} (weight {ww}) - {len(hits)} evidence components")
        w()
        if not hits:
            w("> No component tagged for this dimension. See the critic section."); w(); continue
        w("| Component | Area | State | Evidence |"); w("|---|---|---|---|")
        for x in hits[:60]:
            area = next((a["name"] for a in full if x in a["nodes"]), "")
            w(f"| {esc(x['title'])} | {esc(area)} | {x['state']} | `{esc(x['evidence'] or x['file'])}` |")
        if len(hits) > 60: w(f"| _+{len(hits)-60} more_ |  |  |  |")
        w()
    sec += 1

w(f"## {sec}. Areas - every component, wired")
w()
for a in full:
    dimtxt = ', '.join('D' + str(x) for x in a['dims']) or 'n/a'
    w(f"### {a['name']} ({len(a['nodes'])} components; dims {dimtxt})")
    w(); w(esc(a["what"])); w()
    w("| Component | Type | State | Who | File | Evidence | Dims | What |")
    w("|---|---|---|---|---|---|---|---|")
    for n in a["nodes"]:
        w(f"| {esc(n['title'])} | {n['type']} | {n['state']} | {n['who']} | `{esc(n['file'])}` | "
          f"`{esc(n['evidence'])}` | {' '.join('D'+str(x) for x in n['dims'])} | {esc(n['what'])[:240]} |")
    if a["edges"]:
        w(); w("_Internal wiring:_ " + "; ".join(
            f"{e['from']} -{(e['raw_kind'] or 'flow')}-> {e['to']}" + (" (override)" if e['dash'] else "")
            for e in a["edges"][:40]))
    w()
sec += 1

w(f"## {sec}. Cross-area wiring")
w(); w("| From | To | Relation |"); w("|---|---|---|")
lab = {n["id"]: n["title"] for n in allnodes}
for e in cross:
    w(f"| {esc(lab.get(e['from'], e['from']))} | {esc(lab.get(e['to'], e['to']))} | "
      f"{esc(e['what'])}{' (override/bypass)' if e['dash'] else ''} |")
w()
sec += 1

w(f"## {sec}. Weakest links, open coverage & the critic (self-flagged)")
w()
spec = [x for x in allnodes if x["state"] == "specced"]
over = [x for x in allnodes if x["state"] == "overridden"]
unres = [x for x in allnodes if "UNRESOLVED" in x.get("notes", "")]
w(f"- **Specced-not-Built:** {len(spec)} components.")
w(f"- **Overridden:** {len(over)} components"
  + ((": " + ", ".join(esc(x['title']) for x in over[:12])) if over else "") + ".")
w(f"- **Unresolved evidence:** {len(unres)}"
  + ((": " + ", ".join(esc(x['title']) for x in unres[:10])) if unres else "") + ".")
if BENCH:
    if uncov: w(f"- **Benchmark dims with no evidence component:** "
                + ", ".join('D' + str(x) + ' ' + BTITLE[x] for x in uncov) + ".")
    else: w("- **Benchmark coverage:** all dimensions have at least one evidence component.")
if allfiles:
    w(f"- **Folded coverage:** {len(noded)} of {len(allfiles)} files have their own node; "
      f"{len(folded)} folded into their area: " + ", ".join(f"{k} ({v})" for k, v in fold_by.most_common()) + ".")
if critic.get("verdict"):
    w(f"- **Coverage-critic verdict:** {esc(critic['verdict'])}")
    for mm in (critic.get("misses") or [])[:20]:
        w(f"    - {esc(mm.get('kind', ''))}: {esc(mm.get('detail', ''))}")
w()
w("---")
w()
w(f"*Rendered view: `{TARGET}.html` (self-contained; pan/zoom, per-area tabs, dimension-flow, "
  "light/dark). Regenerate both from the traced dataset with `gen_blueprint.py`.*")

(OUTDIR / f"{TARGET}.md").write_text("\n".join(L) + "\n", encoding="utf-8")

# ---- coverage ledger (data/<target>.coverage.md) ----
if allfiles:
    excl = [f for f in allfiles if str(disp.get(f, "")).startswith("excluded")]
    C = [f"# {TARGET} coverage ledger", "",
         f"In-scope files: **{len(allfiles)}** - "
         f"**{len(noded)}** with their own node, **{len(folded)}** folded into an area, "
         f"**{len(excl)}** excluded.", "",
         "## Per-area disposition", "", "| Area | files (bucket) | folded | nodes |", "|---|---|---|---|"]
    bucket_ct = Counter(FILE_AREA.values())
    for a in full:
        C.append(f"| {a['name']} ({a['id']}) | {bucket_ct.get(a['id'], 0)} | "
                 f"{fold_by.get(a['id'], 0)} | {len(a['nodes'])} |")
    C += ["", "## Folds (files represented by their area node, not drawn individually)", ""]
    C.append(", ".join(f"{k}: {v}" for k, v in fold_by.most_common()) or "(none)")
    if critic.get("verdict"):
        C += ["", "## Coverage-critic verdict", "", "> " + str(critic["verdict"])]
        for mm in (critic.get("misses") or []):
            C.append(f"- {mm.get('kind', '')}: {mm.get('detail', '')}")
    (OUTDIR / f"{TARGET}.coverage.md").write_text("\n".join(C) + "\n", encoding="utf-8")

print(f"nodes={len(allnodes)} areas={len(full)} cross={len(cross)} "
      + (f"coverage={len(noded)}+{len(folded)}folded/{len(allfiles)} " if allfiles else "")
      + (f"extra_areas={extra_areas} " if extra_areas else "")
      + f"critic={critic.get('verdict', 'n/a')}")
print("wrote", OUTDIR / f"{TARGET}.md", OUTDIR / f"{TARGET}.html")
