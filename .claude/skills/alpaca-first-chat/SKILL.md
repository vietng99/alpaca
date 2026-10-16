---
name: alpaca-first-chat
description: >-
  The first chat of an Alpaca project. Use it when the SessionStart hook prints FIRST CHAT, when the
  owner has just run alpaca new and names a repo (a git URL or a folder path) or says "start empty", and
  on "/alpaca-first-chat", "start alpaca" or "set up the harness". It reads the repo before it asks
  anything, asks a few questions, reads the owner's goal back, shows ONE setup card, and sets up only
  after the owner types yes. (Not the `alpaca intake` verb, which turns a spec and a runbook into
  checklist rows.)
---

# alpaca-first-chat

The owner has just started. They know their project and they do not know Alpaca. Your job in this chat
is to find out what they want, say back what you understood, and propose how Alpaca will work with them,
in words they can check. You write nothing into the studio until they type yes.

Write plainly: short sentences, no Alpaca word that the card does not explain, no summary of their goal
(quote it), no claim without a pointer.

## Explain every Alpaca word the first time you use it, in every message

The owner reads one message at a time. In each message you write, the first time you use one of the words below,
put its meaning in parentheses straight after it, even when an earlier message already did. Use these meanings
word for word. The card has its own glosses; do not add to the card, and do not repeat a gloss the message already
holds.

- studio: the folder you made with alpaca new
- gate: a command that must pass before a change can leave the studio
- op: one goal with its own tasks and reports
- task: one small piece of work with its own branch
- level: how much I may do before I ask you, from L1 to L6
- baseline: the result of every check on your code as it is today
- study: a first read of your code, written down as a map
- upstream: the repo your code comes from
- base: the branch your changes start from and are checked against

Example of a summary line: "I ran the baseline (the result of every check on your code as it is today): both gates
pass." A word the owner typed first needs no gloss. A word this list does not hold needs a plain one-line meaning too.

## When it applies

- The SessionStart hook printed `FIRST CHAT`, or `bin/alpaca status` shows `first chat:` as the next action.
- The owner typed `/alpaca-first-chat`, or asks you to set the project up.

If the pad says `setup confirmed, not applied`, skip to step 8. If the first chat is done or skipped, this
skill does not apply.

## The one rule

Nothing is configured, cloned into the studio or attached before the owner's typed yes to the card.
The verbs enforce it: `look`, `answer`, `next` and `propose` write only record events and card files, and
`confirm` accepts only the owner's own typed words from the session transcript.

## Steps

1. **The door.** If the owner's first message names no repo and does not say "start empty", ask only this:
   "Is there code already? Paste a git URL or a folder path and I will read it before I ask you anything
   else. Or say "start empty" for a new project." Ask nothing else yet.

2. **Look first.** Run, with the owner's whole message in `--quote` and your session scratchpad folder
   (outside the studio) in `--scratch`:

   ```
   bin/alpaca onboard look <git-url|path> --scratch <your session scratchpad>/onboard-look --quote "<their whole message>"
   ```

   For "start empty" run `bin/alpaca onboard answer opening --value "<their message>" --quote "<their message>"`
   instead; it prints the questions for a new project.

   If the look fails with `LOOK-AUTH`, tell the owner to run `gh auth login` in their own terminal, or to give
   a folder path instead. Never ask for a token and never handle one.

3. **Record what you noticed.** Up to three findings, each a plain sentence with a pointer to a repo file
   that the verb checks:

   ```
   bin/alpaca onboard answer finding --value "<one plain sentence>" --pointer repo:<path>:<first>-<last> --source agent
   ```

   A finding states something you read in the code (a step written twice, a README that names a file that
   is not there). It is never a guess about what the owner wants.

