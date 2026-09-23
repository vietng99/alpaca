---
name: sam
description: Enforce the plain-writing ban list (SAM). Use when the user runs /sam, says "enforce sam", "sam mode", "ban list", "plain writing", or asks you to stop using AI-slop words and stock phrases. Bans a fixed list of decorative words, canned phrases, and stock sentence structures in your own prose while keeping code, quotations, identifiers, and accurate technical terms intact. Default-on on this machine via a SessionStart hook and a Stop hook that rejects a reply once if it uses a banned term.
---

# SAM: plain-writing ban list enforcement

Write directly, accurately, and naturally. Do not use the banned words, phrases, or stock structures in your own explanatory prose. This is an editorial style rule, not an AI-detector test.

The complete list lives next to this file in `plain-writing-ban-list.md` (162 words, 186 phrases, 28 structure patterns, plus context-dependent words and replacement examples). Read it when you need the full enumeration. The condensed high-frequency set is in `sam-core.md`.

## How enforcement works on this machine

- **Always-on:** `hooks/sam-inject.sh` (SessionStart) injects the ruleset every session when `.alpaca/config/.sam-always` exists.
- **Re-armed as context fills:** the context-band hook re-injects the ruleset at each 10% band, like caveman and adhd.
- **Strictly enforced:** `hooks/sam-stop-guard.sh` (Stop hook) scans your final reply, strips code/inline-code/quotes, and if a banned word or phrase remains it blocks the turn once (exit 2) so you rewrite. Capped at one rewrite per turn.
- **Off switch:** say "stop sam" for the session; set `SAM_MODE=off` in `.alpaca/config/.sam.conf` to disable the Stop hook; delete `.sam-always` to turn always-on off for good. `SAM_MODE=warn` surfaces hits without blocking.

## Application rules

- Match whole words, ignoring capitalization; include grammatical variants and equivalent punctuation. Do not match a banned word inside an unrelated longer word.
- Preserve exact quotations, required source text, code, commands, identifiers, paths, API fields, official names, titles, and accurate citations.
- Keep a banned word when its literal technical meaning is necessary and a substitute would be less accurate (for example `validate` for input validation, `verbatim` for word-for-word reproduction, `load-bearing` in structural engineering, `harness` for a test or agent harness). The Stop guard already exempts these four from hard-blocking.
- Do not replace a banned word with a more obscure synonym. Rewrite the sentence around the specific fact, action, or condition.
- Do not delete an important limitation, warning, uncertainty, or source to make the writing sound simpler.
- Follow the user's requested genre and format. These preferences must not damage a translation, quotation, poem, legal term, or requested example.

## What to do when invoked

1. Apply the ban list to your own prose for the rest of the session.
2. If the user gave text to edit, rewrite it: mark the banned words, phrases, and structures, then produce a version that keeps every supported fact and drops the banned wording. State each point plainly instead of patching flagged phrases one at a time.
3. Do not announce that you checked. Just write clean prose.

## The four rules that catch most cases

1. Start with the answer, result, or required action.
2. State who does what, under which conditions, with what result.
3. Remove praise, staged run-ups, stock transitions, and filler conclusions that add no information.
4. Explain uncertainty specifically; name what is unknown or untested. Stop when the request is answered.
