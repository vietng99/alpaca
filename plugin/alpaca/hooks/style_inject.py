#!/usr/bin/env python3
"""Load an opted-in writing rule from this bundle, using only project-local flags."""
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ALPACA_ROOT") or HERE.parents[2]).resolve()
if not (ROOT / "ALPACA-MANIFEST").is_file():
    raise SystemExit("Optional Alpaca hooks require an explicit local Alpaca project.")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RULES = {
    "sam": (".sam-always", "skills/sam/sam-core.md", "UserPromptSubmit"),
    "adhd": (".i-have-adhd-always", "skills/i-have-adhd/skills/i-have-adhd/SKILL.md", "SessionStart"),
    "html-safe": (".html-safe-always", "rules/html-safe/RULE.md", "SessionStart"),
}


def main():
    selected = RULES.get(sys.argv[1] if len(sys.argv) == 2 else "")
    if selected is None:
        return 2
    flag, relative, event = selected
    if not (ROOT / ".alpaca/config" / flag).is_file():
        return 0
    try:
        text = (HERE.parent / relative).read_text(encoding="utf-8")
    except OSError:
        return 0
    lines = text.splitlines()
    if lines and lines[0] == "---":
        try:
            text = "\n".join(lines[lines.index("---", 1) + 1:])
        except ValueError:
            pass
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                             "additionalContext": text}}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
