"""The watchdog (op-021): an advisory guard for destructive git work. Design: design/watchdog/design.md.

`watcher` prints one line per git or record event for an on-call /watchdog session to wake on.
`preflight` reports what a destructive git command would touch and gives a verdict, `undo` keeps an
undo point before the owner signs, `postcheck` confirms what moved afterwards. Nothing here blocks
or runs another session's command (brief D1).
"""
