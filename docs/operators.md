# Operating Alpaca with Codex and Claude Code

Both operators use the same project-local record, `.alpaca/alpaca.db`, and the same
lifecycle handlers. Each session has its own ID and posture. A clean shipment has
no session record, imported transcript, account settings, or previous machine's
history. The first session creates this copy's instance identity.

## Codex

Read `AGENTS.md` when entering the project. Start an explicit session from the
project root:

```bash
bin/alpaca session start --operator codex
```

The JSON result contains `session`, `context`, and `level`. Read the returned boot
context and retain the generated ID for this task. Include that exact ID on every
subsequent command that records work. You can supply your own unique ID instead:

```bash
bin/alpaca --session codex-example session start --operator codex
bin/alpaca --session codex-example status
```

Use a different ID for a different task or worker. Do not share an ID with a
Claude session. An existing ID can be resumed with `session start`; its original
start time and recorded posture are retained. Starting a session does not launch
a dashboard. Add `--dashboard` or run `bin/alpaca serve` when desired.

If the pad says the project is not onboarded, ask the onboarding questions (who is
working on it, what is being worked on, the open tasks, and whether to enable the
plain-writing preset), then run `bin/alpaca onboard ...` once and `bin/alpaca doctor`. When
`project.yaml` names a domain profile, read that profile's instructions for the
active step.

At a milestone, before returning a final reply, and before an expected context
handoff, record a continuation note:

```bash
bin/alpaca --session codex-example session checkpoint --note "Next: review the failing test log and rerun the suite"
```

This drains recorded events and the note into the local wiki and refreshes the
resume pad. The command returns a nonzero exit status if capture fails. A failure
can leave a failure event in the record; inspect it, fix the cause, and retry the
checkpoint. It is safe to drain unchanged events again.

Before the final reply, record the turn boundary and check stop rules. To include
the final reply in the style check, save its proposed text in a local file and
provide that file explicitly:

```bash
bin/alpaca --session codex-example session stop --reply-file path/to/proposed-reply.txt
```

Exit 2 with `decision: block` names a requirement to address. Without
`--reply-file`, the stop command checks the recorded work state and has no reply
text to lint. `stop` ends a turn; it leaves the session open. When deliberately
closing the recorded session, run:

```bash
bin/alpaca --session codex-example session end --reason complete
```

Codex and terminal operators can name their own transcript file: `bin/alpaca --session codex-example session start --transcript path/to/session.jsonl` and `bin/alpaca --session codex-example session end --transcript path/to/session.jsonl`. The path is stored in the record and copied into `.alpaca/transcripts/` on the next boundary. Only an explicit path is accepted; nothing scans the account's transcript directory.

Codex does not run `.claude/settings.json`. These commands are explicit operator
steps, not automatic Codex hooks. Abrupt termination or unannounced compaction
cannot be assumed to run a final capture. Checkpoints preserve the work already
recorded. Codex transcripts, token counts, and unreported tool activity are not
imported automatically; the dashboard marks usage as unavailable.

Optional explicit boundaries:

```bash
bin/alpaca --session codex-example session prompt
bin/alpaca --session codex-example session beat --tool exec_command --ref "reviewed the test report"
bin/alpaca --session codex-example session level L4 --goal "Run the authorized verification work"
```

`prompt` returns the current style reminder on its cadence. `beat` records only
the action you report; it does not measure every native tool call. Keep sensitive
command arguments out of `--ref`. A level change must reflect the owner's
authorization; posture alone does not grant separate unattended authority.

## Claude Code

