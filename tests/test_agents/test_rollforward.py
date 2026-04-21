"""Unit tests for Agent 5 — Rollforward checks."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from src.agents.base import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_OK,
    SEVERITY_WARN,
)
from src.agents.rollforward import AGENT, run as run_rollforward
from src.store.engagement_db import (
    CONV_PERSONAL,
    DOC_BALANCE_SHEET,
    DOC_PNL,
    EngagementStore,
)


def _write(path: Path, rows: list[tuple]) -> None:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)


def _balance_sheet(
    path: Path, *, assets: float, liab: float, equity: float,
    profit: float, re: float, dividends: float | None = None,
    gst_payable: float | None = None, gst_suspense: float | None = None,
    tle: float | None = None,
    bank_accounts: list[tuple[str, float]] | None = None,
    inventory: float | None = None,
) -> None:
    rows: list[tuple] = [
        ("Test Client Inc.",),
        ("Balance Sheet",),
        ("As of December 31, 2025",),
        (None,),
        (None, "Total"),
        ("Assets",),
    ]
    if bank_accounts or inventory is not None:
        rows.append(("   Current Assets",))
        if bank_accounts:
            rows.append(("      Cash and Cash Equivalent",))
            total_cash = 0.0
            for name, amt in bank_accounts:
                rows.append((f"         {name}", amt))
                total_cash += amt
            rows.append(("      Total Cash and Cash Equivalent", total_cash))
        if inventory is not None:
            rows.append(("      Inventory", inventory))
        rows.append(("   Total Current Assets", (total_cash if bank_accounts else 0.0) + (inventory or 0.0)))
    rows.append(("Total Assets", assets))
    rows.append(("Liabilities and Equity",))
    rows.append(("   Liabilities",))
    if gst_payable is not None or gst_suspense is not None:
        rows.append(("      Current Liabilities",))
        if gst_payable is not None:
            rows.append(("         GST/HST Payable", gst_payable))
        if gst_suspense is not None:
            rows.append(("         GST/HST Suspense", gst_suspense))
        rows.append(("      Total Current Liabilities",
                    (gst_payable or 0) + (gst_suspense or 0)))
    rows.append(("   Total Liabilities", liab))
    rows.append(("   Equity",))
    if dividends is not None:
        rows.append(("      Dividends", dividends))
    rows.append(("      Retained Earnings", re))
    rows.append(("      Profit for the year", profit))
    rows.append(("   Total Equity", equity))
    rows.append(("Total Liabilities and Equity", tle if tle is not None else assets))
    rows.append((None,))
    rows.append(("Tuesday, Apr. 21, 2026 08:36:09 a.m. GMT-7 - Accrual Basis",))
    _write(path, rows)


def _pnl(path: Path, *, income: float, cogs: float, expenses: float, profit: float) -> None:
    gross = income - cogs
    _write(path, [
        ("Test Client Inc.",),
        ("Profit and Loss",),
        ("January - December 2025",),
        (None,),
        (None, "Total"),
        ("   INCOME",),
        ("      Sales", income),
        ("   Total Income", income),
        ("   COST OF GOODS SOLD",),
        ("      Materials", cogs),
        ("   Total Cost of Goods Sold", cogs),
        ("GROSS PROFIT", gross),
        ("EXPENSES",),
        ("   Rent", expenses),
        ("Total Expenses", expenses),
        ("PROFIT", profit),
        (None,),
        ("Tuesday, Apr. 21, 2026 - Accrual Basis",),
    ])


@pytest.fixture
def store(tmp_path: Path) -> EngagementStore:
    return EngagementStore(root=tmp_path / "engagements")


@pytest.fixture
def engagement_with_docs(store: EngagementStore, tmp_path: Path):
    """Clean engagement: BS balances, profit ties, GST/HST zero."""
    eng = store.create_engagement("conv-1", CONV_PERSONAL, period_description="2025")
    bs = tmp_path / "bs.xlsx"
    pl = tmp_path / "pnl.xlsx"
    _balance_sheet(
        bs, assets=100000, liab=20000, equity=80000, profit=10000,
        re=70000, gst_payable=5000, gst_suspense=0.0,
        bank_accounts=[("Scotia Checking", 40000.0), ("Scotia Savings", 60000.0)],
        inventory=15000.0,  # clean books with COGS should have an inventory account
    )
    _pnl(pl, income=200000, cogs=100000, expenses=90000, profit=10000)
    store.attach_document(eng.engagement_id, DOC_BALANCE_SHEET, bs)
    store.attach_document(eng.engagement_id, DOC_PNL, pl)
    return eng, bs, pl


def _find(findings, check: str):
    for f in findings:
        if f.check == check:
            return f
    return None


def test_clean_books_passes_all(engagement_with_docs, store: EngagementStore) -> None:
    eng, _, _ = engagement_with_docs
    findings = run_rollforward(store, eng)

    identity = _find(findings, "accounting_identity")
    profit = _find(findings, "profit_tie")
    assert identity is not None and identity.severity == SEVERITY_OK
    assert profit is not None and profit.severity == SEVERITY_OK
    # No errors in a clean set of books — note: inventory present warns (ask to confirm
    # closing count), but does not error.
    assert all(f.severity != SEVERITY_ERROR for f in findings), [
        f.title for f in findings if f.severity == SEVERITY_ERROR
    ]
    assert all(f.agent == AGENT for f in findings)


def test_accounting_identity_breach(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("conv-1", CONV_PERSONAL, period_description="2025")
    bs = tmp_path / "bs.xlsx"
    pl = tmp_path / "pnl.xlsx"
    # Assets 100k, TL&E deliberately 99k → 1,000 mismatch.
    _balance_sheet(bs, assets=100000, liab=20000, equity=79000, profit=10000, re=69000, tle=99000)
    _pnl(pl, income=200000, cogs=100000, expenses=90000, profit=10000)
    store.attach_document(eng.engagement_id, DOC_BALANCE_SHEET, bs)
    store.attach_document(eng.engagement_id, DOC_PNL, pl)

    findings = run_rollforward(store, eng)
    identity = _find(findings, "accounting_identity")
    assert identity is not None
    assert identity.severity == SEVERITY_ERROR
    assert identity.delta is not None
    assert abs(identity.delta) >= 999


def test_profit_mismatch_flags_error(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("conv-1", CONV_PERSONAL, period_description="2025")
    bs = tmp_path / "bs.xlsx"
    pl = tmp_path / "pnl.xlsx"
    _balance_sheet(bs, assets=100000, liab=20000, equity=80000, profit=10000, re=70000)
    _pnl(pl, income=200000, cogs=100000, expenses=90000, profit=11000)  # off by 1k
    store.attach_document(eng.engagement_id, DOC_BALANCE_SHEET, bs)
    store.attach_document(eng.engagement_id, DOC_PNL, pl)

    findings = run_rollforward(store, eng)
    tie = _find(findings, "profit_tie")
    assert tie is not None
    assert tie.severity == SEVERITY_ERROR


def test_gst_hst_suspense_nonzero_warns(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("conv-1", CONV_PERSONAL, period_description="2025")
    bs = tmp_path / "bs.xlsx"
    pl = tmp_path / "pnl.xlsx"
    _balance_sheet(
        bs, assets=100000, liab=20000, equity=80000, profit=10000, re=70000,
        gst_payable=15000, gst_suspense=-5000,
    )
    _pnl(pl, income=200000, cogs=100000, expenses=90000, profit=10000)
    store.attach_document(eng.engagement_id, DOC_BALANCE_SHEET, bs)
    store.attach_document(eng.engagement_id, DOC_PNL, pl)

    findings = run_rollforward(store, eng)
    gst = _find(findings, "gst_hst_balance")
    assert gst is not None
    assert gst.severity == SEVERITY_WARN


def test_missing_bs_produces_error(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("conv-1", CONV_PERSONAL, period_description="2025")
    pl = tmp_path / "pnl.xlsx"
    _pnl(pl, income=10, cogs=5, expenses=3, profit=2)
    store.attach_document(eng.engagement_id, DOC_PNL, pl)

    findings = run_rollforward(store, eng)
    missing = _find(findings, "bs_present")
    assert missing is not None
    assert missing.severity == SEVERITY_ERROR


def test_cogs_without_inventory_is_warn(store: EngagementStore, tmp_path: Path) -> None:
    """COGS-without-inventory is a judgment call (could be a service business) —
    surface as WARN asking the CPA to confirm, not as ERROR."""
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    bs = tmp_path / "bs.xlsx"
    pl = tmp_path / "pnl.xlsx"
    _balance_sheet(bs, assets=100000, liab=20000, equity=80000, profit=10000, re=70000)
    _pnl(pl, income=200000, cogs=50000, expenses=140000, profit=10000)
    store.attach_document(eng.engagement_id, DOC_BALANCE_SHEET, bs)
    store.attach_document(eng.engagement_id, DOC_PNL, pl)
    findings = run_rollforward(store, eng)
    inv = _find(findings, "inventory_missing")
    assert inv is not None
    assert inv.severity == SEVERITY_WARN
    assert "closing" in inv.proposed_fix.lower()


def test_no_cogs_no_inventory_finding(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    bs = tmp_path / "bs.xlsx"
    pl = tmp_path / "pnl.xlsx"
    _balance_sheet(bs, assets=100000, liab=20000, equity=80000, profit=10000, re=70000)
    # COGS = 0 → inventory check is irrelevant, should emit no finding.
    _pnl(pl, income=200000, cogs=0, expenses=190000, profit=10000)
    store.attach_document(eng.engagement_id, DOC_BALANCE_SHEET, bs)
    store.attach_document(eng.engagement_id, DOC_PNL, pl)
    findings = run_rollforward(store, eng)
    assert _find(findings, "inventory_missing") is None
    assert _find(findings, "inventory_closing_count_confirmation") is None


def test_bank_balances_extracted(engagement_with_docs, store: EngagementStore) -> None:
    eng, _, _ = engagement_with_docs
    findings = run_rollforward(store, eng)
    banks = _find(findings, "bank_balances")
    assert banks is not None
    assert banks.severity == SEVERITY_INFO
    assert "Scotia Checking" in banks.detail
    assert "Scotia Savings" in banks.detail
