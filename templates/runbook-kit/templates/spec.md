# Feature Specification: My service

<!--
  A minimal spec in the spec-kit shape. The checker reads three kinds of line:
  - `- **FR-001**: ...` functional requirements (a check should show them; a warning if not)
  - `- **SC-001**: ...` success criteria (every one must be covered by a check or an owner gate)
  - `- **EC-001**: ...` edge cases, under `### Edge Cases` (every one must be covered too; an
    edge case without an id is an error)
  Write each criterion so a check can show it: a number, a file, a status, a line in a log.
  Mark what is still open with [NEEDS CLARIFICATION: ...]; the checker warns while one is left.
-->

**Status**: Draft

## User Scenarios & Testing

### User Story 1 - The main thing a user does (Priority: P1)

One or two sentences on what the user does and why.

**Acceptance Scenarios**:

1. **Given** a starting state, **When** the user acts, **Then** this is the result.

### Edge Cases

- **EC-001**: An empty input is refused with a clear message, not a crash.

## Requirements

### Functional Requirements

- **FR-001**: System MUST do the main thing.

## Success Criteria

### Measurable Outcomes

- **SC-001**: The test suite passes with no failed test.