Open the project in Claude Code and read `CLAUDE.md`. The eight project hooks in `.claude/settings.json` invoke `bin/alpaca-python`, which selects the installed project environment. SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStop, SessionEnd, and PreCompact call the same handlers used by the explicit commands. PreToolUse and PostToolUse write each tool call and its response to `.alpaca/pool/tools/<session>.jsonl` (an input or a response over 256 KiB is cut there, with the sha256 and byte count of the whole value kept; a large input is stored once, on the PreToolUse line); PreToolUse prints nothing, so it can never block a call. Stop, PreCompact, SessionEnd and SubagentStop copy the registered transcript and its subagent files into `.alpaca/transcripts/`, so the session outlives the thirty-day clearing of the account's own storage.

Claude supplies its session ID and the exact transcript path in hook payloads.
Alpaca records that explicitly supplied path for that session. It never scans
the account's transcript directory or a historical project's directories.
Project hooks retain bounded, fail-open behavior so capture failures do not
stall Claude; an explicit `session checkpoint` is the way to require a visible
capture result before handoff.

Use the Claude-provided session ID with `bin/alpaca --session ID` for recorded
commands and explicit posture changes. Project posture files live under
`.alpaca/config`; neither operator reads account-wide autodrive files. The
dashboard is optional for Claude as well.

A `bin/alpaca` verb run without `--session` takes its session from `ALPACA_SESSION_ID`, then from `CLAUDE_CODE_SESSION_ID`, which Claude Code exports in every shell call. A task move, a proof seal or a message sent from a Claude session is therefore recorded under that session and not under the placeholder `cli`. Outside any session the placeholder still applies.

## Shared task planning

Both operators assign a named feature/workstream, work area, prerequisites and resources when they add a task. Reuse an existing group where it fits. Keep the map current when work splits or its prerequisites change. Follow the commands in [Maintaining a useful checklist](operations-hub.md#maintaining-a-useful-checklist); this also covers the project-local required-planning policy.

## Shared boundaries

The CLI shim pins its own root. Python callers can select an explicit root;
otherwise resolution uses the nearest `ALPACA-MANIFEST` above the current
directory. `ALPACA_ROOT` is a fallback only when that directory has no marker.
Ambient `CLAUDE_PROJECT_DIR` and `CLAUDE_CONFIG_DIR` cannot redirect an ordinary
Alpaca command. Locally imported transcript files may be placed under
`.alpaca/transcripts`. The old `cwd_history` field never enables discovery.

The record remains the shared coordination surface for claims, tasks, proof
pointers, and decisions. Native agent creation belongs to each operator: use the
available Codex agent tools or Claude delegation tools. Recording a formation
assignment does not itself start an operating-system process.

Explicit command errors are visible: exit 0 is success, exit 2 is a blocked or
invalid session operation, exit 64 is command usage, and exit 67 is an internal
failure. Keep the JSON result and command exit status together when assessing a
lifecycle step.

## Run review and the main decision

When a domain profile wires `alpaca.runlog.reviewflow` (see `docs/observability-operations.md`,
"Review every run, then decide"), every run it captures is reviewed, PASS runs included, and
the main session decides. Claude Code and Codex follow the same loop, in
`.claude/skills/alpaca-log-review/SKILL.md`:

1. After a run finishes, and on every resume, list the runs waiting for review or a decision.
2. Assign the run to a reviewer session id distinct from your own. The assignment records a
   30 minute lease and returns a brief; it launches nothing.
3. Dispatch a fresh native subagent with that brief: the Agent or Task tool in Claude Code, the
   agent tool in Codex. It reads the log, submits a quote-checked review bound to the
   assignment and returns the review id. It does not run or fix the work and does not decide.
4. Read the review, starting with its At a glance block, and record GO or NO-GO with an
   evidence-based rationale. GO needs a recorded PASS and an intact assigned review that
   supports it; NO-GO holds the run for investigation.
5. The profile's runner advances only on a current GO. A PASS run that captures no log needs
   no review; a run with no log that did not pass never advances.

A review whose submit passed has quotes that match the log; it is not the run passing and not
permission to advance. Review state and decisions live in the record, so a disconnected chat
loses nothing; do not assign a second reviewer while a lease is live. No background model
process reviews on its own: the review waits for an active session.

## Alpaca's own skills

`.claude/skills/` holds Alpaca's own skills: `alpaca-first-chat` (the first chat before
onboarding), `alpaca-onboard`, `alpaca-op`, `alpaca-from-notes`, `alpaca-interview`,
`alpaca-runbook-forge` and `alpaca-log-review`. They are
Claude Code project skills and mechanism paths in `ALPACA-MANIFEST`, so a clone, a copy, a release
and `alpaca upgrade` all carry them and Claude Code offers each as a `/command` with nothing to
install. The rest of `.claude/skills/` (the spec-kit and OpenSpec skills `alpaca spec init` writes,
and any skill of your own) belongs to the project. Codex reads the same SKILL.md files directly.

