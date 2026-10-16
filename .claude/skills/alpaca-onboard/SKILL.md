---
name: alpaca-onboard
description: >-
  The verbs behind the first chat, and the one-time hand setup. Use it to look up what `alpaca onboard
  look`, `answer`, `next`, `propose`, `confirm`, `apply`, `show` and `onboard skip` do, or to onboard a project by
  hand with `alpaca onboard --name ...`. Triggers on "/alpaca-onboard", "onboard this repo", or a
  hand-off from alpaca-first-chat. The conversation itself is the alpaca-first-chat skill.
---

# alpaca-onboard

`alpaca onboard` turns the first chat into the first record rows. The talking is `alpaca-first-chat`; this
skill is the verb reference and the setup by hand.

## After `alpaca new`

`alpaca new` onboards the new studio provisionally (`--provisional`): `project.yaml` exists, op-0 is on the
record, and the first chat is marked due. The SessionStart hook and the pad say so until the owner has
confirmed a setup card. `onboard apply` then revises that setup; it never writes a second op-0.

## The verbs, in the order of a first chat

| Verb | What it does | What it writes |
|---|---|---|
| `bin/alpaca onboard look <git-url\|path> --scratch DIR --quote "<message>"` | reads the repo in a copy outside the studio, tries only checks that run no repo code, prints what it found, the guessed situation and round 1 | op-0 events `onboard-look`, `onboard-answer` (opening), `onboard-round` |
| `bin/alpaca onboard answer <slot> [--value V \| --option K] [--quote Q] [--source S] ...` | records one answer; an owner answer needs the owner's whole message and a value that is part of it | `onboard-answer` |
| `bin/alpaca onboard next` | prints the next round of required questions still open (four rounds at most), or `ready` | `onboard-round` |
| `bin/alpaca onboard propose [--reask]` | renders the setup card, stores its bytes, records that the owner is asked | `onboard-card`, `owner-ask`, `.alpaca/onboard/cards/` |
| `bin/alpaca onboard confirm <card> --quote "<reply>"` | proves the owner's typed yes (or no) to those bytes | `onboard-confirmed` or `onboard-declined` |
| `bin/alpaca onboard apply <card>` | configures from the confirmed card: project settings, attach, plain-writing style, goal note, level; resumes after a failed step | `project.yaml`, `app/`, `input/notes/`, `intents/queue.md`, `onboard-applied` |
| `bin/alpaca onboard show` | prints the confirmed card from its stored copy and says `changed` if the copy no longer matches | nothing |
| `bin/alpaca onboard skip --quote "skip setup"` | ends the first chat on the owner's word: the owner types `skip setup` (or another skip phrase) as a whole message after your last step, then you run this | `onboard-skipped` |

Slots: `opening`, `situation`, `goal`, `limits`, `others-commit`, `merges`, `ci` (where the checks run: a only on my machine, b on a CI service, c do not know), `files-in-repo` (a: no Alpaca file and no
trace in the repo, which makes the card propose a neutral commit identity, footer off and branch prefix `fix/`; b: files may
live in it, and the card says the in-place mode is not built in this version and attaches), `base`,
`branch-prefix`, `pr-says-agent` (answer `b` turns the pull request footer off), `commit-identity` (`Name <email>`), `audit-reader`, `migrate-target`, `keep-working`, `stack`, `preset`,
`level-override`, `done-guess`, `finding` (up to 3), `unknown` (up to 3), `gate.extra`, `gate.drop`.

Before the owner confirms, a first chat changes no file outside `.alpaca/` (the verbs write record events
and card files only). `apply` is the only verb that configures, and it refuses a card the owner did not
confirm.

## By hand, once

For a project that is not attached and not started with `alpaca new`, or when the owner skips the first
chat (while a first chat is open in an agent session, this form is refused with ONBOARD-FIRST-CHAT-OPEN; the
owner's skip ends the first chat, and then it works):

1. Gather who is working (name:role), the one line on what is being worked on, the open tasks, and whether to
   turn on the plain-writing preset.
2. Run `bin/alpaca onboard --name <project> --who <name:role,...> --what "<one line>" [--task "..."]
   [--preset plain-writing]` once. A second run is refused by the record.
3. Run `bin/alpaca doctor` and read `RESUME.md` back.

## What it does not do

- It does not hand-edit `RESUME.md` or `analytics/`; those are projections rendered by `alpaca`.
- It does not open an op. Opening an op is `alpaca-op`, from a stated intent.

## After

Hand off to `alpaca-op` when the owner is ready to open the first op. If the goal is still loose, run
`/alpaca-from-notes`: the goal the owner gave in the first chat is already in the inbox as a note.
