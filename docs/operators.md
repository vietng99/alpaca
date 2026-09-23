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

## Alpaca's own skills

`.claude/skills/` holds Alpaca's own skills: `alpaca-first-chat` (the first chat before
onboarding), `alpaca-onboard`, `alpaca-op`, `alpaca-from-notes` and `alpaca-runbook-forge`. They are
Claude Code project skills and mechanism paths in `ALPACA-MANIFEST`, so a clone, a copy, a release
and `alpaca upgrade` all carry them and Claude Code offers each as a `/command` with nothing to
install. The rest of `.claude/skills/` (the spec-kit and OpenSpec skills `alpaca spec init` writes,
and any skill of your own) belongs to the project. Codex reads the same SKILL.md files directly.

## Optional bundled skills

The root lifecycle adapters do not require a plugin installation. To load the optional bundled skills in Claude Code, launch from the project root with `ALPACA_ROOT="$PWD" claude --plugin-dir ./plugin/alpaca`. Codex may read the relevant bundled SKILL.md files directly. No global plugin or agent configuration is installed by bootstrap.
