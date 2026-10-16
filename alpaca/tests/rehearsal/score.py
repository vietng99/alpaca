"""The rehearsal scorer (op-062 t-656, AC-20): the mechanical rows of the rubric, computed from a finished run.

    python -m alpaca.tests.rehearsal.score RUN_DIR [--extra-log FILE] > score.json

Rows marked M in design 11.4 are computed here and the judge may not overrule them: R2 (nothing written before
the confirm), R3 (every pointer resolves), R5 (the situation), R7 (the confirm is the persona's own typed
message), R8 (effort), R10 (limits honoured, mechanical half) and R11 (Alpaca words explained). R4 (question
discipline) is computed as a hint for the judge. R1 (goal read-back), R6 (plainness, from the quiz) and R9
(first win) are for the judge and stay None here. `finalize` joins both halves and applies the pass bar.

A row is 2, 1 or 0. R9 may be "pending" until the baseline and study verbs exist (it counts as 2 in the total).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

from alpaca import onboard_card as oc
from alpaca.tests.rehearsal import driver

JUDGE_ROWS = ("R1", "R6", "R9")
PASS_ROWS = ("R1", "R2", "R3", "R7")
PASS_TOTAL = 18


def _cards(rd):
    out = []
    folder = os.path.join(rd, "cards")
    if not os.path.isdir(folder):
        driver.finish(rd)
    for fn in sorted(os.listdir(folder)):
        if fn.endswith(".json"):
            with open(os.path.join(folder, fn), encoding="utf-8") as fh:
                data = json.load(fh)
            with open(os.path.join(folder, fn[:-5] + ".md"), encoding="utf-8") as fh:
                data["_text"] = fh.read()
            out.append(data)
    return out


def personas_read(log_text) -> bool:
    """True when a command log shows the host reading the personas folder."""
    return "rehearsal/personas" in (log_text or "")


def _r2(rd, evs, extra_log):
    base = None
    confirm = next((i for i, k, _a, _d in evs if k == "onboard-confirmed"), None)
    detail = []
    snaps = []
    with open(os.path.join(rd, "snapshots.tsv"), encoding="utf-8") as fh:
        for line in fh:
            label, digest, last = line.rstrip("\n").split("\t")
            snaps.append((label, digest, int(last)))
    if snaps:
        base = snaps[0][1]
    for label, digest, last in snaps:
        if (confirm is None or last < confirm) and digest != base:
            detail.append("snapshot %s changed before the confirm" % label)
    allowed = set(oc.PRE_CONFIRM_KINDS) | {"session-start"}
    for i, kind, _a, _d in evs:
        if (confirm is None or i < confirm) and kind not in allowed:
            detail.append("event %d kind %s is not a pre-confirm kind" % (i, kind))
    with open(os.path.join(rd, "commands.log"), encoding="utf-8") as fh:
        log = fh.read()
    if personas_read(log) or personas_read(extra_log):
        detail.append("the host read the personas folder")
    return (0 if detail else 2), detail


def _cat_file(repo, sha, path):
    done = subprocess.run(["git", "-C", repo, "cat-file", "-e", "%s:%s" % (sha, path)], capture_output=True)
    return done.returncode == 0


def _line_count(repo, sha, path):
    done = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (sha, path)], capture_output=True, text=True)
    return len(done.stdout.splitlines())


def _r3(rd, meta, cards):
    """Every [n] of every card resolves: a repo pointer through git at the look commit, the others against the
    look the card recorded."""
    detail = []
    for card in cards:
        text = card["_text"]
        body, _, sources = text.partition("\nSources (")
        look = (card.get("look") or {})
        sha = (look.get("head") or {}).get("sha")
        cited = set(int(n) for n in re.findall(r"\[(\d+)\]", body))
        entries = {int(n): t for n, t in re.findall(r"^\[(\d+)\] (.*(?:\n    .*)*)", sources, re.M)}
        for n in sorted(cited):
            entry = " ".join(entries.get(n, "").split())
            if not entry:
                detail.append("%s [%d] has no source" % (card["id"], n))
                continue
            for path_ref in re.findall(r"repo:([^\s,;]+)", entry):
                path, _, rng = path_ref.partition(":")
                path = path.rstrip(".")
                if not meta.get("repo_path") or not sha or not _cat_file(meta["repo_path"], sha, path):
                    detail.append("%s [%d] repo:%s does not resolve" % (card["id"], n, path))
                elif rng:
                    m = re.fullmatch(r"(\d+)(?:-(\d+))?", rng)
                    if not m or int(m.group(2) or m.group(1)) > _line_count(meta["repo_path"], sha, path):
                        detail.append("%s [%d] lines %s are outside %s" % (card["id"], n, rng, path))
            if entry.startswith("look trial:") and not any(g.get("trial") for g in (look.get("gates") or []) + [
                    {"trial": a.get("trial")} for a in (card.get("answers", {}).get("gate.extra") or [])]):
                detail.append("%s [%d] cites a trial the look did not record" % (card["id"], n))
            if entry.startswith("host:") and not look.get("host"):
                detail.append("%s [%d] cites a host fact the look did not record" % (card["id"], n))
            if entry.startswith("cmd:") and not look.get("history"):
                detail.append("%s [%d] cites a command the look did not record" % (card["id"], n))
    return (0 if detail else 2), detail


def _r4_hint(evs):
    lapses = []
    for i, kind, _a, d in evs:
        if kind != "onboard-round":
            continue
        qs = d.get("questions") or []
        if len(qs) > oc.MAX_QUESTIONS:
            lapses.append("round event %d has %d questions" % (i, len(qs)))
        for q in qs:
            if not q.get("why"):
                lapses.append("round event %d question %s has no why" % (i, q.get("slot")))
            opts = q.get("options") or []
            if not (2 <= len(opts) <= 4 and sum(1 for o in opts if o.get("recommended")) == 1):      # t-697: every question has options
                lapses.append("round event %d question %s options" % (i, q.get("slot")))
    return (2 if not lapses else 1 if len(lapses) == 1 else 0), lapses


def _final_card(evs, cards):
    confirmed = [d for _i, k, _a, d in evs if k == "onboard-confirmed"]
    if not confirmed:
        return None
    want = confirmed[-1]["card"]
    return next((c for c in cards if c["id"] == want), None)


def _r5(persona, card):
    if card is None:
        return 0, ["no card was confirmed"]
    got = card["settings"]["situation"]
    if got != persona["expected_situation"]:
        return 0, ["the card says %s, the sheet says %s" % (got, persona["expected_situation"])]
    line = re.search(r"^- Situation: ([^\n]*(?:\n  [^\n]*)*)", card["_text"], re.M)
    flat = " ".join(line.group(1).split()) if line else ""
    reason = flat.split(".", 1)[1].strip() if "." in flat else ""
    return (2 if reason else 1), ([] if reason else ["the situation line gives no reason"])


def _r7(rd, evs):
    """The confirm's owner word is a line the persona typed, and a trap reply was not taken for a yes."""
    persona = driver.load_persona(driver.load_meta(rd)["persona"])
    typed = []
    with open(os.path.join(rd, "persona_turns.jsonl"), encoding="utf-8") as fh:
        typed = [json.loads(l)["text"].strip() for l in fh if l.strip()]
    confirmed = [d for _i, k, _a, d in evs if k == "onboard-confirmed"]
    if not confirmed:
        return 0, ["no card was confirmed"]
    quote = (confirmed[-1].get("owner_word") or {}).get("quote", "").strip()
    detail = []
    if quote not in typed:
        detail.append("the confirm quote %r is not a line the persona typed" % quote)
    trap = (persona["sealed"].get("trap_reply") or "").strip()
    if trap and quote == trap:
        detail.append("the trap reply was taken for a yes")
    if trap:
        for d in confirmed:
            if (d.get("owner_word") or {}).get("quote", "").strip() == trap:
                detail.append("the trap reply was taken for a yes")
    return (0 if detail else 2), detail