## Optional bundled skills

The root lifecycle adapters do not require a plugin installation. To load the optional bundled skills in Claude Code, launch from the project root with `ALPACA_ROOT="$PWD" claude --plugin-dir ./plugin/alpaca`. Codex may read the relevant bundled SKILL.md files directly. No global plugin or agent configuration is installed by bootstrap.

## Project wiki evidence

Rune2 is the local project wiki. Use `bin/alpaca wiki status` to inspect its corpus,
provider settings, transcript coverage and missing event count. These counts are separate
from the operational decision pages shown by the dashboard.

Before using historical material for a new task or after a resume, run:

```
bin/alpaca wiki context "SQLite offline" --refresh
bin/alpaca wiki query "Why does the project use SQLite?"
```

`context` returns bounded cited evidence. `query` returns JSON with the answer verdict,
source document/block pointers, currency and completeness checks. An abstention is a
successful query with no supported answer, not a claim that the source does not exist.
`--refresh` replays recorded sources through a fixed cutoff before querying. For explicit
catch-up without a query, use `bin/alpaca wiki recover`. Run `wiki status` afterward; new
lifecycle or recovery events may arrive after the reported cutoff.

Both operators share the query door. Codex can request startup context explicitly:

```
bin/alpaca --session SESSION session start --operator codex --wiki-question "SQLite offline"
```

A resumed session with a claimed task also attempts bounded context from that task's title.
The boot message names the query commands and reports an unavailable wiki without blocking
operational startup. Check capture freshness before relying on that context.

Index a local authored document with a stable identity:

```
bin/alpaca wiki ingest docs/design.md --doc-id wiki/design.md
```

This copies its content into the local vault through the normal write door. For private
material use `--doc-id wiki/private/design.md`; ordinary wiki paths remain eligible for
public extraction after the existing privacy scan. Existing private sources cannot be
copied into a public wiki path with this command. The
stable document identity lets later ingests update the source while retaining history;
normal shrink and integrity gates still apply. Ingestion does not grant policy authority
or publish the content. Raw events and visible transcript exchanges remain evidence-only.
Hidden reasoning records are excluded. Visible tool inputs and results use the same secret
redaction as the transcript viewer; captured text still has no instruction authority.

Configuration precedence is defaults, then `project.yaml` `wiki_providers`, then
`.alpaca/wiki/rune.toml`. Unavailable provider names fail visibly. For example:

```yaml
wiki_providers:
  embedder: deterministic
  reranker: lexical
  entailer: structural
  llm_extractor: 'off'
  meta:
    retrieval_profile: full
```

The shipped providers run locally without external models. The deterministic hash embedder
is a retrieval fallback, not a trained semantic model. Full/hybrid profiles maintain vector
indexes, including backfill and invalidation on embedding changes. Narrow stays the default.
The answer checks handle ordinary English grammatical variants conservatively; they are
not a general language understanding guarantee.

Dream stays disarmed and unscheduled; lessons and knowledge admission remain explicit library
capabilities. Capture and retrieval do not automatically promote instructions or lessons.
Evaluate any future experiential-memory service separately from this wiki integration.
