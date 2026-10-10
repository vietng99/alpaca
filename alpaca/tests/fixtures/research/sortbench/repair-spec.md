# Feature Specification: sortlib bug fixes

## User Scenarios & Testing

### Edge Cases

- **EC-001**: A list whose records all share one key comes back in input order.
- **EC-002**: A fix that edits the tests instead of the code is refused.

## Success Criteria

- **SC-001**: Sorting records with equal keys keeps their input order.
- **SC-002**: sort(items, key=None) sorts by the items themselves.
- **SC-003**: Every correctness test that passes on the base still passes after each fix.