4. **Turn 1: say what you found, then ask.** Use only the lines `look` printed under "found" and the
   findings you recorded, with the same source numbers. Then name the situation the look guessed (take over,
   team repo, audit only, migrate or rewrite, start empty) and why, then the printed questions, word for
   word, with their options and their "why" line (a round never has more than four questions, and
   every round after round 1 holds only required questions that are still unanswered, and four rounds is the most). A take-over and a team repo get the footprint question,
   "May Alpaca files live in your repo?", with the recommended answer no, unless the repo already holds Alpaca files
   (then it has answered). A take-over also gets "Does anyone else still push to this repo?" (`others-commit`). A move
   to new code gets the target, the goal, what must keep working and the limits in round 1; when the owner says the new
   code goes in the same repo, `onboard next` prints the next round (round 3 when the target was only answered in round 2) with the footprint question and the others question, which
   are required for that move. A move into a new repo, an audit and an empty start get neither of those two. Every situation that
   attaches a repo (a take-over, a team repo, an audit, a move into the same repo) also gets "Where do this repo's checks
   run?" (`ci`) in the next round, unless the look found CI files (the card states them then). A take-over whose owner says others
   push (`others-commit` b or c) also gets "Who reviews and merges a change to this repo?" (`merges`) in the next round. When `onboard next` prints a round, ask exactly its questions with their options, even when it is round 3 or 4; only its "not ready ... plain words" line (after four rounds) lets you ask in your own words.
   Every question has two to four options, one recommended, and a "why" line; print them as `onboard` prints them.
   The goal question has options too, and its recommended last option is "Something else, in my own words": the goal
   is quoted, never summarized. Tell the owner that one
   message can answer everything, for example "1a" and then the goal. Never ask what the look already
   answered. Ask in plain chat, never AskUserQuestion: a click is not an owner word, and a typed message
   can answer all the questions at once.

5. **Record every answer, quoting the whole reply.** For each question:

   ```
   bin/alpaca onboard answer <slot> --option <a|b|c> --quote "<their whole reply>"
   bin/alpaca onboard answer goal --value "<part of their reply, their words>" --quote "<their whole reply>"
   bin/alpaca onboard answer limits --value "<part of their reply>" --quote "<their whole reply>"
   ```

   Limits, what must keep working and the branch pattern are questions with options as well. When the owner picks the
   option that needs no words (limits a "None", keep-working a "Nothing in particular", branch-prefix a "Anything goes"),
   record just the option: `answer limits --option a --quote "<their whole reply>"`. When they pick the option that needs
   their words (b), record the option and the words together:
   `answer limits --option b --value "<part of their reply>" --quote "<their whole reply>"`. The value is always a part of
   what the owner typed, and the quote is always their whole reply. For the goal, record the owner's words as the value
   (`--option` is optional); if they pick the last option and type no goal, ask once for the goal in their own words.
   Where the checks run: `answer ci --option a|b|c --quote "<their whole reply>"` (a only on my machine, b a CI service,
   c do not know).

   When the owner answers the footprint question, record it as the others: `answer files-in-repo --option a|b
   --quote "<their whole reply>"` (a: no Alpaca file and no trace of Alpaca in commits, branches or pull requests; b: files
   may live in the repo). Option a makes the card propose a trace-free set (a neutral commit identity, the pull request
   footer off, a neutral branch prefix); an owner who names their own identity, footer or prefix is recorded with
   `answer commit-identity --value "Name <email>"`, `answer pr-says-agent --option a|b` (b: footer off) and
   `answer branch-prefix --value "<prefix>/"`, and their answer wins over the proposal. Option b on a solo take-over
   still gets attach: the in-place mode is not built in this version, and the card says so. Never tell the owner Alpaca
   will work in place, and never record an answer the owner did not give.

   When the owner names who merges ("Hana merges"), record the words with the option:
   `answer merges --option a --value "<the words that name the person>" --quote "<their whole reply>"`. The value
   must be a part of their message. The card then says who merges, in their words, and your done line names that person
   too.

   The goal must be a part of their message, never your summary; the verb refuses a paraphrase. If a reply
   does not hold the option token (for example "1a"), record your reading with `--source agent --reason "my
   reading of: <what they wrote>"`; the card marks it. Then write what done looks like in one or two plain
   lines with `bin/alpaca onboard answer done-guess --value "..." --source agent` (say "merged on GitHub" or
   "pull request" only when the card's repo is on GitHub; the card's parts 6, 8 and 9 follow the host). Add a check of your own
   with `answer gate.extra --name <name> --argv-json '[...]' --reason "<the repo file it comes from>"
   --proves "<what passing proves>" --source agent` only when the repo's files point to it.

