# OpenSpec specs

If your spec is written with OpenSpec instead of spec-kit, the checker reads it too. It detects
the shape by itself: a file with a `### Requirement:` heading is read as OpenSpec.

The items a runbook must cover are the scenarios: every `#### ` heading under a
`### Requirement:` block, named without its `Scenario:` prefix.

```markdown
## Requirements

### Requirement: Session Timeout
The system SHALL expire a session after 30 minutes of inactivity.

#### Scenario: Idle timeout
- **WHEN** 30 minutes pass with no activity
- **THEN** the session is invalidated
```

A check covers it as `<requirement>/<scenario>` (case and extra spaces do not matter):

```yaml
covers: ["Session Timeout/Idle timeout"]
```

What you can pass with `--spec`:

- one `spec.md` (a main spec, or a delta spec of a change);
- a folder of `<capability>/spec.md` files (`openspec/specs`); the items are then
  `<capability>/<requirement>/<scenario>`, and the short form `<requirement>/<scenario>` works
  when only one capability has it;
- a change folder (`openspec/changes/<id>`, with `proposal.md` and a `specs` folder): it is read
  as the living specs next to it with the change applied, so the runbook covers the whole spec
  after the change. Scenarios under `## REMOVED Requirements` need no check.

A scenario whose name starts with a spec-kit id (`#### Scenario: SC-002 redirect latency`)
keeps that id, so `covers: [SC-002]` matches it. FORMAT.md, "Linking checks to the spec", has
the full rules.
