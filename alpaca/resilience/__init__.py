"""The resilience kit for unattended runs (M3.12).

Five small mechanisms that keep an unattended run honest, ported against the RECORD rather than
the earlier harness file formats: progress is the events count plus the row cursor, both read from the
store, never a worker-controllable counter.

  * pulse_watch  -- liveness from real progress: healthy | HUNG | DEAD (the earlier harness ops/pulse-watch.py).
  * worker_launch -- a same-host re-entry lock: a second launcher for one worker is refused
                     (the earlier harness ops/worker-launch.py).
  * kicker       -- a durable checker: fire a resume only when the record is quiet AND no live
                    claim holds the work (the earlier harness ops/kicker-check.py).
  * reset_wait   -- fold a rate-limit header pair into one reset instant (the earlier harness ops/reset-wait.py).
  * budget       -- net-new: budget survival, concurrency tiers and preflight floors, written to
                    doctrine/{budget-survival,concurrency-tiers,preflight-floors}.md.

Four of the five port an earlier harness --selftest control table; here each is a `selftest()` callable that
returns 0 on all-PASS and 1 otherwise, wrapped by a pytest case that fails on any non-PASS. budget
has no predecessor; its controls are net-new. Every module reads the resource class and the
reserve from project.yaml, never from code.
"""
