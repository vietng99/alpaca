"""A wall clock pinned to a chosen instant, for the Studio fixture and its server.

The Studio baseline (t-474) must not move with the date: rolling windows on the server (the burn board's
last 24 hours, today's activity), stamps the record writes, and file times all read the wall clock. This
module replaces `time.time`, `time.time_ns`, `time.localtime`, `time.gmtime`, `time.ctime`,
`time.strftime`, `datetime.datetime.now/utcnow/today` and `datetime.date.today` with a clock that stands
still at a chosen instant (or, with `--running`, starts there and runs at real speed). Waits and
timeouts use `time.monotonic`, which is left alone, so the server still answers and its SSE heartbeat
still beats; durations the server computes ("running 2h 5m") stay the same however long a capture
takes. It must be installed before `alpaca` is imported, so every `from datetime import datetime` sees
the patched class.

    python fixed_clock.py --at 2026-03-18T14:20:00Z [--running] -- serve --port 8123 --keep

runs `python -m alpaca serve ...` under that clock (the fixture server of capture and motion checks).
In process, `install(epoch)` returns a controller whose `set(epoch)` moves the clock (the fixture
builder stamps each record row at the time it chooses).
"""
import datetime as _dt
import sys
import time as _time

_real_mono = _time.monotonic


class _Clock:
    def __init__(self, epoch, running=False):
        self.running = running
        self.set(epoch)

    def set(self, epoch):
        self.base, self.mono = float(epoch), _real_mono()

    def now(self):
        return self.base + (_real_mono() - self.mono if self.running else 0.0)


CLOCK = None


def parse(text):
    """Epoch seconds from an ISO 8601 instant ('2026-03-18T14:20:00Z')."""
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def install(epoch, running=False):
    global CLOCK
    if CLOCK is not None:
        CLOCK.running = running
        CLOCK.set(epoch)
        return CLOCK
    CLOCK = clock = _Clock(epoch, running)
    real_localtime, real_gmtime, real_ctime, real_strftime = _time.localtime, _time.gmtime, _time.ctime, _time.strftime
    _time.time = clock.now
    _time.time_ns = lambda: int(clock.now() * 1e9)
    _time.localtime = lambda secs=None: real_localtime(clock.now() if secs is None else secs)
    _time.gmtime = lambda secs=None: real_gmtime(clock.now() if secs is None else secs)
    _time.ctime = lambda secs=None: real_ctime(clock.now() if secs is None else secs)
    _time.strftime = lambda fmt, t=None: real_strftime(fmt, real_localtime(clock.now()) if t is None else t)

    real_datetime, real_date = _dt.datetime, _dt.date

    class datetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock.now(), tz)

        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(clock.now(), _dt.timezone.utc).replace(tzinfo=None)

        @classmethod
        def today(cls):
            return cls.fromtimestamp(clock.now())

    class date(real_date):
        @classmethod
        def today(cls):
            d = real_datetime.fromtimestamp(clock.now())
            return cls(d.year, d.month, d.day)

    _dt.datetime, _dt.date = datetime, date
    return clock


def main(argv):
    if "--" not in argv or argv[0] != "--at":
        print("usage: fixed_clock.py --at <ISO instant> -- <alpaca arguments>", file=sys.stderr)
        return 2
    split = argv.index("--")
    install(parse(argv[1]), running="--running" in argv[:split])
    sys.argv = ["alpaca"] + argv[split + 1:]
    from alpaca.cli import main as alpaca_main
    return alpaca_main()


if __name__ == "__main__":
    # Serve the tree this file belongs to, never another checkout an installed path points at.
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path = [os.path.dirname(os.path.dirname(os.path.dirname(here)))] + [p for p in sys.path if p != here]
    sys.exit(main(sys.argv[1:]))
