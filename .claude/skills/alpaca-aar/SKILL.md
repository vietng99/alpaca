---
name: alpaca-aar
description: >-
  Write, check, review and seal the after-action report of an op. Use when an op closes or stops,
  on "/alpaca-aar <op>", "write the AAR", "after-action report" or "review the AAR". A fresh
  subagent reviews the report against named checks; the main session records accept or rework;
  seal needs an accept for the exact bytes. Lines in Lessons can be proposed to lessons.write,
  whose probe decides.
---

# alpaca-aar

An after-action report has three separate answers, and this skill keeps them apart:

- **Check**: `alpaca aar check <op>` reads the closed line grammar and names what is missing,
  empty or unreadable. A check PASS does not mean seal will pass.
- **Review**: a fresh subagent judges the report against the checks below and returns a review
  file with exact quotes. The review decides nothing.
- **Decision**: the main session records accept or rework. `alpaca aar seal` refuses unless the
  latest decision for the current sha256 of the report is accept.

The code is `alpaca/aar.py`. Session names are attribution, not authentication: a subagent shares
your session id, so the guard is the reviewer id on the packet and the rule that the reviewer runs
no aar verb.

## Main session

1. `alpaca aar new <op>` writes the draft with the Timeline filled from the record.
2. Fill every section: At a glance, Impact, Five whys, Contributing factors, What went well,
   Action items, Lessons. Every claim carries a pointer (event:N, local:path, receipt:, job:,
   remote:). Keep the Carried over rows `aar new` wrote; while an item they list is open, check
   and seal refuse a report without its row. When nothing is open there is no such section.
3. `alpaca aar check <op>` until it prints PASS.
4. `alpaca aar review <op> --reviewer <id>` writes a packet under `.alpaca/aar/<op>.review/`. The id
   names the reviewer subagent (1-80 letters, digits or `. _ : -`) and must differ from your
   session and from `cli`. Use a new id for each packet.
5. Dispatch a fresh subagent (Agent or Task tool) with the packet path, the report path, the
   project root and the Reviewer section below. Give it the raw report, never your view of it or
   an earlier review.
6. Read the review file it returns and open the lines it quotes yourself.
7. When every check passes: `alpaca aar accept <op> --review <file>`. Otherwise
   `alpaca aar rework <op> --reason <what to fix>`, fix the report, go back to step 3.
8. `alpaca aar seal <op>`.
9. Optional, per Lessons item worth keeping: `alpaca aar lesson <op> --line N --probe <file>
   --label <name> --governs <tags> --prescribes <regex>`.

## Reviewer (the subagent)

Read the packet, the whole report and what each pointer names (open the event, the message or the
file). Do not read an earlier review. Judge every check in the list below. Write one JSON file in
your scratch folder:

```
{"op": "op-001", "report_sha256": "<from the packet>", "reviewer": "<from the packet>",
 "checks": {"<check id>": {"result": "pass or fail", "note": "4-600 characters",
                           "quote": {"line": 12, "text": "exact words from that report line"}}},
 "findings": [{"check": "<check id>", "note": "...", "quote": {"line": 12, "text": "..."}}]}
```

Every check id needs an entry. A quote must be copied exactly from the report line it names. The
file is UTF-8 JSON of at most 1 MB, uses the template's keys, never repeats a key in one object and
nests no deeper than 8 levels. Anything else is refused with a named problem, and values from the
file are echoed back cut short. Return the file path, a short summary and what stays uncertain. The reviewer never runs alpaca aar accept,
rework or seal and never edits the report.

## Checks

The four checks of the task, then one check per gap that the mechanical check cannot see (G1-G7,
from the t-621 control pass: `alpaca aar check` reads pointers and cells, not meaning).

- `whys-chain`: each why answers the one before it. None is circular or restates an earlier why.
- `root-cause`: the last why names a process or system cause, not a person or a one-off slip.
- `pointers`: every Timeline and Impact claim has a pointer that resolves to something that says
  what the claim says.
  A pointer is an event id, a message id or a file; open it and compare.
- `actions`: every action item is concrete: a real owner, a due, a size and a done-when a reader
  can test.
- `g1-placeholders`: G1, no owner or done-when that only fills the cell, such as `nobody`,
  `someone`, `x`, `done` or `same`.
- `g2-math-pointers`: G2, no pointer that sits only inside math ($...$), where a viewer hides it.
- `g3-header-facts`: G3, no header fact line (intent, done when, close basis) that carries a claim
  with no pointer.
- `g4-foreign-pointers`: G4, no pointer to another op's event, and no event:N inside a URL.
- `g5-bare-whys`: G5, no why that says only `Why?` beside its pointer.
- `g6-padded-glance`: G6, At a glance says what happened in words, not filler or punctuation to
  reach 40 characters (a padded At a glance).
- `g7-struck-pointers`: G7, no pointer that is struck through (~~...~~).

## Lessons

A Lessons line qualifies when it is a procedural habit, not a fact about the system: lessons.write
refuses a line that states what is true of the domain (the H7 control). The probe file is JSON
lines, each with exactly `{id, tag, outcome, action}`: outcome PASS or FAIL, id and tag 1-80
letters, digits or `. _ : -`, action 1-2000 characters, at least two of each outcome per governed
tag. `--prescribes` is at most 300 characters. A pattern that backtracks badly (for example
`(a+)+$`) can make the probe slow, so keep it a plain alternation of phrases. The report must have a current accept. `alpaca aar lesson` passes the line to lessons.write,
whose probe gate decides. A RejectedLesson is printed with its control and reason and the command
exits non-zero: report it, and do not loosen the regex to get past it. An admitted lesson lands in
lessons_log of the record, and the attempt is kept as an aar-lesson event either way. The same lesson text
cannot be admitted twice, wherever it sits in the report; the same text in two ops is two lessons,
because the op is in the id. A printed rejection reason is cut at 280 characters; lessons_log keeps
it whole.

## Recovery

- An edit after accept needs a new review and a new accept, because the decision binds the sha256.
- A new packet makes older reviews stale. A rework stands until a new packet.
- Reverting the file to bytes that were accepted seals again: the rule reads the latest decision
  for the current sha256.
- Reports sealed before this gate keep their seal.
