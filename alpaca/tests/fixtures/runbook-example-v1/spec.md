# Feature Specification: Link shortener service

**Feature Branch**: `001-link-shortener`

**Created**: 2026-09-24

**Status**: Draft

**Input**: User description: "A small HTTP service that turns a long URL into a short code and redirects the code back to the URL, with a hit counter per code."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Shorten a link (Priority: P1)

A user posts a long URL and gets back a short code they can share.

**Why this priority**: without it there is nothing to redirect.

**Independent Test**: post a URL to `/links` and read the code from the response.

**Acceptance Scenarios**:

1. **Given** a valid http or https URL, **When** the user posts it to `/links`, **Then** the service answers 201 with a code of 7 characters.
2. **Given** a string that is not a URL, **When** the user posts it, **Then** the service answers 400 and stores nothing.

---

### User Story 2 - Follow a short link (Priority: P1)

Anyone who opens `/<code>` is sent to the long URL.

**Why this priority**: this is the reason the link was shortened.

**Independent Test**: create a code, then request `/<code>` and read the `Location` header.

**Acceptance Scenarios**:

1. **Given** a known code, **When** a client requests `/<code>`, **Then** the service answers 301 with the long URL in `Location`.
2. **Given** an unknown code, **When** a client requests it, **Then** the service answers 404.

---

### User Story 3 - See how often a link was used (Priority: P2)

The creator of a link reads its hit count.

**Why this priority**: useful, but the service works without it.

**Independent Test**: follow a code three times, then read `/links/<code>/stats`.

**Acceptance Scenarios**:

1. **Given** a code followed three times, **When** the creator reads its stats, **Then** the count is 3.

---

### Edge Cases

- A URL longer than 2048 characters is refused with 400.
- The same long URL posted twice gets two different codes.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST accept an http or https URL of at most 2048 characters and return a new 7-character code.
- **FR-002**: System MUST refuse anything that is not an http or https URL with status 400.
- **FR-003**: System MUST redirect a known code to its URL with status 301.
- **FR-004**: System MUST answer 404 for an unknown code.
- **FR-005**: System MUST count each redirect and return the count at `/links/<code>/stats`.
- **FR-006**: System MUST keep links across a restart of the service.

### Key Entities

- **Link**: a code, the long URL, the creation time and a hit count.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every endpoint answers with the status codes the acceptance scenarios name, in an automated test run.
- **SC-002**: A redirect answers within 50 ms at the 95th percentile under 200 requests per second.
- **SC-003**: No link is lost when the service restarts.
- **SC-004**: A new team member can start the service locally in under 10 minutes by following the README.

## Assumptions

- Links never expire in the first release.
- The service runs behind a proxy that terminates TLS.
