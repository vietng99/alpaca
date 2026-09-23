---
name: caveman-stats
description: Report exact usage exposed by the active operator and identify metrics that are unavailable.
---

# Exact session metrics

Read the active operator's native usage/status tool if one is available. Report observed token
counts and remaining allowance with their measurement scope. Do not scan account caches or
unrelated transcripts. If the operator exposes no count, state "Token usage unavailable."
Compression savings require measured before/after counts using the same tokenizer; do not
infer savings from prose length or advertise a fixed percentage for this session.