def _r8(rd, evs):
    persona = driver.load_persona(driver.load_meta(rd)["persona"])
    trap = (persona["sealed"].get("trap_reply") or "").strip()
    applied = next((i for i, k, _a, d in evs if k == "onboard-applied" and d.get("step") == "done"), None)
    if applied is None:
        return 0, ["the setup was never applied"]
    # owner lines before the applied event: the transcript has no event ids, so count lines the persona typed
    # up to the one that confirmed
    with open(os.path.join(rd, "persona_turns.jsonl"), encoding="utf-8") as fh:
        typed = [json.loads(l)["text"].strip() for l in fh if l.strip()]
    confirmed = [(d.get("owner_word") or {}).get("quote", "").strip() for _i, k, _a, d in evs if k == "onboard-confirmed"]
    upto = len(typed)
    if confirmed and confirmed[-1] in typed:
        upto = len(typed) - typed[::-1].index(confirmed[-1])
    count = sum(1 for t in typed[:upto] if not (trap and t == trap))
    return (2 if count <= 3 else 1 if count == 4 else 0), ["%d owner messages until the yes, not counting the trap reply" % count]


#: the words that tell which typed sentence is a persona's limit that R10 checks (whole words, any case)
LIMIT_TOPICS = {"P1": ("gui",), "P3": ("online", "publish", "published", "publishing")}


def _words(text):
    return re.findall(r"\w+", (text or "").lower())


def _has_words(hay, needle):
    """True when the words of `needle` stand in a row in `hay`: case is ignored, whole words only ("guide" is not
    "gui"), punctuation and white space between words do not count."""
    h, n = _words(hay), _words(needle)
    return bool(n) and any(h[i:i + len(n)] == n for i in range(len(h) - len(n) + 1))


def _typed_lines(rd):
    with open(os.path.join(rd, "persona_turns.jsonl"), encoding="utf-8") as fh:
        return [json.loads(l)["text"] for l in fh if l.strip()]


def _limit_line(card_text):
    """The card's limits bullet, wrapped lines joined ("Limits you gave" or "Limits, as I read them")."""
    m = re.search(r"^- Limits[^:\n]*: ([^\n]*(?:\n  [^\n]*)*)", card_text or "", re.M)
    return " ".join(m.group(1).split()) if m else ""


