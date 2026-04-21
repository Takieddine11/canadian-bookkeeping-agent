"""Unit tests for the engagement store.

These tests exercise the two-DB layout (global index + per-engagement DB) without
any Teams / Bot Framework dependencies. Run with ``pytest tests/test_store/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.store.engagement_db import (
    CONV_CHANNEL,
    CONV_PERSONAL,
    DOC_BALANCE_SHEET,
    DOC_JOURNAL,
    PHASE_DELIVERED,
    PHASE_INTAKE,
    PHASE_TAX_AUDIT,
    EngagementStore,
)


@pytest.fixture
def store(tmp_path: Path) -> EngagementStore:
    return EngagementStore(root=tmp_path / "engagements")


def test_init_creates_index_db(tmp_path: Path) -> None:
    root = tmp_path / "engagements"
    EngagementStore(root=root)
    assert (root / "_index.db").exists()


def test_reinit_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "engagements"
    EngagementStore(root=root)
    EngagementStore(root=root)  # must not raise


def test_create_engagement_writes_both_dbs(store: EngagementStore) -> None:
    eng = store.create_engagement(
        conversation_id="conv-1",
        conversation_type=CONV_PERSONAL,
        user_aad_id="user-aad-1",
        client_id="acme-corp",
        period_description="Q3 2026",
    )
    assert eng.engagement_id
    assert eng.phase == PHASE_INTAKE
    assert eng.conversation_type == CONV_PERSONAL
    assert eng.db_path.exists()
    assert eng.db_path.name == "engagement.db"
    assert eng.db_path.parent.name == eng.engagement_id


def test_get_active_engagement_returns_latest_non_delivered(store: EngagementStore) -> None:
    first = store.create_engagement("conv-A", CONV_PERSONAL, period_description="Q1 2026")
    second = store.create_engagement("conv-A", CONV_PERSONAL, period_description="Q2 2026")

    active = store.get_active_engagement("conv-A")
    assert active is not None
    assert active.engagement_id == second.engagement_id

    store.update_phase(second.engagement_id, PHASE_DELIVERED)
    active = store.get_active_engagement("conv-A")
    assert active is not None
    assert active.engagement_id == first.engagement_id


def test_get_active_engagement_none_when_empty(store: EngagementStore) -> None:
    assert store.get_active_engagement("conv-A") is None


def test_get_active_engagement_scopes_by_conversation(store: EngagementStore) -> None:
    store.create_engagement("conv-A", CONV_PERSONAL, period_description="Q1")
    other = store.create_engagement("conv-B", CONV_CHANNEL, period_description="Q1")
    active = store.get_active_engagement("conv-B")
    assert active is not None
    assert active.engagement_id == other.engagement_id
    assert active.conversation_type == CONV_CHANNEL


def test_attach_document_and_list(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("conv-A", CONV_PERSONAL, period_description="Q3")
    file1 = tmp_path / "journal.pdf"
    file1.write_bytes(b"fake pdf")
    file2 = tmp_path / "bs.xlsx"
    file2.write_bytes(b"fake xlsx")

    doc1 = store.attach_document(eng.engagement_id, DOC_JOURNAL, file1, "Journal Report Q3.pdf")
    doc2 = store.attach_document(eng.engagement_id, DOC_BALANCE_SHEET, file2, "BS Q3.xlsx")

    docs = store.list_documents(eng.engagement_id)
    assert [d.id for d in docs] == [doc1, doc2]
    assert {d.doc_type for d in docs} == {DOC_JOURNAL, DOC_BALANCE_SHEET}
    assert docs[0].original_filename == "Journal Report Q3.pdf"


def test_attach_document_unknown_engagement_raises(store: EngagementStore) -> None:
    with pytest.raises(KeyError):
        store.attach_document("does-not-exist", DOC_JOURNAL, "/tmp/x.pdf")


def test_update_phase_propagates_to_both_dbs(store: EngagementStore) -> None:
    eng = store.create_engagement("conv-A", CONV_PERSONAL, period_description="Q3")
    store.update_phase(eng.engagement_id, PHASE_TAX_AUDIT)

    refreshed = store.get_active_engagement("conv-A")
    assert refreshed is not None
    assert refreshed.phase == PHASE_TAX_AUDIT
