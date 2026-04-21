"""Unit tests for Agent 4 — CPA Reviewer memo aggregator."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.base import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_OK,
    SEVERITY_WARN,
    Finding,
)
from src.agents.cpa_reviewer import build_memo
from src.store.engagement_db import CONV_PERSONAL, EngagementStore


@pytest.fixture
def engagement(tmp_path: Path):
    store = EngagementStore(root=tmp_path / "engagements")
    return store.create_engagement(
        "conv-1", CONV_PERSONAL, client_id="Acme Corp", period_description="Q3 2026"
    )


def _mk(severity: str, agent: str = "rollforward", title: str = "t",
        fix: str = "") -> Finding:
    return Finding(agent=agent, check="c", severity=severity, title=title, proposed_fix=fix)


def test_clean_audit_is_sign_off_ready(engagement) -> None:
    findings = [
        _mk(SEVERITY_OK, agent="rollforward", title="Identity OK"),
        _mk(SEVERITY_OK, agent="reconciliation", title="No duplicates"),
        _mk(SEVERITY_INFO, agent="tax_auditor", title="Accounts summary"),
    ]
    memo = build_memo(engagement, findings, company="Acme Corp")
    assert memo.sign_off_ready is True
    assert memo.n_errors == 0
    assert memo.n_warnings == 0
    assert "ready for cpa sign-off" in memo.executive_summary.lower()


def test_error_blocks_sign_off(engagement) -> None:
    findings = [
        _mk(SEVERITY_ERROR, agent="tax_auditor",
            title="QST account missing",
            fix="Create a QST Payable account and re-code Quebec purchases."),
        _mk(SEVERITY_WARN, agent="rollforward", title="Suspense non-zero"),
        _mk(SEVERITY_OK, agent="reconciliation", title="No duplicates"),
    ]
    memo = build_memo(engagement, findings, company="Acme Corp")
    assert memo.sign_off_ready is False
    assert memo.n_errors == 1
    assert memo.n_warnings == 1
    assert any("QST account missing" in a for a in memo.actions_required)
    assert any("Suspense non-zero" in w for w in memo.recommend_review)


def test_only_warnings_passes_but_flags(engagement) -> None:
    findings = [
        _mk(SEVERITY_WARN, title="Rate outlier"),
        _mk(SEVERITY_INFO, title="Top vendors"),
        _mk(SEVERITY_OK, title="Identity OK"),
    ]
    memo = build_memo(engagement, findings, company="Acme Corp")
    assert memo.sign_off_ready is True  # No errors → sign-off is possible
    assert memo.n_warnings == 1
    assert "approve after review" in memo.executive_summary.lower()


def test_memo_preserves_agent_attribution(engagement) -> None:
    findings = [
        _mk(SEVERITY_ERROR, agent="reconciliation", title="11 duplicate groups"),
        _mk(SEVERITY_ERROR, agent="tax_auditor", title="QST missing"),
    ]
    memo = build_memo(engagement, findings, company="Acme Corp")
    text = " ".join(memo.actions_required)
    assert "Reconciliation (Agent 3)" in text
    assert "Sales tax (Agent 2)" in text


def test_counts_are_correct(engagement) -> None:
    findings = [
        _mk(SEVERITY_ERROR),
        _mk(SEVERITY_ERROR),
        _mk(SEVERITY_WARN),
        _mk(SEVERITY_INFO),
        _mk(SEVERITY_INFO),
        _mk(SEVERITY_OK),
        _mk(SEVERITY_OK),
        _mk(SEVERITY_OK),
    ]
    memo = build_memo(engagement, findings, company="Acme Corp")
    assert memo.total_checks == 8
    assert memo.n_errors == 2
    assert memo.n_warnings == 1
    assert memo.n_info == 2
    assert memo.n_ok == 3


def test_fix_line_truncated_if_long(engagement) -> None:
    long_fix = "A" * 300
    findings = [_mk(SEVERITY_ERROR, title="x", fix=long_fix)]
    memo = build_memo(engagement, findings, company="Acme Corp")
    joined = " ".join(memo.actions_required)
    # The original 300-A run should be truncated with an ellipsis.
    assert "A" * 300 not in joined
    assert "…" in joined


def test_falls_back_to_engagement_client_id(engagement) -> None:
    memo = build_memo(engagement, [_mk(SEVERITY_OK)])
    assert memo.company == "Acme Corp"
