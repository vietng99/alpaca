"""Generic run-log mechanisms a domain profile wires to its own jobs.

  * `telemetry`  - a /proc host and process-tree sampler for one long job, with `window` and
    `summary` readers that keep null for what was not measured;
  * `quotes`     - `check` verifies a review's quotes against the log bytes and its sha256;
  * `reviews`    - `submit` keeps a checked review write-once and records it, `mark` appends an
    engineer's confirm or dispute per finding, `reviews` reads them back with integrity;
  * `junit`      - `parse_junit` turns a JUnit XML report into tests and measurements, null
    never zero;
  * `textwindow` - `read_window` and `whole_chars` read a growing log one byte window at a time,
    cut on whole UTF-8 characters.

Every path and record event kind is passed in; nothing here knows where a domain keeps its
jobs, logs, reviews or reports.
"""
