"""The hook steps that run under an enforced budget (t-070 RC5, part B; the rest of RC4).

RC4 moved the wiki capture of the Stop, PreCompact and SessionEnd hooks into a child process that the
capture runner kills at the hook's deadline (alpaca/capture_runner.py), because a SIGALRM deadline is
only as good as the longest blocking call under it: a call asleep in the kernel holds the signal off
until it returns (A-4, a `pwrite64` that slept 56 s). The hooks' other steps that read or write files
the hook does not bound (the pad, the data push and the hub tile, the transcript snapshot, the
analytics build, a domain profile's refresh and the SessionEnd consolidation step) ran in the hook's
own process under that signal alone. Each now runs here, as its own job of the same runner
(`common.run_step`): in a hook, `capture_runner.run_process` starts `python -m alpaca.capture_child`
on the function named below and kills it at the step's share of the hook's time; outside a hook (a
verb, a test) `common.run_step` calls the function in its own process, as before.

Every function takes the project root first and keywords after, returns JSON, and opens what it needs
itself (a child has no connection of the hook's to borrow).
"""


def pad(root):
    """`RESUME.md`, the checklist and `board.json` from the record (alpaca/pad.py `write`)."""
    from alpaca import pad as _pad
    _pad.write(root)


def export(root):
    """The hub's data.json, pushed only when the record changed it (alpaca/export.py)."""
    from alpaca import export as _export
    _export.write_if_changed(root)


def hub(root):
    """The SessionEnd hub tile: data.json from the record first, then the publish
    (alpaca/hub_publish.py `at_session_end`)."""
    from alpaca import export as _export, hub_publish
    _export.write_if_changed(root)
    hub_publish.at_session_end(root)


def snapshot(root, *, session, event=False):
    """The local copy of the session's registered transcript (alpaca/transcripts.py `snapshot`);
    with `event` the recorded `transcript-snapshot` fact. Returns how the copy went."""
    from alpaca import transcripts
    fact = transcripts.snapshot(root, session, event=event)
    return {"mode": fact.get("mode"), "bytes": fact.get("bytes")}


def analytics(root, *, session=None):
    """The analytics page: this session's fold first when `session` is given (the Stop hook's tenth
    stop), then the merged page (alpaca/analytics/build_index.py)."""
    from alpaca.analytics import build_index
    if session:
        build_index.collect(root, only=session)
    build_index.build(root)


def profile(root, *, session):
    """The domain profile's own projections after a session end (alpaca/profile.py `refresh`)."""
    from alpaca import profile as _profile
    _profile.load(root).refresh(root, session)


def consolidate(root, *, session, deadline_s, where="session_end_consolidate"):
    """One bounded consolidation step after the SessionEnd capture (FR-18, architecture.md 9.2); it
    records its own `drain-failed` {where} when it fails (alpaca/wiki/consolidate.py)."""
    from alpaca.wiki import consolidate as _consolidate
    result = _consolidate.run_step_after_capture(root, where=where, session=session,
                                                 deadline_s=deadline_s)
    return None if result is None else {"checkpoint_event_id": getattr(result, "checkpoint_event_id", None)}
