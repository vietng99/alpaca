# The first chat

What happens between `alpaca new` and the first piece of work, what it records, and how to check it.

## The flow

`alpaca new` sets the studio up provisionally (`alpaca onboard --provisional`): `project.yaml` exists, op-0 is on
the record, and the record mark `first_chat` is `due`. The SessionStart hook and the pad then say so until the
owner has confirmed a setup card. A project that was set up before this existed has no mark and is left alone.

| Step | Owner | Agent | Verb |
|---|---|---|---|
| door | opens the agent in the studio | asks for a git URL, a folder path or "start empty" if none was given | none |
| look | names a repo | reads it in a read-only copy outside the studio, tries only checks that run no repo code | `onboard look` |
| ask | answers up to four questions in one typed message | records each answer with the owner's whole message as the quote | `onboard answer`, `onboard next` |
| propose | reads one card | pastes the card unchanged | `onboard propose` |
| confirm | types yes (or asks, or changes a line) | runs confirm with the owner's reply | `onboard confirm` |
| apply | | configures from the confirmed card, then runs doctor | `onboard apply`, `doctor` |

## The states

Meta key `first_chat`: absent (every project that exists today), `due` (after `alpaca new`), `confirmed` (the owner
said yes to a card), `done` (applied), `skipped` (the owner skipped it). `adopt.detect` is unchanged; the key is
read through `adopt.first_chat_state`.

## What the owner says counts only when typed

`onboard confirm` reads the session transcript. Two forms count: a lone assent as the first owner message after
the ask (`yes`, `ok`, `go ahead`, `looks good`, `do it`, and a few more), or `card-<id> yes`. A click on an
option, a line written by a subagent, a headless (`sdk`) entry, a yes with a condition ("yes, but use L3"), a yes
typed before the ask or after another message, and a card the owner was not shown are all refused. A change to a
card is shown as the parts that changed, if the first card was shown whole. With no agent session in the
environment (a terminal, or Codex) the person types the card id instead.

## The card

Ten parts, pure ASCII (the owner's own words keep their accents; every check reads the card with the owner's words removed, so they never fail it), lines of at most 96 characters, at most 110 lines
that the agent wrote (the owner's quoted goal and limits are never shortened and do not count toward the 110): what I
understood, what I found (each line with a numbered source), where Alpaca sits, the checks, the first steps with their time, how a change reaches the repo,
how much the agent does alone (the level), what stays with the owner, undo, and the settings that will be written.
Each Alpaca word (studio, gate, op, task, level, baseline) is explained once, where it first appears. The card is
made by `onboard_card.render` from the recorded look and answers, so the same inputs give the same bytes and the
same id `card-<sha8>`. Cards and their inputs are stored under `.alpaca/onboard/cards/`. The owner's yes binds both
files: every ask about the card records `inputs_sha256` (the hash of the inputs file), the yes copies it into
`onboard-confirmed`, and `apply` and `attach init --card` refuse (ONBOARD-COPY-CHANGED, ATTACH-CARD-CHANGED) when the
inputs no longer hash to it or their settings no longer follow from their own look and answers. The time an ask was made
is also kept with microseconds (`at`), so a message typed in the same second before the ask is not read as the reply.

## The footprint, the mode and the trace lines

The first chat asks whether Alpaca files may live in the repo (slot `files-in-repo`; a recommended no) unless the repo already holds Alpaca files (`ALPACA-MANIFEST`, `.alpaca/`, `.claude/skills/alpaca-*`: the
look records them as `around.alpaca_files` and the card cites them as source 6). Which situations ask what:

| Situation | Round 1 asks, in order (at most four) |
|---|---|
| start empty | `goal`, `stack`, `limits` |
| take over | `others-commit`, `files-in-repo`, `goal`, `limits`; then, in a later round, `merges` (when others push, `others-commit` `b` or `c`) and `ci` |
| team repo | `goal`, `merges`, `files-in-repo`, `limits`, `branch-prefix`; then, in a later round, `ci` |
| audit only | `goal`, `audit-reader`, `limits`; then, in a later round, `ci` |
| migrate or rewrite | `migrate-target`, `goal`, `keep-working`, `limits`; then, in a later round, `others-commit`, `files-in-repo` and `ci` for a move into the same repo |

Every question has two to four options, one of them recommended, and a line on why the answer matters. An option that
needs the owner's own words (`limits` b "Yes, I will say them", `keep-working` b "Yes, I will list them", `branch-prefix`
b "Yes, this pattern: ...") is recorded with `--option` and `--value`, where the value is a part of the owner's reply and
the whole reply is the quote. The `goal` options fit the situation (for a take-over: get it into a clean, working state,
add a new feature, fix a problem I already know about) and the last one, "Something else, in my own words", is the
recommended one, because the goal is quoted in the owner's words. A starter option picked with no words stands as the
goal and the card says it came from the list; the last option with no words is not an answer yet, so `goal` stays
required. `branch-prefix` recommends the pattern the side branches of the repo share (`feature/`), else "Anything goes".