6. **Run `bin/alpaca onboard next`.** If it prints a round, ask those questions (once more) and record them.
   When it prints `ready`, go on. If `propose` later refuses with `ONBOARD-NEEDS`, do what it names: record
   the missing answer, or, when the look found no check to attach with, add one from the repo's own files with
   `answer gate.extra`, or ask the owner for a command that proves the code works.

7. **Turn 2: the card.** Run `bin/alpaca onboard propose`. It prints the setup card. Paste that output
   (a line starting `propose:` on the error output is a note for you, not for the owner)
   unchanged as one message, and add nothing before or after it. Do not shorten it, reword it, or split it.
   The card has ten parts: what I understood (the goal in the owner's words, the limits, what done looks
   like, the situation), what I found, where Alpaca sits (attach or native, and why, from the answers), the checks, the first
   steps with their time, how a change reaches the repo (with the three trace lines: commit identity, pull request
   footer and branch prefix, defaults included), how much I do alone (the level), what stays with
   the owner, how to undo it, and the settings that will be written. `apply` writes exactly the trace lines the card
   shows; if the owner wants one changed, record the answer and run `propose` again. The owner has two ways to answer:
   reply "yes", or say which line to change. The goal and the limits are printed in the owner's own words, whole,
   however long they are, and every quote of theirs on the card ("You said ...") is theirs too; never shorten,
   reword or edit the owner's words, and never change an answer to make the card pass. If `propose` says
   `ONBOARD-CARD-LINT`, it names text you wrote (a finding, a gate proof, the done guess; the check skips the
   owner's words, so a refusal never names them): fix that line and run `propose` again.

   When the owner replies:

   - Run `bin/alpaca onboard confirm <card id> --quote "<their whole reply>"`. If it accepts, go to step 8. A yes may
     come with polite words: after `ok`, `okay`, `got it`, `sure`, `please set it up`, `set it up`, `go ahead`,
     `thanks`, `thank you` and `sounds good` are taken out, the words left must be exactly `yes`. "ok got it. yes"
     confirms. Any other word keeps the reply a change, so ask for nothing extra and do not tell the owner to type
     a lone yes.
   - If it says `ONBOARD-NOT-A-YES` and the owner asked a question, answer in at most five lines, then run
     `bin/alpaca onboard propose --reask` and end with: Reply yes to set up the card above. Do not paste the
     card again.
   - If the owner wants a line changed, record it (`answer <slot> ... --quote "<their reply>"`, for example
     `answer level-override --value L3 --quote "..."`) and run `bin/alpaca onboard propose` again. It prints
     the new card, and on its error output a line starting `propose:` that names the parts that changed;
     that line is for you. Show the owner only those parts, each whole with its number and heading, then the
     new card id and the reply line. "yes, but use L3" is a change, not a yes.
   - If it says `ONBOARD-NOT-NEWEST`, the owner wrote again after their reply. Read that message and act on
     it. If it changes nothing, ask the owner to say yes again.
   - If it says `ONBOARD-CARD-NOT-SHOWN`, paste the card unchanged as one message, run
     `bin/alpaca onboard propose --reask`, and ask again.
   - If the owner says no, it records the decline. Ask what they want different and run `propose` again.

   In a Codex session `confirm` prints the card and asks the person to type its id in a terminal of their own.
   Tell them so in one line.

8. **Set up.** Run `bin/alpaca onboard apply <card id>`, then `bin/alpaca doctor`. Turn 3: say what was set
   up, each line with a pointer (the card id with `bin/alpaca onboard show`, the settings in `project.yaml`,
   the doctor result, the goal note under `input/notes/`). Then do the first win the card promised, and post
   turn 4 with what Alpaca now knows about the code, each line with a pointer, and one next step in the owner's words.

   In native mode there is no code to read yet: turn 4 gives the first-step plan with its pointers.

   In attach mode (`bin/alpaca attach status` shows what is there) the first win has these steps, in this order:

   1. Run `bin/alpaca attach baseline` if `bin/alpaca attach status` shows no baseline for the base commit yet.
      Read its result. A gate that fails on the base goes into turn 4 as it is.
   2. Run `bin/alpaca attach study`. It writes the map skeleton to `design/attach/codebase-map.md`, with one
      `## Area:` heading for each top-level folder, and the snapshot of the base under `.alpaca/attach/study/<sha12>/`.
   3. Run the study scouts: one read-only subagent for each area of the skeleton (a few areas for each scout when
      there are many), each with a model named by tier as the shield rule says. A scout reads only the snapshot and
      the stored outputs for its area, and writes bullets under its `## Area:` heading in the map: one claim for each
      bullet, ending with one pointer, `[ptr: app@<path>:<line>]` or `[ptr: out:<outputs>/<sha12>/<file>:<line>]`
      (the skeleton's comments show both). A claim states what the code shows: the entry point, how to build and test,
      where the tests live, the risky or busy files from the stored git log. A scout never guesses and never edits
      anything but the map.
   4. Run `bin/alpaca attach study check`. For each problem it names, send the claim back to its scout or drop the
      line, and run the check again until it passes. Then run `bin/alpaca attach study ingest`.
   5. Turn 4 shows the map: each area with its one or two most useful claims and their pointers, the number of claims
      the check counted, and the baseline result. A first summary that does not show the map is not finished.

   If the owner wants to skip all of this, ask them to type `skip setup` on a line of its own and nothing else
   in that message. After they do, run `bin/alpaca onboard skip --quote "skip setup"`. A sentence that holds
   those words, the word skip alone (that passes over one question), a message they typed before your last step, or
   a `skip setup` they wrote more after, does not skip the first chat, and you do not ask for the skip on
   their behalf. Once it is skipped, `bin/alpaca onboard --name ... --who ... --what ...`
   sets up by hand, and a plain `bin/alpaca attach init <url>` works for you again.

## What a turn sounds like

Turn 1 (after a look): "I read <repo> before asking you anything. I made a read-only copy in a temporary
folder outside the studio (the studio is the folder you made with alpaca new). Nothing is set up yet and your
repo is unchanged. What I found: ... You said you took it over, so I read this as a take-over. Two questions.
One message is enough, for example "1a" and then your goal."

## Never

- Write a fact the verbs did not print or a finding you did not record.
- Use an Alpaca word the card does not explain (studio, gate, op, task, level, baseline, study, upstream, base)
  when talking to the owner before the card; say what you mean in plain words. After the card, gloss each word at its
  first use in each message, as the list above says.
- Run `bin/alpaca attach init` on your own. It runs inside `onboard apply`. Any call that does not come from the
  owner's own terminal is refused when it has no `--card`, whether or not it carries the agent markers: a program
  you start with no terminal on its input counts as you. The same goes for `bin/alpaca new --attach`, and for
  `bin/alpaca onboard --name ...` while the first chat is open (the refusal names `onboard propose` and
  `onboard skip`). If you ran `bin/alpaca onboard --name ...` yourself, a plain attach is refused afterwards too;
  the owner attaches with `bin/alpaca attach init <url>` in a terminal of their own.
- Run other Alpaca commands while the first chat is open. Until the owner says yes to the card or types
  `skip setup`, you may run only `bin/alpaca onboard ...`, `status`, `doctor`, `verify`, `recall`, `init`,
  `serve`, `attach init`, `attach status`, and `new` for a folder outside this studio; every other command is
  refused with ONBOARD-FIRST-CHAT-OPEN and changes nothing. The same holds from any other folder: a command
  that would write this studio (for example `bin/alpaca upgrade --target <this studio>`, or `new --dir` a
  folder inside it) is refused too. The plain-writing answer and the owner's goal go on the card
  (`onboard answer preset`, `onboard answer goal`), not through `bin/alpaca style` or `bin/alpaca note add`.
- Look for another way when a command is refused with ONBOARD-FIRST-CHAT-OPEN. Tell the owner what the
  command would do, and ask them to answer the card or type `skip setup`.
- Make a command look as if the owner ran it. Do not wrap a command in `script`, do not remove environment
  values to hide that you are an agent, do not type a card id or a yes into a prompt for the owner, and do
  not write Alpaca's record or its files by hand. Alpaca's doctor shows these.
- Edit `project.yaml`, `intents/` or `CLAUDE.md` by hand to speed things up.
- Retry a refused `confirm` with other flags, or type a yes for the owner. A yes the owner did not type does
  not count, and the record shows it.
- Raise the level above what the card says. Only the owner raises it, later.
