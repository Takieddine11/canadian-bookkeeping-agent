"""Unit tests for the Government Remittance Auditor agent."""

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
from src.agents.government_remittance import AGENT, run as run_gov
from src.store.engagement_db import (
    CONV_PERSONAL,
    DOC_BALANCE_SHEET,
    DOC_JOURNAL,
    EngagementStore,
)


def _write_journal(path: Path, rows: list[tuple]) -> None:
    """rows = (group_id, date, type, num, name, desc, account, debit, credit)."""
    out: list[str] = [
        "Journal,,,,,,,,",
        "Test,,,,,,,,",
        '"2025",,,,,,,,',
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
        def q(s):  # quote strings with commas
            return f'"{s}"' if isinstance(s, str) and "," in s else str(s)
        out.append(
            f",{d},{t},{n},{q(nm)},{q(desc)},{q(acct)},"
            f"{dr or ''},{cr or ''}"
        )
    if current is not None:
        td, tc = totals[current]
        out.append(f"Total for {current},,,,,,,${td:.2f},${tc:.2f}")
    path.write_text("\n".join(out), encoding="utf-8")


def _write_bs(path: Path, *, payroll_liab: float = 0.0,
              gst_qst_payable: float = 0.0,
              corp_tax_payable: float = 0.0) -> None:
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
        ("         Payroll Liabilities - DAS", payroll_liab),
        ("         GST/HST Payable", gst_qst_payable),
        ("         Corporate Income Tax Payable", corp_tax_payable),
        ("      Total Current Liabilities", payroll_liab + gst_qst_payable + corp_tax_payable),
        ("   Total Liabilities", payroll_liab + gst_qst_payable + corp_tax_payable),
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
    findings = run_gov(store, eng)
    jp = _find(findings, "journal_present")
    assert jp is not None and jp.severity == SEVERITY_ERROR


def test_no_gov_payments_in_journal(store: EngagementStore, tmp_path: Path) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, [
        ("1", "15/06/2025", "Expense", "", "Regular Vendor", "Office supplies",
         "Office Supplies", "100.00", ""),
        ("1", "15/06/2025", "Expense", "", "Regular Vendor", "Payment",
         "BMO Chequing", "", "100.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_gov(store, eng)
    no_pay = _find(findings, "no_gov_payments")
    assert no_pay is not None and no_pay.severity == SEVERITY_INFO


def test_classification_summary_counts_each_category(
    store: EngagementStore, tmp_path: Path
) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, [
        # Payroll DAS remittance — clears the payroll liability.
        ("1", "05/03/2025", "Expense", "", "Receiver General",
         "Monthly payroll source deductions - CPP EI fed tax",
         "Payroll Liabilities - DAS", "1200.00", ""),
        ("1", "05/03/2025", "Expense", "", "Receiver General", "",
         "BMO Chequing", "", "1200.00"),
        # GST/HST remittance — clears GST/HST Payable.
        ("2", "15/04/2025", "Expense", "", "Receiver General",
         "GST/HST Q1 remittance",
         "GST/HST Payable", "800.00", ""),
        ("2", "15/04/2025", "Expense", "", "Receiver General", "",
         "BMO Chequing", "", "800.00"),
        # Corporate tax installment.
        ("3", "20/06/2025", "Expense", "", "Canada Revenue Agency",
         "T2 installment #2 FY2025",
         "Corporate Income Tax Payable", "5000.00", ""),
        ("3", "20/06/2025", "Expense", "", "Canada Revenue Agency", "",
         "BMO Chequing", "", "5000.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_gov(store, eng)
    summary = _find(findings, "classification_summary")
    assert summary is not None
    assert "Payroll source deductions" in summary.detail
    assert "GST/HST remittance" in summary.detail
    assert "Corporate income tax" in summary.detail
    # Total should be 1,200 + 800 + 5,000 = 7,000.
    assert "7,000" in summary.title


def test_expense_miscoding_flags_remittance_posted_to_expense(
    store: EngagementStore, tmp_path: Path
) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, [
        # WRONG — CRA payroll remittance debited to Professional Fees instead
        # of Payroll Liabilities.
        ("1", "12/05/2025", "Expense", "", "Receiver General",
         "Monthly payroll source deductions",
         "Professional Fees", "6840.00", ""),
        ("1", "12/05/2025", "Expense", "", "Receiver General", "",
         "BMO Chequing", "", "6840.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_gov(store, eng)
    miscode = _find(findings, "expense_miscoding")
    assert miscode is not None
    assert miscode.severity == SEVERITY_ERROR
    assert "6,840" in miscode.title
    # The per-item detail must include the date, vendor, and amount so the
    # bookkeeper can find the entry in QBO.
    assert "2025-05-12" in miscode.detail
    assert "Receiver General" in miscode.detail
    assert "6,840" in miscode.detail


def test_expense_miscoding_ok_when_liability_account_used(
    store: EngagementStore, tmp_path: Path
) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, [
        ("1", "12/05/2025", "Expense", "", "Receiver General",
         "Payroll DAS", "Payroll Liabilities", "6840.00", ""),
        ("1", "12/05/2025", "Expense", "", "Receiver General", "",
         "BMO Chequing", "", "6840.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_gov(store, eng)
    miscode = _find(findings, "expense_miscoding")
    assert miscode is not None
    assert miscode.severity == SEVERITY_OK


def test_prior_year_noise_flags_jan_feb_material_sales_tax(
    store: EngagementStore, tmp_path: Path
) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, [
        # $3,500 GST remittance on Jan 31 = typical prior-year Q4 balance due.
        ("1", "31/01/2025", "Expense", "", "Revenu Quebec",
         "QST Q4 2024 remittance",
         "QST Payable", "3500.00", ""),
        ("1", "31/01/2025", "Expense", "", "Revenu Quebec", "",
         "BMO Chequing", "", "3500.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_gov(store, eng)
    noise = _find(findings, "prior_year_noise")
    assert noise is not None
    assert noise.severity == SEVERITY_WARN
    assert "2025-01-31" in noise.detail
    assert "Revenu Quebec" in noise.detail


def test_unclassified_government_payment(
    store: EngagementStore, tmp_path: Path
) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, [
        # A CRA payment with a blank memo and no obvious classification.
        ("1", "15/07/2025", "Expense", "", "Receiver General",
         "", "Taxes Payable", "2500.00", ""),
        ("1", "15/07/2025", "Expense", "", "Receiver General", "",
         "BMO Chequing", "", "2500.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_gov(store, eng)
    orphan = _find(findings, "unclassified_payments")
    assert orphan is not None
    assert orphan.severity == SEVERITY_WARN
    assert "2,500" in orphan.title


def test_bs_reconciliation_flags_zero_installments_with_material_payable(
    store: EngagementStore, tmp_path: Path
) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    # Only a payroll DAS remittance — no corporate-tax installments.
    _write_journal(j, [
        ("1", "05/03/2025", "Expense", "", "Receiver General",
         "Payroll DAS", "Payroll Liabilities", "1000.00", ""),
        ("1", "05/03/2025", "Expense", "", "Receiver General", "",
         "BMO Chequing", "", "1000.00"),
    ])
    bs = tmp_path / "bs.xlsx"
    _write_bs(bs, corp_tax_payable=15000.0)
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    store.attach_document(eng.engagement_id, DOC_BALANCE_SHEET, bs)
    findings = run_gov(store, eng)
    recon = _find(findings, "bs_reconciliation")
    assert recon is not None
    assert recon.severity == SEVERITY_WARN
    assert "Zero corporate-tax installments" in recon.detail
    assert "15,000" in recon.detail


def test_french_payee_quebec_classified_as_qst(
    store: EngagementStore, tmp_path: Path
) -> None:
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, [
        # FR memo + RQ payee should classify as QST remittance.
        ("1", "15/04/2025", "Expense", "", "Revenu Quebec",
         "Remise TVQ Q1 2025", "QST Payable", "1200.00", ""),
        ("1", "15/04/2025", "Expense", "", "Revenu Quebec", "",
         "BMO Chequing", "", "1200.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_gov(store, eng)
    summary = _find(findings, "classification_summary")
    assert summary is not None
    assert "QST remittance" in summary.detail


def test_single_leg_sales_tax_remittance_flagged(
    store: EngagementStore, tmp_path: Path
) -> None:
    """A material QC sales-tax remittance posted to ONE sub-account (all to
    GST/HST Payable, nothing to QST Payable) must be flagged for splitting."""
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, [
        # $8,200 to Revenu Quebec for a Q3 GST+QST return, posted entirely to
        # GST/HST Payable — no QST Payable leg. This is the Lefebvre pattern.
        ("1", "15/10/2025", "Expense", "", "Revenu Quebec",
         "GST/QST Q3 2025 return payment",
         "GST/HST Payable", "8200.00", ""),
        ("1", "15/10/2025", "Expense", "", "Revenu Quebec", "",
         "BMO Chequing", "", "8200.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_gov(store, eng)
    single = _find(findings, "single_leg_sales_tax_remittance")
    assert single is not None
    assert single.severity == SEVERITY_WARN
    assert "8,200" in single.title
    # Must include date + vendor + amount per-item so bookkeeper can search QBO.
    assert "2025-10-15" in single.detail
    assert "Revenu Quebec" in single.detail


def test_sales_tax_remittance_with_both_legs_not_flagged(
    store: EngagementStore, tmp_path: Path
) -> None:
    """When a remittance correctly splits across GST/HST Payable AND QST
    Payable sub-accounts, it must NOT be flagged."""
    eng = store.create_engagement("c1", CONV_PERSONAL, period_description="2025")
    j = tmp_path / "j.csv"
    _write_journal(j, [
        # Properly split: GST portion + QST portion.
        ("1", "15/10/2025", "Expense", "", "Revenu Quebec",
         "GST/QST Q3 2025 return — GST portion",
         "GST/HST Payable", "2700.00", ""),
        ("1", "15/10/2025", "Expense", "", "Revenu Quebec",
         "GST/QST Q3 2025 return — QST portion",
         "QST Payable", "5500.00", ""),
        ("1", "15/10/2025", "Expense", "", "Revenu Quebec", "",
         "BMO Chequing", "", "8200.00"),
    ])
    store.attach_document(eng.engagement_id, DOC_JOURNAL, j)
    findings = run_gov(store, eng)
    single = _find(findings, "single_leg_sales_tax_remittance")
    assert single is None  # correctly split → no finding
