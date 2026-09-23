---
name: capture
description: Conclude the entire current session into one polished, self-contained, phone-safe HTML report - the ideas explored, the tracing/research findings with evidence, the back-and-forth, the decisions, and the open questions. Trigger with /capture (optionally "/capture <title>" or "/capture to <path>").
---

# capture - turn a whole session into a keepable HTML report

When the user invokes `/capture`, produce ONE standalone HTML file that distills everything worth keeping from the current conversation, styled like a considered research/decision brief. It is a synthesis, not a transcript dump.

## 1. Gather the material (from the current session)

Re-read the whole conversation and pull out:

- **The goal / question** the user was chasing, in their own framing.
- **The journey** - the ideas raised, the turns the thinking took, what was tried and rejected.
- **Findings** - results of any tracing, code reading, research, tool runs, or audits, with concrete evidence (file:line quotes, command output, source URLs, numbers). Keep the receipts; they are what make the report authoritative on reopening.
- **Decisions & rationale** - what was concluded and why; what remains undecided (say so plainly).
- **Options** on the table, with trade-offs.
- **Open questions / next actions** - the resume point.

Prefer synthesis over quantity: organize by idea, not by message order. Where the session already produced a structured artifact (a scorecard, a table, an audit), carry it in.

## 2. Structure the report

Adapt to the session, but a strong default is:

1. Masthead - title, one-line subject, metadata row (date, status, scope), and a short "how to reopen this" note.
2. Executive summary / TL;DR - the whole thing at a glance, with a status legend if verdicts are involved.
3. The goal & context.
4. Body sections - one per major idea or finding. Use tables, callouts, and badges to encode state (built/partial/gap, yes/no, etc.).
5. Decisions & the fork(s) taken or pending.
6. Open questions & next actions.
7. Appendices - evidence receipts (file:line, quotes), a command/reference card, source links.

Number sections only when the order carries real meaning. Write in clear normal prose (never caveman/shorthand in the file - this is a persisted document).

## 3. Build the HTML

Start from the vetted skeleton in this skill folder: **`template.html`** (same folder as this SKILL.md). It already carries the house design system and is pure ASCII. Copy it, then fill the placeholder regions with the synthesized content. Keep its structure:

- **Design system** (do not drift): cool graphite neutrals with a slight blue bias; a cobalt brand accent used ONLY for structure (nav, links, section numbers, rules) kept separate from the semantic status colors (green = built/good, amber = partial/warn, red = gap/critical, violet = specced); system sans for prose paired with a monospace face for labels, badges, metadata, and file:line receipts.
- **Layout**: centered reading measure; a Table of Contents that is a sticky left rail on wide screens and a collapsible `<details>` on mobile; sections with a mono eyebrow; status badges; callout blocks with a colored left rule; wide content (tables, code, evidence) inside `overflow-x:auto` wrappers so the page body never scrolls sideways.
- **Theme**: works in light and dark via `prefers-color-scheme` plus a small toggle button; every color comes from a CSS token; `body` sets an explicit token background.
- Respect `prefers-reduced-motion`; give focus states; close every tag.

## 4. PHONE-SAFE - mandatory

The user reads these on a phone. The file body MUST be pure ASCII:

- HTML entities in text (`&mdash; &rarr; &sect; &bull; &hellip; &frac12; &nbsp;` etc.), CSS unicode escapes in `content:` (`\0025BE`), straight quotes only.
- `<meta charset="utf-8">` first in `<head>`, plus the viewport meta.
- **Final gate:** run `grep -oP '[^\x00-\x7F]' <file> | wc -l` - it must print `0`. If not, convert offenders before handing off.

The template is already ASCII-safe; keep any content you add ASCII too.

## 5. Save & report

- Default location: `.alpaca/captures/` (create the folder if missing). Filename: a descriptive kebab-case slug of the topic, e.g. `capture-<topic>.html`. If the user passed `/capture <title>` use that for the slug; if they passed `/capture to <path>` save there.
- Use the session's current date (from context) in the masthead and, if helpful, the filename - do not invent a date.
- After saving, tell the user the full path in one line, confirm the ASCII gate passed (0 non-ASCII), and give one concrete next action (e.g. "open it on your phone").

Do not publish as an Artifact unless the user asks - this is a local file by default.