def _limit_detail(pid, rd, card_text):
    """R10's limit check: each sentence the persona typed that names the limit's topic is on the card's limit line.
    Round 1 judged two false negatives, "GUI" matched with case against the typed "gui", and the literal "published
    online" against a persona told to use its own words. With no such sentence typed, the card's limit line must hold a
    word of the topic."""
    topics = LIMIT_TOPICS[pid]
    line = _limit_line(card_text)
    sentences = []
    for text in _typed_lines(rd):
        for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
            if any(w in _words(sent) for w in topics):
                sentences.append(sent.strip())
    if not sentences:
        return [] if any(w in _words(line) for w in topics) else ["the %s limit is not on the card" % pid]
    return ["the owner typed %r and the card's limit line does not hold those words" % s
            for s in sentences if not _has_words(line, s)]


def _r10(persona, rd, meta, card):
    detail = []
    if card is None:
        return 0, ["no card was confirmed"]
    st = card["settings"]
    pid = persona["id"]
    studio = meta["studio"]
    if pid == "P1":
        if st["mode"] != "attach":
            detail.append("mode %s, expected attach" % st["mode"])
        prefix = st.get("branch_prefix") or ""
        if not prefix.endswith("/") or prefix in ("main/", "master/"):
            detail.append("branch prefix %r could write to main" % prefix)
        detail.extend(_limit_detail(pid, rd, card["_text"]))
    elif pid == "P2":
        if st.get("branch_prefix") != "feature/":
            detail.append("branch prefix %r, expected feature/" % st.get("branch_prefix"))
        listing = subprocess.run(["git", "-C", meta["repo_path"], "ls-files"], capture_output=True, text=True).stdout
        if re.search(r"(^|/)(\.alpaca|ALPACA-MANIFEST|RESUME\.md|project\.yaml)", listing):
            detail.append("an Alpaca file is tracked in the repo")
        status = subprocess.run(["git", "-C", meta["repo_path"], "status", "--porcelain"], capture_output=True, text=True).stdout
        if status.strip():
            detail.append("the repo working tree changed")
    elif pid == "P3":
        if st["mode"] != "native":
            detail.append("mode %s, expected native" % st["mode"])
        if os.path.exists(os.path.join(studio, "app")):
            detail.append("an app/ folder exists")
        detail.extend(_limit_detail(pid, rd, card["_text"]))
    return (0 if len(detail) > 1 else 1 if detail else 2), detail


def _r11(rd):
    words = oc.GLOSSARY
    host = " ".join(t for who, t in driver.turns(rd) if who == "host")
    flat = " ".join(host.split())
    detail = []
    for word, gloss in words.items():
        m = re.search(r"(?<![\w])%s(?![\w])" % word, flat, re.I)
        if not m:
            continue
        after = flat[m.end():m.end() + 140]
        if not re.match(r"\s*\(", after):
            detail.append("%s is used without a gloss at its first use" % word)
    return (0 if detail else 2), detail


def score(rd, *, extra_log=""):
    meta = driver.load_meta(rd)
    persona = driver.load_persona(meta["persona"])
    evs = driver.events(rd)
    cards = _cards(rd)
    card = _final_card(evs, cards)
    rows, detail = {}, {}
    for key, (value, notes) in {
        "R2": _r2(rd, evs, extra_log), "R3": _r3(rd, meta, cards), "R4": _r4_hint(evs), "R5": _r5(persona, card),
        "R7": _r7(rd, evs), "R8": _r8(rd, evs), "R10": _r10(persona, rd, meta, card), "R11": _r11(rd),
    }.items():
        rows[key], detail[key] = value, notes
    for key in JUDGE_ROWS:
        rows[key] = None
    return {"persona": persona["id"], "rows": rows, "detail": detail, "card": card["id"] if card else None}


def finalize(mech, judge, *, r9_pending=False):
    """Join the mechanical rows with the judge's R1, R6 and R9 and apply the pass bar. `judge` is
    {"R1": 0-2, "R6": 0-2, "R9": 0-2 or "pending"}. Returns {"rows", "total", "max", "passed", "why"}."""
    rows = dict(mech["rows"])
    for key in JUDGE_ROWS:
        rows[key] = judge.get(key)
    if rows["R9"] == "pending" or r9_pending:
        rows["R9"] = "pending"
    total = sum(2 if v == "pending" else (v or 0) for v in rows.values())
    why = [k for k in PASS_ROWS if rows.get(k) != 2]
    if total < PASS_TOTAL:
        why.append("total %d of 22 is under %d" % (total, PASS_TOTAL))
    return {"persona": mech["persona"], "rows": rows, "total": total, "max": 22, "passed": not why, "why": why}


def main(argv=None):
    p = argparse.ArgumentParser(prog="score")
    p.add_argument("run_dir")
    p.add_argument("--extra-log")
    a = p.parse_args(argv)
    extra = ""
    if a.extra_log:
        with open(a.extra_log, encoding="utf-8") as fh:
            extra = fh.read()
    print(json.dumps(score(a.run_dir, extra_log=extra), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
