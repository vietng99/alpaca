---
name: caveman-compress
description: Compress a user-selected local instruction or memory file while preserving facts, code, paths, links, and structure.
---

# Local document compression

Only modify a file explicitly selected by the owner. Keep a pre-image under
`.alpaca/compress/` with a unique filename and record the original path. Draft the shorter
candidate there using the active operator; no external model CLI is required.

Preserve frontmatter, headings, fenced code, inline code, URLs, paths, numbers, negations,
exceptions, and technical claims. From the Alpaca project root, run:

```bash
bin/alpaca-python plugin/alpaca/skills/caveman/skills/caveman-compress/scripts/check.py SOURCE CANDIDATE
```

The helper only compares files. A REVIEW result names structural changes to resolve. Also
compare meaning manually: the helper cannot establish semantic fidelity. Apply the candidate
only after both checks pass. Report the backup path and byte counts; claim token savings only
when a real tokenizer measurement is available. Keep the original unchanged if fidelity cannot
be established after two revisions.
