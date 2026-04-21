"""Unit tests for Agent 2 — Tax auditor (deterministic v1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from src.agents.base import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_OK, SEVERITY_WARN
from src.agents.tax_auditor import AGENT, run as run_tax
from src.store.engagement_db import (
    CONV_PERSONAL,
    DOC_BALANCE_SHEET,
    DOC_JOURNAL,
    EngagementStore,
)


def _write_journal(path: Path, period: str, rows: list[tuple]) -> None:
    """rows = (group_id, date, type, num, name, desc, account, debit, credit)."""
    out: list[str] = [
        "Journal,,,,,,,,",
        "Test,,,,,,,,",
        f'"{period}",,,,,,,,',
        "",
        ",Transaction date,Transaction type,#,Name,Description,Full name,Debit,Credit",
    ]
    current = None
    totals: dict[str, tuple[float, float]] = {}
    for group, d, t, n, nm, desc, acct, dr, cr in rows:
        if group != current:
            if current is not None:
                td, tc = totals[current]
                out.append(f"Total for {current},,,,,,,${td:.2f},${tc:.2f}")
            out.append(f"{group},,,,,,,,")
            current = group
            totals[group] = (0.0, 0.0)
        td, tc = totals[current]
        totals[current] = (td + (float(dr) if dr else 0.0), tc + (float(cr) if cr else 0.0))
        def q(s): return f'"{s}"' if isinstance(s, str) and "," in s else str(s)
        out.append(
            f",{d},{t},{n},{q(nm)},{q(desc)},{q(acct)},"
            f"{dr or ''},{cr or ''}"
        )
    if current is not None:
        td, tc = totals[current]
        out.append(f"Total for {current},,,,,,,${td:.2f},${tc:.2f}")
    path.write_text("\n".join(out), encoding="utf-8")


def _write_bs(path: Path, gst_hst_payable: float = 0.0) -> None:
    wb = Workbook()
    ws = wb.active
    for row in [
        ("Test",),
        ("Balance Sheet",),
        ("As of December 31, 2025",),
        (None,),
        (None, "Total"),
        ("Assets",),
        ("Total Assets", 100.0),
        ("Liabilities and Equity",),
        ("   Liabilities",),
        ("      Current Liabilities",),
        ("         GST/HST Payable", gst_hst_payable),
        ("      Total Current Liabilities", gst_hst_payable),
        ("   Total Liabilities", gst_hst_payable),
        ("Total Liabilities and Equity", 100.0),
        ("Tuesday - Accrual Basis",),
    ]:
        ws.append(row)
    wb.save(path)


@pytest.fixture
def store(tmp_path: Path) -> EngagementStore:
    return EngagementStore(root=tmp_path / "engagements")


def _find(findings, check):
    for f in findings:
        if f.check == check:
            return f
    return None


def test_no_journal_returns_error(store: EngagementStore) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    findings = run_tax(store, eng)
    jp = _find(findings, "journal_present")
    assert jp is not None and jp.severity == SEVERITY_ERROR


def test_inventory_lists_tax_accounts(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, "2025", [
        ("1", "15/06/2025", "Expense", "", "Vendor", "Purchase", "GST/HST Payable", "5.00", ""),
        ("1", "15/06/2025", "Expense", "", "Vendor", "Purchase", "Supplies", "100.00", ""),
        ("1", "15/06/2025", "Expense", "", "Vendor", "Purchase", "Bank", "", "105.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_tax(store, eng)
    inv = _find(findings, "tax_accounts_present")
    assert inv is not None
    assert "GST/HST Payable" in inv.detail


def test_quebec_activity_flagged_as_info_not_error(
    store: EngagementStore, tmp_path: Path
) -> None:
    """QBO tracks GST+QST via tax codes, not separate accounts. A Quebec file with
    one consolidated GST/HST Payable account is correct — we surface Quebec activity
    as INFO so the CPA can verify the Tax Center config, not as an error."""
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, "2025", [
        ("1", "15/06/2025", "Expense", "", "QC Vendor", "Purchase", "Supplies", "1000.00", ""),
        ("1", "15/06/2025", "Expense", "", "QC Vendor", "Purchase", "GST/HST Payable", "149.75", ""),
        ("1", "15/06/2025", "Expense", "", "QC Vendor", "Purchase", "Bank", "", "1149.75"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_tax(store, eng)
    qc = _find(findings, "quebec_activity_detected")
    assert qc is not None
    assert qc.severity == SEVERITY_INFO
    assert "tax center" in qc.proposed_fix.lower()
    # No error should fire about a missing QST account under the new rule.
    assert _find(findings, "qst_account_missing") is None


def test_customer_invoices_dont_pollute_vendor_analysis(
    store: EngagementStore, tmp_path: Path
) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, "2025", [
        # Customer invoice — must be excluded from vendor stats.
        ("1", "15/06/2025", "Invoice", "", "Acme Customer", "Service", "A/R", "1149.75", ""),
        ("1", "15/06/2025", "Invoice", "", "Acme Customer", "Service", "Sales", "", "1000.00"),
        ("1", "15/06/2025", "Invoice", "", "Acme Customer", "Service", "GST/HST Payable", "", "149.75"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_tax(store, eng)
    top = _find(findings, "top_vendors_by_spend")
    # No actual vendor transactions → no top-vendors finding emitted.
    assert top is None or "Acme Customer" not in (top.detail or "")


def test_rate_outlier_flagged(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    # Spend 1000, tax 80 → 8% = non-standard rate.
    _write_journal(j, "2025", [
        ("1", "15/06/2025", "Expense", "", "Odd Vendor", "Purchase", "Supplies", "1000.00", ""),
        ("1", "15/06/2025", "Expense", "", "Odd Vendor", "Purchase", "GST/HST Payable", "80.00", ""),
        ("1", "15/06/2025", "Expense", "", "Odd Vendor", "Purchase", "Bank", "", "1080.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_tax(store, eng)
    out = _find(findings, "rate_outliers")
    assert out is not None and out.severity == SEVERITY_WARN
    assert "Odd Vendor" in out.detail


def test_net_tax_position_is_informational(store: EngagementStore, tmp_path: Path) -> None:
    """Journal tax movement is surfaced as INFO, not compared to BS.

    Journal activity mixes current-period accruals with prior-period return
    payments, so a direct comparison to BS Payable creates false alarms.
    """
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    bs = tmp_path / "bs.xlsx"
    # Regardless of the BS Payable value, severity stays INFO.
    _write_journal(j, "2025", [
        ("1", "15/06/2025", "Expense", "", "V", "P", "Supplies", "700", ""),
        ("1", "15/06/2025", "Expense", "", "V", "P", "GST/HST Payable", "70", ""),
        ("1", "15/06/2025", "Expense", "", "V", "P", "Bank", "", "770"),
        ("2", "15/07/2025", "Invoice", "", "C", "S", "A/R", "1100", ""),
        ("2", "15/07/2025", "Invoice", "", "C", "S", "Sales", "", "1000"),
        ("2", "15/07/2025", "Invoice", "", "C", "S", "GST/HST Payable", "", "100"),
    ])
    _write_bs(bs, gst_hst_payable=500.0)
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    store.attach_document(eng.engagement_id, DOC_BALANCE_SHEET, bs)

    findings = run_tax(store, eng)
    net = _find(findings, "net_tax_position")
    assert net is not None
    assert net.severity == SEVERITY_INFO
    # The proposed-fix text must explain the timing nuance, not call it an error.
    assert "prior" in net.proposed_fix.lower() or "rollforward" in net.proposed_fix.lower()


def test_net_tax_position_works_without_bs(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, "2025", [
        ("1", "15/06/2025", "Expense", "", "V", "P", "Supplies", "700", ""),
        ("1", "15/06/2025", "Expense", "", "V", "P", "GST/HST Payable", "70", ""),
        ("1", "15/06/2025", "Expense", "", "V", "P", "Bank", "", "770"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)

    findings = run_tax(store, eng)
    net = _find(findings, "net_tax_position")
    assert net is not None
    assert net.severity == SEVERITY_INFO
