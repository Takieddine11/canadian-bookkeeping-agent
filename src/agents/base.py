"""Shared primitives for audit agents.

Every agent produces a list of :class:`Finding` objects. The orchestrator stores
them in the engagement DB and posts a rollup card to Teams. Keeping all agents
emitting the same shape means the CPA reviewer (Agent 4) can aggregate findings
across every earlier agent uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

SEVERITY_OK = "ok"            # "✓" — check passed, no action
SEVERITY_INFO = "info"        # "ℹ" — informational context, no action
SEVERITY_WARN = "warn"        # "⚠" — review recommended
SEVERITY_ERROR = "error"      # "✗" — clear problem, action required


@dataclass(frozen=True)
class Finding:
    """One result from an agent's check.

    Designed to round-trip through SQLite (see ``findings`` table) and feed the
    adaptive-card rollup. Keep fields JSON-safe: store ``Decimal`` values as strings
    when persisting.
    """

    agent: str              # "rollforward", "reconciliation", "tax_auditor", ...
    check: str              # short stable id, e.g. "accounting_identity"
    severity: str           # one of SEVERITY_*
    title: str              # one-line human summary
    detail: str = ""        # longer explanation (can be multi-line)
    proposed_fix: str = ""  # what the bookkeeper/CPA should do about it
    delta: Decimal | None = None  # optional numeric gap (e.g., difference amount)


SEVERITY_ORDER = {
    SEVERITY_ERROR: 0,
    SEVERITY_WARN: 1,
    SEVERITY_INFO: 2,
    SEVERITY_OK: 3,
}


SEVERITY_ICONS = {
    SEVERITY_OK: "✓",
    SEVERITY_INFO: "ℹ",
    SEVERITY_WARN: "⚠",
    SEVERITY_ERROR: "✗",
}


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Stable sort: errors first, then warnings, info, ok. Original order within each tier."""
    return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 99),))
