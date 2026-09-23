# Optional project SQLite runtime

`setup/install-sqlite-runtime.py` builds SQLite 3.53.4 for Linux below
`.alpaca/toolchain/sqlite/3.53.4`. It uses the official amalgamation archive and
refuses a source SHA3-256 mismatch. The manifest records source and library hashes,
compiler flags, SQLite source ID and JSON/FTS5/backup checks. No system package,
Python interpreter, active selector or service process is changed.

```bash
python3 setup/install-sqlite-runtime.py
```

The installation is optional. `bin/alpaca-python` uses host SQLite until the owner
activates a built version by creating `.alpaca/toolchain/sqlite/enabled` containing
`3.53.4` followed by a newline. Write that selector atomically, then restart existing
long-running project processes. A process already running keeps its previously
loaded library. `bin/alpaca` and the Claude Code hooks already invoke this Python wrapper.

The wrapper validates the selected version and the local library's SHA256 sidecar,
then prepends that version's library directory to `LD_LIBRARY_PATH`. Descendant
Python processes inherit the same runtime. A selected runtime with a missing or
mismatched library stops startup; an absent selector leaves the host runtime intact.
Move the selector aside and restart to return to the host runtime. Neither the
library nor selector belongs in a clean shipment.

Verify the activated version and source ID through the actual wrapper:

```bash
bin/alpaca-python -c 'import sqlite3; print(sqlite3.sqlite_version); print(sqlite3.connect(":memory:").execute("select sqlite_source_id()").fetchone()[0])'
```

Before activation, a candidate can be exercised without changing the selector:

```bash
LD_LIBRARY_PATH="$PWD/.alpaca/toolchain/sqlite/3.53.4/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" .venv/bin/python -m pytest
```

The official [WAL documentation](https://www.sqlite.org/wal.html#walresetbug)
identifies the WAL-reset fix in 3.51.3 and later. The pinned archive and its SHA3
come from the [SQLite download page](https://www.sqlite.org/download.html).
Compatibility and integrity checks exercise this installation; they do not replace
upstream race-condition tests or establish that earlier project data was corrupt.
