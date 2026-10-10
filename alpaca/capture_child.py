"""The child side of the capture runner (t-070 RC4): `python -m alpaca.capture_child`.

`capture_runner.run_process` starts this module in a fresh interpreter, writes one job on its stdin
({"target": "module:function", "root", "kwargs", "fd", "progress"}) and reads JSON lines from the pipe
`fd`: `{"progress": {...}}` after each document, then `{"ok": value}` or `{"error": text, "type": name}`,
each final line with `"package"`, the folder this process imported `alpaca` from, which the parent
compares with its own (t-070 round 2 C-A).
The parent kills this process at its deadline, so nothing here needs a deadline of its own beyond the
work's (`deadline_s`, `hard_s`). This is the only module that imports the work by the name it is
given; no module of alpaca imports it, it only runs as its own process.
"""
import importlib
import json
import os
import sys

from alpaca import capture_runner


def resolve(target):
    """The callable `module:function` names."""
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


def main():
    """Read one job on stdin, run it, report on the job's pipe, and leave with os._exit."""
    job = json.loads(sys.stdin.read())
    fd = int(job["fd"])

    package = os.path.dirname(os.path.dirname(os.path.abspath(capture_runner.__file__)))

    def send(message):
        data = (json.dumps(message, default=str) + "\n").encode("utf-8")
        while data:
            data = data[os.write(fd, data):]

    def report(**fields):
        send({"progress": fields})
    try:
        value = resolve(job["target"])(job["root"], **capture_runner._child_kwargs(
            job.get("kwargs") or {}, report if job.get("progress") else None))
    except BaseException as exc:                    # the parent records it as drain-failed
        try:
            send({"error": capture_runner._describe(exc), "type": type(exc).__name__,
                  "package": package})
        finally:
            os._exit(1)
    send({"ok": value, "package": package})
    os._exit(0)


if __name__ == "__main__":
    main()
