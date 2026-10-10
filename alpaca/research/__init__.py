"""The research engine: an autoresearch loop with an improve mode and a repair mode whose results
can be trusted. Every attempt runs in a jail on its own worktree, is judged by a frozen contract
and is verified before it counts. Design: design/research-engine/architecture.md (the package
tree of section 2.1; this file is the scaffold of section 12.1, which no builder edits)."""

SCHEMA = "research/1"
