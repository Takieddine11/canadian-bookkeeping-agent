"""Engagement lifecycle state machine.

Owns phase transitions and "is intake ready?" decisions. Intentionally stateless —
takes an :class:`EngagementStore` as its dependency and reads/writes through it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.store.engagement_db import (
    DOC_BALANCE_SHEET,
    DOC_BANK_STATEMENT,
    DOC_JOURNAL,
    DOC_PNL,
    PHASE_CPA_REVIEW,
    PHASE_DELIVERED,
    PHASE_INTAKE,
    PHASE_RECONCILIATION,
    PHASE_ROLLFORWARD,
    PHASE_TAX_AUDIT,
    Engagement,
    EngagementStore,
)

log = logging.getLogger(__name__)

CORE_INTAKE_DOCS = (DOC_JOURNAL, DOC_BALANCE_SHEET, DOC_PNL)
OPTIONAL_INTAKE_DOCS = (DOC_BANK_STATEMENT,)

# Phrases the bookkeeper can type to signal "I've uploaded everything I'm going to,
# run the audit now". Matched case-insensitively on the full stripped message.
_READY_TRIGGERS = frozenset({
    "ready", "done", "go", "analyze", "audit", "start", "continue",
    "that's all", "thats all", "that is all", "all set",
    "ready to audit", "run audit", "start audit",
})


@dataclass(frozen=True)
class IntakeStatus:
    """Snapshot of whether the bookkeeper has given us enough to move on."""

    core_present: set[str]        # which of the core doc types are in
    core_missing: set[str]        # which core doc types are still missing
    optional_present: set[str]
    ready_for_audit: bool         # all core docs + user confirmation would be enough

    @property
    def has_all_core(self) -> bool:
        return not self.core_missing


def intake_status(store: EngagementStore, engagement: Engagement) -> IntakeStatus:
    doc_types = {d.doc_type for d in store.list_documents(engagement.engagement_id)}
    core_present = doc_types & set(CORE_INTAKE_DOCS)
    core_missing = set(CORE_INTAKE_DOCS) - core_present
    optional_present = doc_types & set(OPTIONAL_INTAKE_DOCS)
    return IntakeStatus(
        core_present=core_present,
        core_missing=core_missing,
        optional_present=optional_present,
        ready_for_audit=not core_missing,
    )


def is_ready_trigger(text: str) -> bool:
    """True if the bookkeeper typed a phrase that means 'run the audit now'."""
    return text.strip().lower() in _READY_TRIGGERS


def advance_from_intake(
    store: EngagementStore, engagement: Engagement
) -> str:
    """Advance an engagement out of ``intake`` into the first audit phase.

    Returns the new phase. Idempotent — calling on an already-advanced engagement
    is a no-op that returns the current phase.
    """
    if engagement.phase != PHASE_INTAKE:
        log.info(
            "orchestrator.advance_skipped engagement=%s already_in_phase=%s",
            engagement.engagement_id, engagement.phase,
        )
        return engagement.phase

    # Audit phases run 3 -> 5 -> 2 per the plan: reconciliation first (deterministic
    # foundation), then rollforward (arithmetic ties), then tax auditor.
    new_phase = PHASE_RECONCILIATION
    store.update_phase(engagement.engagement_id, new_phase)
    log.info(
        "orchestrator.phase_advanced engagement=%s from=%s to=%s",
        engagement.engagement_id, engagement.phase, new_phase,
    )
    return new_phase


__all__ = [
    "CORE_INTAKE_DOCS",
    "OPTIONAL_INTAKE_DOCS",
    "IntakeStatus",
    "advance_from_intake",
    "intake_status",
    "is_ready_trigger",
    "PHASE_CPA_REVIEW",
    "PHASE_DELIVERED",
    "PHASE_INTAKE",
    "PHASE_RECONCILIATION",
    "PHASE_ROLLFORWARD",
    "PHASE_TAX_AUDIT",
]