Where the repo's checks run (`ci`: a "only on my machine, there is no CI", b "on a CI service", c "I do not know") is a
core slot of every situation that attaches a repo: a take-over, a team repo, an audit, and a move into the same repo. It
is not open when the look found CI files (`.github/workflows`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/config.yml`):
the card states them (source 6) instead. The card uses the answer in part 1: a or b gives a "Where checks run" line, and
only c (or no answer) leaves "whether a CI runs outside the repo" under "Still unknown". A take-over also asks who merges
(`merges`) when `others-commit` is `b` or `c`; when it is `a` the card says the owner is the only one who commits and
merges.

Round 1 is chosen from the opening alone, before the owner has said where the new code lives, so a move always starts
with the same four questions, and a required slot outside a situation's round 1 list waits for a later round (round 2, or round 3 when the target of a move is only answered in round 2). In every later round a required slot that is still missing comes before every optional one, so the limit of four questions can never push it out.

Rounds: round 1 is the situation's list, the only round that asks optional questions. Every later round asks only the required
slots the answers so far still need. Some required slots only become known late (the same-repo slots of a move once its target is
answered in round 2, or every slot after a round 1 the owner left unanswered). So `onboard next` keeps printing a round that holds
only the required slots still missing, as options questions with a recommended answer and at most four of them, until they are
answered or `MAX_REQUIRED_ROUNDS` (4) rounds were printed. Only then does it say "not ready ... ask the owner for them in plain
words", so a chat cannot loop. Optional slots are never asked after round 1. (`MAX_ROUNDS` (2) in `onboard_card.py` is no longer read by the round rule.) `onboard_card.plan_next` is the one decision
`onboard next` and the tests share; the "later round" test uses the round count, not whether anything was answered. A move into the
same repo (`migrate-target` `a`) is attached and hands its changes to that
repo, so who else commits (`others-commit`) and whether Alpaca files may live in it (`files-in-repo`) become required
slots: `onboard next` is not ready until they are answered, and it asks them in the next round: round 2, or round 3 when the target is only answered in round 2. A slot the look cannot open is not
required, so it never blocks `ready`: `others-commit` needs recent authors in the history, `files-in-repo` is
answered by a repo that already holds Alpaca files, and `ci` is answered by CI files in the repo. A move into a new repo
(`migrate-target` `b`) only reads the old repo and is never asked any of the three. A later round asks only a required slot that is still missing (`onboard_card.required_slots`
is the one reader of what is required). The answers are read the same way in every situation: an owner of a move who
answers no to the footprint question gets the same trace-free set as one who takes over a repo, and an `others-commit`
answer of `b` or `c` marks that others push. The part-1 line "Others who push" shows for a take-over and for a move into
the same repo.

The card derives the mode from the answers and states why (part 3):

| Answers | Mode | Reason the card gives |
|---|---|---|
| no files in the repo (answer `a`) | attach | you want no trace of Alpaca in your repo (and, if others push, that too) |
| others push (a team repo, or `others-commit` `b` or `c`) | attach | other people work in your repo |
| files allowed (answer `b`) on a solo take-over | attach | the in-place mode (Alpaca files inside the repo) is not built in this version, so attach is proposed; the card says so and offers no native studio for an existing repo |
| no repo (start empty), or a move into a new repo | native | there is no repo of yours to keep apart |

The card always shows the three things a repo shows of Alpaca, as three lines of part 6 with their values, defaults
included, and part 10 repeats them: the commit identity (default `Alpaca agent <alpaca-agent@localhost>`), the pull request
footer (default on) and the branch prefix (default the repo's own rule from `CONTRIBUTING.md`, else `alpaca/`). When the
owner answered no to the footprint question the card proposes a trace-free set where the owner gave nothing: the identity
`Maintenance bot <maintenance@localhost>`, the footer off and the prefix `fix/`, each marked "proposed". An answer of the
owner (`commit-identity`, `pr-says-agent`, `branch-prefix`) always wins, and so does the repo's own branch rule over the
neutral prefix. `onboard apply` writes exactly the values the card shows (`attach.commit_name`, `attach.commit_email`,
`attach.pr_footer`, `attach.branch_prefix`); a default is written as no key, because it is what attach holds anyway. A
footprint answer is an answer, so the card shows it in part 1 ("Alpaca in your repo: ..."); with none the card says "no
files (my default; tell me if they may live there)" and proposes the defaults, not the neutral set.

## Before the yes

A first chat writes only these op-0 events: `onboard-look`, `onboard-round`, `onboard-answer`, `onboard-card`,
`owner-ask`, `onboard-declined`, the card files, and a record of the setup files taken when the first chat begins
(under `.alpaca/onboard/setup/`, see "What this cannot stop" below). It changes no other file of the studio, not
`project.yaml`, `intents/`, `input/`, the studio's git config or exclude file, and creates no `app/` folder.
Three more things are refused to any caller that is not the owner's own terminal. The owner's own terminal is a
process with no session id and no agent marker in its environment and a terminal on its standard input
(`ownerword.owner_terminal`); every other call, including a child process of an agent that removed the markers and has
no terminal, is an agent call. Two checks decide, by command and by the state of the first chat: `cli.main` asks
`first_chat.verb_refusal` before any command runs, and the three setup writers also ask
`onboard_flow.setup_write_refusal`:

| Agent call | refused when |
|---|---|
| `alpaca onboard --name ...` (flat; writes `project.yaml`) | the first chat is open: due, confirmed, or a look was recorded in a fresh studio (ONBOARD-FIRST-CHAT-OPEN; it names `onboard propose` and `onboard skip`). Otherwise it goes on, and its record event carries `agent_call: true`. |
| `alpaca attach init` without `--card` | unless the first chat is done or was skipped on the owner's word. A fresh studio, a due or confirmed first chat, and a studio whose newest flat `onboard` event carries `agent_call` are all refused (ATTACH-NEEDS-CARD). |
| `alpaca new --attach` | always, before anything is made, `--yes` from a script with no terminal included; run it in your own terminal. |
| any other command (for example `style use`, `note add`, `op new`, `task add`, `upgrade`, a profile's own command) | the first chat is open in the studio the command runs in, or in a studio the command would write from another folder (`upgrade --target`, `new --dir`, an output folder: `first_chat.WRITTEN_PATH_ARGS`). While it is open an agent call runs only `onboard ...`, `attach init`, `attach status`, `new` (for a folder outside this studio), `status`, `doctor`, `verify`, `recall`, `init` and `serve` (`first_chat.FIRST_CHAT_VERBS`). Every other command is refused before it runs and changes nothing (ONBOARD-FIRST-CHAT-OPEN). The refusal says where the answer goes instead: the plain-writing choice and the goal are lines of the setup card. |

The owner's own terminal is never refused by these checks. `test_first_chat_verbs.py` pins the command list,
`test_setup_write_gate.py` pins every cell of verb, state and caller; `test_onboard_nowrite.py` and `test_onboard_attacks.py` pin the rest. The first chat is open when it is due or
confirmed, or when a look was recorded in a fresh studio.

`onboard skip` ends the first chat on the owner's word, and only on a whole message that is the owner's newest: the
owner types `skip setup` (or `skip the setup`, `skip the first chat`, `i will set it up myself`) on a line of its own,
after the newest step of the first chat (the last round of questions or the last ask about a card). A sentence that
holds those words, a message typed before that step, or a skip phrase the owner wrote more after, does not skip. A
one-word reply (just the word skip) does not end the first chat either: an owner writes that to pass over one question.
The yes to the card follows the same rule: it counts only while it is the owner's newest message, so a yes followed by
"wait, change the goal" does not confirm (ONBOARD-NOT-NEWEST). Which message is newest is decided by its place in the
transcript, never by the clock. With no agent session the owner types the id at a terminal, as for `confirm`.

The look tries a gate before the yes only when it runs nothing the repository wrote. That is an allow list
(`onboard_look.classify_gate`): the built-in Python syntax check, `git diff --check`, and a compiler named by a bare
program name (found on the PATH outside the repository and the studio) with only `-fsyntax-only`, `-std=`, `-I`, `-D`,
`-U`, `-W` words without a comma, `-w`, `-pedantic`, `-x c` or `-x c++`, and source files that lie in the clone.
Every try runs with a memory cap, a time cap and no permission to write a file, and with an environment built from a
fixed list (`onboard_look.trial_env`): `PATH` (absolute folders outside the repository and the studio), `LC_ALL`, `LANG`,
`HOME` and `TMPDIR` (an empty folder made for that try and removed after it), and three git switches that keep git
from reading any configuration. A variable the caller has set, such as `COMPILER_PATH`, never reaches the program.

## What this cannot stop

The checks above stop an agent that runs Alpaca's commands as they are: with its agent markers set and no terminal it
made itself. They cannot stop an agent that hides itself (it removes its environment markers, makes a terminal with
`script`, or types the owner's reply at a prompt), or that writes the record, the meta table, a transcript or a setup
file directly. Such an agent runs as the same Unix user as the owner, so code on the same machine cannot tell it from
the owner; `alpaca/ownerword.py` (HONEST LIMITS) says the same for every owner word. A second Unix user for the owner
would close it only while the agent has no sudo or root.

What Alpaca does instead is show it. `alpaca doctor` and `alpaca onboard show` warn when:

- the first chat is marked skipped, confirmed or done, but the record has no event behind it;
- the owner's yes or skip was typed at a terminal while the first chat ran in a Claude Code chat;
- the owner's yes or skip points at a transcript line that does not hold that message;
- setup files changed after the first chat began. When the first chat begins, Alpaca records the files outside
  `.alpaca/` under `.alpaca/onboard/setup/`; `confirm` and `apply` also print the files that changed since. They do not
  refuse, because the owner may change a file in their own terminal.

## After the yes

`onboard apply <card>` runs these steps in order, each with its own record event, so a failed step is resumed by
running apply again: `config` (project settings, or the first full onboarding in a fresh copy), `attach`
(`alpaca attach init <source> --card <card>`: the upstream, base, branch prefix and gates come from the card, and so do the pull request footer (off for a `pr-says-agent` answer `b` or a footprint answer `a`: `attach.pr_footer: false`) and the commit identity when it is not the default (`attach.commit_name` and `attach.commit_email`), exactly as the trace lines of part 6 show them),
`preset` (plain-writing style), `note` (the goal, word for word, as a note; one line in `intents/queue.md`),
`level` (the session level, only if the session has none), `scratch` (the read-only copy is removed), `done`.

## Record events on op-0

`onboard-look`, `onboard-answer`, `onboard-round`, `onboard-card`, `owner-ask`, `onboard-confirmed` or
`onboard-declined` (actor `owner`, with the proved owner word), `onboard` (revision 2, what changed),
`attach-init` (with the card id), `onboard-applied` (one per step), `onboard-skipped`. `alpaca onboard show` prints
the confirmed card from its stored copy and says `changed` if the copy no longer matches its recorded hash.

## Rehearsal

`alpaca/tests/rehearsal/` holds three newcomer personas, a driver, a scorer and a Workflow script (AC-20). See its
README. A rehearsal's confirm proves the code path, not the owner's consent; its events carry `rehearsal: true`.
