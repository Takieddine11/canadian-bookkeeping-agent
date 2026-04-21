"""Unit tests for the engagement lifecycle state machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.orchestrator.state_machine import (
    advance_from_intake,
    intake_status,
    is_ready_trigger,
)
from src.store.engagement_db import (
    CONV_PERSONAL,
    DOC_BALANCE_SHEET,
    DOC_BANK_STATEMENT,
    DOC_JOURNAL,
    DOC_PNL,
    PHASE_INTAKE,
    PHASE_RECONCILIATION,
    EngagementStore,
)


@pytest.fixture
def store(tmp_path: Path) -> EngagementStore:
    return EngagementStore(root=tmp_path / "engagements")


@pytest.mark.parametrize("text,expected", [
    ("ready", True),
    ("  Ready  ", True),
    ("DONE", True),
    ("go", True),
    ("Analyze", True),
    ("audit", True),
    ("that's all", True),
    ("all set", True),
    ("ready to audit", True),
    ("I am ready", False),       # not a bare trigger
    ("new audit 2025", False),
    ("", False),
    ("run the audit please", False),  # phrase has extra words
])
def test_is_ready_trigger(text: str, expected: bool) -> None:
    assert is_ready_trigger(text) is expected


def test_intake_status_empty(store: EngagementStore) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    status = intake_status(store, eng)
    assert status.core_present == set()
    assert status.core_missing == {DOC_JOURNAL, DOC_BALANCE_SHEET, DOC_PNL}
    assert status.ready_for_audit is False


def test_intake_status_partial(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    f = tmp_path / "x.csv"
    f.write_bytes(b"x")
    store.attach_document(eng.engagement_id, DOC_JOURNAL, f)
    status = intake_status(store, eng)
    assert status.core_present == {DOC_JOURNAL}
    assert status.core_missing == {DOC_BALANCE_SHEET, DOC_PNL}
    assert status.ready_for_audit is False


def test_intake_status_all_core(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    for doc_type in (DOC_JOURNAL, DOC_BALANCE_SHEET, DOC_PNL):
        f = tmp_path / f"{doc_type}.x"
        f.write_bytes(b"x")
        store.attach_document(eng.engagement_id, doc_type, f)
    status = intake_status(store, eng)
    assert status.has_all_core
    assert status.ready_for_audit is True
    assert status.optional_present == set()


def test_intake_status_with_bank_statement(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    for doc_type in (DOC_JOURNAL, DOC_BALANCE_SHEET, DOC_PNL, DOC_BANK_STATEMENT):
        f = tmp_path / f"{doc_type}.x"
        f.write_bytes(b"x")
        store.attach_document(eng.engagement_id, doc_type, f)
    status = intake_status(store, eng)
    assert status.has_all_core
    assert status.optional_present == {DOC_BANK_STATEMENT}


def test_advance_from_intake_moves_to_reconciliation(store: EngagementStore) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    new_phase = advance_from_intake(store, eng)
    assert new_phase == PHASE_RECONCILIATION
    refreshed = store.get_active_engagement("c1")
    assert refreshed is not None
    assert refreshed.phase == PHASE_RECONCILIATION


def test_advance_from_intake_idempotent(store: EngagementStore) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    advance_from_intake(store, eng)
    # Refreshed engagement is already past intake; second call is a no-op.
    refreshed = store.get_active_engagement("c1")
    assert refreshed is not None
    second = advance_from_intake(store, refreshed)
    assert second == PHASE_RECONCILIATION
