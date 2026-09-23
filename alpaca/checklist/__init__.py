"""The checklist engine: spec shape in, Alpaca rows out.

M1.9 lands the front of the pipeline: the acceptance-table parser (`artifact.parse`) and
the per-phase step-model loader (`step_model.load`), consumed downstream by synthesis
(M1.10) and closure (M1.11).

`Halt` is the one refusal this package raises. It carries an integer verdict-band code
from `alpaca.gates.verdict` (never a private copy of those numbers), so a caller decoding a
verdict does not have to know which sub-module raised. A refusal is surfaced, never
laundered into a degraded pass: an empty measured population is BLOCKED, not an empty PASS.
"""
from __future__ import annotations

from alpaca.gates import verdict


class Halt(Exception):
    """A refusal that ends a parse rather than degrading it.

    `verdict` is an integer from the verdict band (`alpaca.gates.verdict`); `code` is the
    machine-stable reason string; `detail` is the human pointer. The numbers are
    referenced from the contract, never restated here.
    """

    def __init__(self, verdict_code: int, code: str, detail: str = ""):
        super().__init__("%s %s: %s" % (verdict.name_of(verdict_code), code, detail))
        if verdict_code not in verdict.VERDICT_BAND:
            raise ValueError("Halt needs a verdict-band code, got %r" % (verdict_code,))
        self.verdict = verdict_code
        self.code = code
        self.detail = detail


__all__ = ["Halt"]
