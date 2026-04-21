"""Unit tests for Agent 3 — Reconciliation deterministic checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.base import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_OK, SEVERITY_WARN
from src.agents.reconciliation import AGENT, _normalize_memo, run as run_recon
from src.store.engagement_db import CONV_PERSONAL, DOC_JOURNAL, EngagementStore


def _write_journal(path: Path, period: str, rows: list[tuple]) -> None:
    """Write a minimal QBO Journal Detail CSV with the given detail rows.

    Each tuple is (group_id, date, type, num, name, desc, account, debit, credit).
    """
    out: list[str] = [
        "Journal,,,,,,,,",
        "Test Client,,,,,,,,",
        f'"{period}",,,,,,,,',
        "",
        ",Transaction date,Transaction type,#,Name,Description,Full name,Debit,Credit",
    ]
    current_group = None
    group_totals: dict[str, tuple[float, float]] = {}
    for group, d, t, n, nm, desc, acct, dr, cr in rows:
        if group != current_group:
            if current_group is not None:
                td, tc = group_totals[current_group]
                out.append(f"Total for {current_group},,,,,,,${td:.2f},${tc:.2f}")
            out.append(f"{group},,,,,,,,")
            current_group = group
            group_totals[group] = (0.0, 0.0)
        td, tc = group_totals[current_group]
        group_totals[current_group] = (
            td + (float(dr) if dr else 0.0),
            tc + (float(cr) if cr else 0.0),
        )
        # Quote fields that might contain commas.
        def q(s): return f'"{s}"' if isinstance(s, str) and ("," in s or '"' in s) else str(s)
        desc_out = q(desc)
        acct_out = q(acct)
        nm_out = q(nm)
        dr_out = f"{dr}" if dr else ""
        cr_out = f"{cr}" if cr else ""
        out.append(f",{d},{t},{n},{nm_out},{desc_out},{acct_out},{dr_out},{cr_out}")
    if current_group is not None:
        td, tc = group_totals[current_group]
        out.append(f"Total for {current_group},,,,,,,${td:.2f},${tc:.2f}")
    path.write_text("\n".join(out), encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> EngagementStore:
    return EngagementStore(root=tmp_path / "engagements")


def _find(findings, check):
    for f in findings:
        if f.check == check:
            return f
    return None


def test_clean_journal_all_ok(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    path = tmp_path / "j.csv"
    _write_journal(path, "January-December 2025", [
        ("1", "01/01/2025", "Journal Entry", "1", "", "Opening", "Cash", "100.00", ""),
        ("1", "01/01/2025", "Journal Entry", "1", "", "Opening", "Equity", "", "100.00"),
        ("2", "15/06/2025", "Expense", "", "Acme", "Office supplies", "Supplies", "50.00", ""),
        ("2", "15/06/2025", "Expense", "", "Acme", "Office supplies", "Cash", "", "50.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, path)

    findings = run_recon(store, eng)
    assert all(f.agent == AGENT for f in findings)
    assert _find(findings, "duplicates").severity == SEVERITY_OK
    assert _find(findings, "missing_account").severity == SEVERITY_OK
    assert _find(findings, "out_of_period").severity == SEVERITY_OK
    assert _find(findings, "monthly_activity").severity == SEVERITY_INFO


def test_exact_duplicate_flagged(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    path = tmp_path / "j.csv"
    _write_journal(path, "January-December 2025", [
        ("1", "15/06/2025", "Expense", "", "Acme", "Office supplies", "Supplies", "50.00", ""),
        ("1", "15/06/2025", "Expense", "", "Acme", "Office supplies", "Cash", "", "50.00"),
        ("2", "15/06/2025", "Expense", "", "Acme", "Office supplies", "Supplies", "50.00", ""),
        ("2", "15/06/2025", "Expense", "", "Acme", "Office supplies", "Cash", "", "50.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, path)

    findings = run_recon(store, eng)
    dup = _find(findings, "duplicates")
    assert dup.severity == SEVERITY_WARN
    assert "duplicate group" in dup.title.lower()


def test_missing_account_only_flags_nonzero_amounts(
    store: EngagementStore, tmp_path: Path
) -> None:
    """$0 lines with no account are invoice line-items — not real audit issues."""
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    path = tmp_path / "j.csv"
    _write_journal(path, "January-December 2025", [
        ("1", "15/06/2025", "Invoice", "", "Client", "Service line detail", "", "", ""),
        ("2", "15/06/2025", "Expense", "", "Acme", "Real expense no account", "", "50.00", ""),
        ("2", "15/06/2025", "Expense", "", "Acme", "Matching credit", "Cash", "", "50.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, path)

    findings = run_recon(store, eng)
    ma = _find(findings, "missing_account")
    assert ma.severity == SEVERITY_ERROR
    assert "1 line" in ma.title  # only the non-zero one


def test_out_of_period_flagged(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025 annual")
    path = tmp_path / "j.csv"
    _write_journal(path, "January-December 2025", [
        ("1", "15/06/2025", "Expense", "", "", "Normal", "Cash", "100.00", ""),
        ("1", "15/06/2025", "Expense", "", "", "Normal", "Equity", "", "100.00"),
        ("2", "15/06/2024", "Expense", "", "", "Prior year leak", "Cash", "50.00", ""),
        ("2", "15/06/2024", "Expense", "", "", "Prior year leak", "Equity", "", "50.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, path)

    findings = run_recon(store, eng)
    oop = _find(findings, "out_of_period")
    assert oop.severity == SEVERITY_WARN
    assert "2024" in oop.detail


def test_monthly_breakdown_info(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    path = tmp_path / "j.csv"
    _write_journal(path, "January-December 2025", [
        ("1", "15/01/2025", "Expense", "", "", "Jan", "Cash", "10.00", ""),
        ("1", "15/01/2025", "Expense", "", "", "Jan", "Equity", "", "10.00"),
        ("2", "15/02/2025", "Expense", "", "", "Feb", "Cash", "20.00", ""),
        ("2", "15/02/2025", "Expense", "", "", "Feb", "Equity", "", "20.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, path)

    findings = run_recon(store, eng)
    m = _find(findings, "monthly_activity")
    assert m.severity == SEVERITY_INFO
    assert "2025-01" in m.detail
    assert "2025-02" in m.detail


def test_interac_deposit_sales_flagged_as_warn(store: EngagementStore, tmp_path: Path) -> None:
    """Interac-to-Sales is a docs check, not an error — coding may be correct."""
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    path = tmp_path / "j.csv"
    _write_journal(path, "January-December 2025", [
        ("1", "15/06/2025", "Deposit", "", "Client", "FREE INTERAC E-TRANSFER, 6/15", "Bank", "100.00", ""),
        ("1", "15/06/2025", "Deposit", "", "Client", "FREE INTERAC E-TRANSFER, 6/15", "Sales", "", "100.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, path)
    findings = run_recon(store, eng)
    sales = _find(findings, "interac_deposits_sales")
    assert sales is not None
    assert sales.severity == SEVERITY_WARN
    assert "receipt" in sales.proposed_fix.lower() or "confirm" in sales.proposed_fix.lower()


def test_interac_deposit_shareholder_flagged_as_warn(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    path = tmp_path / "j.csv"
    _write_journal(path, "January-December 2025", [
        ("1", "15/06/2025", "Deposit", "", "", "INTERAC E-TRANSFER shareholder top-up", "Bank", "500.00", ""),
        ("1", "15/06/2025", "Deposit", "", "", "INTERAC E-TRANSFER shareholder top-up", "Loans to Shareholders", "", "500.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, path)
    findings = run_recon(store, eng)
    sh = _find(findings, "interac_deposits_shareholder")
    assert sh is not None
    assert sh.severity == SEVERITY_WARN
    assert "confirm" in sh.proposed_fix.lower()


def test_interac_deposit_bank_side_is_excluded(store: EngagementStore, tmp_path: Path) -> None:
    """Interac bank-side lines (DR to asset account) are NOT flagged — they're
    bank-ops, not classification problems. Only the revenue/equity side counts."""
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    path = tmp_path / "j.csv"
    # Plain bank-to-bank transfer described as Interac, with no revenue line.
    _write_journal(path, "January-December 2025", [
        ("1", "09/06/2025", "Expense", "", "", "Service Charge INTERAC E-TRANSFER FEE",
         "Scotia Tax Account (1018)", "", "1.00"),
        ("1", "09/06/2025", "Expense", "", "", "Service Charge INTERAC E-TRANSFER FEE",
         "Finance Cost:Bank Charges", "1.00", ""),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, path)
    findings = run_recon(store, eng)
    # No Interac findings beyond the "all clear" OK — bank-side fee should not trigger.
    assert _find(findings, "interac_deposits_sales") is None
    assert _find(findings, "interac_deposits_shareholder") is None
    assert _find(findings, "interac_deposits_other") is None
    clear = _find(findings, "interac_deposits")
    assert clear is not None and clear.severity == SEVERITY_OK


def test_no_interac_deposits_is_ok(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    path = tmp_path / "j.csv"
    _write_journal(path, "January-December 2025", [
        ("1", "15/06/2025", "Expense", "", "Acme", "Office supplies", "Supplies", "50.00", ""),
        ("1", "15/06/2025", "Expense", "", "Acme", "Office supplies", "Cash", "", "50.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, path)
    findings = run_recon(store, eng)
    interac = _find(findings, "interac_deposits")
    assert interac is not None
    assert interac.severity == SEVERITY_OK


def test_missing_journal_errors(store: EngagementStore) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    findings = run_recon(store, eng)
    jp = _find(findings, "journal_present")
    assert jp is not None
    assert jp.severity == SEVERITY_ERROR


@pytest.mark.parametrize("a,b,match", [
    ("  Office supplies ", "office   supplies", True),
    ("Payment – INV #1234", "payment INV 1234", True),
    ("Totally different", "Totally similar", False),
])
def test_normalize_memo(a: str, b: str, match: bool) -> None:
    assert (_normalize_memo(a) == _normalize_memo(b)) is match
