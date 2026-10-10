"""Pure native admission diagnostic. Prepared passive receipts never enable research."""
from __future__ import annotations

from alpaca.research import errors

REASONS = (
    ('native-adapter-unimplemented', 'Implement and review the measured native evidence adapter.'),
    ('hook-installation-unverified', 'Measure loading of the reviewed diagnostic definitions.'),
    ('exact-definition-trust-unverified', 'Review exact definition trust through a supported runtime route.'),
    ('live-child-hook-coverage-unmeasured', 'Measure inheritance and coverage in a fresh diagnostic child.'),
    ('effective-spawn-plaintext-unmeasured', 'Establish genuine effective plaintext spawn evidence.'),
    ('independent-operation-witness-unmeasured', 'Reconcile independent receipts with native operation items.'),
    ('call-time-guard-unimplemented', 'Implement and measure actual denial and rewrite behavior.'),
)


def diagnostic() -> dict:
    """No filesystem, environment, DB or purported evidence is consulted."""
    return {'format_version': 1, 'purpose': 'diagnostic', 'ready': False,
            'reasons': [{'code': code, 'remediation': text} for code, text in REASONS]}


def require_ready() -> None:
    missing = ', '.join(item['code'] for item in diagnostic()['reasons'])
    raise errors.refusal('RUN-CODEX-NOT-READY', 'Native research unavailable: ' + missing)
