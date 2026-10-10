"""Capture targets for tests of the capture runner (alpaca/capture_runner.py, t-070 RC4).

A test points `capture_runner.CAPTURE` at one of these ("alpaca.tests.runner_fakes:<name>"), so the
child process the runner starts runs it in place of `service.capture`. The child inherits the test's
environment, which is how a test passes a file or a duration to it.
"""
import json
import os
import signal
import time


def broken(root, **kwargs):
    raise RuntimeError("store exploded")


def broken_os(root, **kwargs):
    raise OSError("capture failed")


def spy(root, **kwargs):
    """`service.capture` itself, after one JSON line of its keyword arguments is appended to the file
    $ALPACA_TEST_SPY (the parent test reads the calls the child made)."""
    seen = {k: v for k, v in kwargs.items() if k != "progress"}
    with open(os.environ["ALPACA_TEST_SPY"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(seen, default=str) + "\n")
    from alpaca.wiki import service
    return service.capture(root, **kwargs)


def hold(root, **kwargs):
    """The A-4 shape: a call during which the process takes no catchable signal (a `pwrite64` asleep
    in the kernel defers SIGALRM until it returns). SIGALRM, SIGTERM and SIGINT are blocked while the
    work sleeps $ALPACA_TEST_HOLD_S seconds (default 30) after reporting that it started; then the mask
    is restored, so a signal that came meanwhile is handled late, as the hook's 8 s SIGALRM was at 60 s,
    and only then would it capture. A deadline that relies on a signal reaching this process cannot
    stop it in time; only the runner's SIGKILL from outside can."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGTERM, signal.SIGINT})
    progress = kwargs.get("progress")
    if progress is not None:
        progress(through_event=0, done=0, total=7)
    marker = os.environ.get("ALPACA_TEST_HOLD_MARK")
    if marker:
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write("%d\n" % os.getpid())
    time.sleep(float(os.environ.get("ALPACA_TEST_HOLD_S") or 30))
    signal.pthread_sigmask(signal.SIG_SETMASK, old)
    return (REAL or _service_capture())(root, **kwargs)


#: The real `service.capture` when a test process patched `hold` in its place (see `install_hold`).
REAL = None


def _service_capture():
    from alpaca.wiki import service
    return service.capture


def install_hold():
    """Put `hold` in place of `service.capture` in this process (a `sitecustomize` of a test calls
    it in the hook process it starts, so the hook process on a tree without the runner holds too),
    and name `hold` as the capture target of every child this process starts.

    The runner's child is isolated (t-070 round 2 C-A): it gets no PYTHONPATH, so the test's
    `sitecustomize` no longer runs there. The hook process therefore asks the child, by target name,
    to run `hold`, which captures for real once its hold ends."""
    global REAL
    from alpaca.wiki import service
    if service.capture is not hold:
        REAL = service.capture
        service.capture = hold
    _redirect_child_targets({"alpaca.wiki.service:capture": "alpaca.tests.runner_fakes:hold"})


def _redirect_child_targets(names):
    """In this process, start the runner's child with the target `names` maps a target to."""
    from alpaca import capture_runner
    real = getattr(capture_runner.run_process, "real", capture_runner.run_process)
    mapping = dict(getattr(capture_runner.run_process, "names", {}), **names)

    def run_process(target, kwargs, **options):
        return real(mapping.get(target, target), kwargs, **options)
    run_process.real, run_process.names = real, mapping
    capture_runner.run_process = run_process


def hold_in_transaction(root, progress=None, **kwargs):
    """Open the wiki store as the capture does, write inside a Writer transaction, report, then
    sleep without committing: the runner kills it there."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    absorber = Absorber(Config.for_project(root))
    with absorber.writer.transaction():
        absorber.db.set_meta("rc4-half-written", "yes")
        if progress is not None:
            progress(through_event=0, done=0, total=1)
        time.sleep(60)


def echo(root, **kwargs):
    return {"root": root, "value": kwargs.get("value")}


def vanish(root, **kwargs):
    os._exit(3)


def run_inline(target, kwargs, *, root, kill_s=None, progress=False):
    """`capture_runner.run_process`'s contract run in this process, with no outside deadline: the
    work's own budget (`deadline_s`, `hard_s`) is all that bounds it. For the tests that fake the
    drain's clock or spy on its documents in the test process; the child process itself is tested in
    alpaca/tests/test_capture_runner.py."""
    from alpaca import capture_child, capture_runner
    start = time.monotonic()
    last = {}

    def report(**fields):
        last.clear()
        last.update(fields)
    try:
        value = capture_child.resolve(target)(str(root), **capture_runner._child_kwargs(
            kwargs, report if progress else None))
    except Exception as exc:
        return capture_runner.Result("failed", error=capture_runner._describe(exc), exc=exc,
                                     progress=dict(last) or None, seconds=time.monotonic() - start)
    return capture_runner.Result("ok", value=value, progress=dict(last) or None,
                                 seconds=time.monotonic() - start)


#: The function each hook step of t-070 RC5 calls (alpaca/hook_steps.py), as (module, attribute):
#: `install_step_hold` puts `hold_step` in its place.
STEP_FUNCTIONS = {
    "pad": ("alpaca.pad", "write"),
    "export": ("alpaca.export", "write_if_changed"),
    "snapshot": ("alpaca.transcripts", "snapshot"),
    "analytics": ("alpaca.analytics.build_index", "build"),
    "consolidate": ("alpaca.wiki.consolidate", "run_step_after_capture"),
}


def _held(real):
    def hold_step(*args, **kwargs):
        """The A-4 shape for a hook step: SIGALRM, SIGTERM and SIGINT blocked while it sleeps
        $ALPACA_TEST_HOLD_S seconds, as a `pwrite64` asleep in the kernel holds them off; then the mask
        is restored and the real step runs."""
        old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGTERM, signal.SIGINT})
        marker = os.environ.get("ALPACA_TEST_HOLD_MARK")
        if marker:
            with open(marker, "a", encoding="utf-8") as fh:
                fh.write("%d\n" % os.getpid())
        time.sleep(float(os.environ.get("ALPACA_TEST_HOLD_S") or 30))
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
        return real(*args, **kwargs)
    hold_step.held = True
    return hold_step


def install_step_hold(name):
    """Put `hold_step` in place of the function the hook step `name` calls, in this process (a test's
    `sitecustomize` calls it in the hook process, where a tree without RC5 part B runs the step), and
    have the runner's child run the step through `child_step_<name>`, which does the same there: the
    child is isolated (t-070 round 2 C-A) and runs no `sitecustomize` of the test's."""
    _install_step_hold(name)
    _redirect_child_targets({"alpaca.hook_steps:" + name: "alpaca.tests.runner_fakes:child_step_" + name})


def _install_step_hold(name):
    import importlib
    module_name, attr = STEP_FUNCTIONS[name]
    module = importlib.import_module(module_name)
    real = getattr(module, attr)
    if not getattr(real, "held", False):
        setattr(module, attr, _held(real))


def _child_step(name):
    def child_step(root, **kwargs):
        """In the runner's child: hold the step's function, then run the step."""
        _install_step_hold(name)
        from alpaca import hook_steps
        return getattr(hook_steps, name)(root, **kwargs)
    return child_step


child_step_pad = _child_step("pad")
child_step_export = _child_step("export")
child_step_snapshot = _child_step("snapshot")
child_step_analytics = _child_step("analytics")
child_step_consolidate = _child_step("consolidate")
