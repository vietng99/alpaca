# Feature Specification: a faster sortlib.sort

## User Scenarios & Testing

### Edge Cases

- **EC-001**: An empty list and a one-element list come back unchanged.
- **EC-002**: A benchmark run that prints no result markers gets no score.
- **EC-003**: A change that edits the benchmark, the tests or this pack is refused.

## Requirements

- **FR-001**: sortlib.sort keeps its signature and returns a new sorted list.

## Success Criteria

- **SC-001**: The sort benchmark makes at most 18000 comparisons on the fixed input set.
- **SC-002**: The median wall time of the sort benchmark over 5 runs is at most 120 ms.
- **SC-003**: Every correctness test of the pack passes on the champion.
- **SC-004**: A run stops when both targets are met, or after 5 rounds, when it asks the owner.
