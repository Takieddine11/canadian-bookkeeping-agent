"""Unit tests for the QBO Balance Sheet / P&L xlsx parser."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from src.parsers.financial_statement import (
    REPORT_BALANCE_SHEET,
    REPORT_PNL,
    StatementParseError,
    parse_balance_sheet,
    parse_financial_statement,
    parse_pnl,
)


def _write_xlsx(path: Path, rows: list[tuple]) -> None:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)


@pytest.fixture
def bs_file(tmp_path: Path) -> Path:
    path = tmp_path / "bs.xlsx"
    _write_xlsx(path, [
        ("Test Client Inc.",),
        ("Balance Sheet",),
        ("As of December 31, 2025",),
        (None,),
        (None, "Total"),
        ("Assets",),
        ("   Current Assets",),
        ("      Cash",),
        ("         Scotia Checking", 12345.67),
        ("         Scotia Savings",  1000.00),
        ("      Total Cash", 13345.67),
        ("      Accounts Receivable (A/R)", 5000.00),
        ("   Total Current Assets", 18345.67),
        ("Total Assets", 18345.67),
        ("Liabilities and Equity",),
        ("   Liabilities",),
        ("      Current Liabilities",),
        ("         GST/HST Payable", 2345.67),
        ("      Total Current Liabilities", 2345.67),
        ("   Total Liabilities", 2345.67),
        ("   Equity",),
        ("      Retained Earnings", 10000.00),
        ("      Profit for the year", 6000.00),
        ("   Total Equity", 16000.00),
        ("Total Liabilities and Equity", 18345.67),
        (None,),
        ("Tuesday, Apr. 21, 2026 08:36:09 a.m. GMT-7 - Accrual Basis",),
    ])
    return path


@pytest.fixture
def pnl_file(tmp_path: Path) -> Path:
    path = tmp_path / "pnl.xlsx"
    _write_xlsx(path, [
        ("Test Client Inc.",),
        ("Profit and Loss",),
        ("January - December 2025",),
        (None,),
        (None, "Total"),
        ("   INCOME",),
        ("      Sales", 100000.00),
        ("   Total Income", 100000.00),
        ("   COST OF GOODS SOLD",),
        ("      Materials", 30000.00),
        ("   Total Cost of Goods Sold", 30000.00),
        ("GROSS PROFIT", 70000.00),
        ("EXPENSES",),
        ("   Rent", 12000.00),
        ("   Bank Charges", 500.00),
        ("Total Expenses", 12500.00),
        ("PROFIT", 57500.00),
        (None,),
        ("Tuesday, Apr. 21, 2026 08:35:34 a.m. GMT-7 - Cash Basis",),
    ])
    return path


def test_bs_metadata(bs_file: Path) -> None:
    bs = parse_balance_sheet(bs_file)
    assert bs.company == "Test Client Inc."
    assert bs.report_type == REPORT_BALANCE_SHEET
    assert bs.as_of == date(2025, 12, 31)
    assert bs.basis == "Accrual"


def test_pnl_metadata(pnl_file: Path) -> None:
    pl = parse_pnl(pnl_file)
    assert pl.company == "Test Client Inc."
    assert pl.report_type == REPORT_PNL
    # Full-year period → as_of = Dec 31 of that year.
    assert pl.as_of == date(2025, 12, 31)
    assert pl.basis == "Cash"


def test_bs_amounts_extractable_by_name(bs_file: Path) -> None:
    bs = parse_balance_sheet(bs_file)
    assert bs.amount_of("Total Assets") == Decimal("18345.67")
    assert bs.amount_of("Total Liabilities") == Decimal("2345.67")
    assert bs.amount_of("Total Equity") == Decimal("16000")
    assert bs.amount_of("GST/HST Payable") == Decimal("2345.67")
    assert bs.amount_of("Scotia Checking") == Decimal("12345.67")


def test_pnl_amounts(pnl_file: Path) -> None:
    pl = parse_pnl(pnl_file)
    assert pl.amount_of("Total Income") == Decimal("100000")
    assert pl.amount_of("GROSS PROFIT") == Decimal("70000")
    assert pl.amount_of("PROFIT") == Decimal("57500")


def test_bs_accounting_identity(bs_file: Path) -> None:
    """Assets = Liabilities + Equity holds in the fixture."""
    bs = parse_balance_sheet(bs_file)
    assert bs.amount_of("Total Assets") == bs.amount_of("Total Liabilities and Equity")


def test_section_vs_total_flagging(bs_file: Path) -> None:
    bs = parse_balance_sheet(bs_file)
    assets_header = bs.find("Assets")
    assert assets_header is not None
    assert assets_header.is_section is True
    assert assets_header.is_total is False
    assert assets_header.amount is None

    total_assets = bs.find("Total Assets")
    assert total_assets.is_total is True
    assert total_assets.is_section is False


def test_level_from_indent(bs_file: Path) -> None:
    bs = parse_balance_sheet(bs_file)
    assert bs.find("Assets").level == 0
    assert bs.find("Current Assets").level == 1
    assert bs.find("Cash").level == 2
    assert bs.find("Scotia Checking").level == 3


def test_footer_row_skipped(bs_file: Path) -> None:
    bs = parse_balance_sheet(bs_file)
    # The "Accrual Basis" timestamp row must not appear as a line.
    assert all("Accrual Basis" not in l.name for l in bs.lines)


def test_auto_detect_report_type(bs_file: Path, pnl_file: Path) -> None:
    assert parse_financial_statement(bs_file).report_type == REPORT_BALANCE_SHEET
    assert parse_financial_statement(pnl_file).report_type == REPORT_PNL


def test_wrong_wrapper_raises(bs_file: Path, pnl_file: Path) -> None:
    with pytest.raises(StatementParseError):
        parse_pnl(bs_file)
    with pytest.raises(StatementParseError):
        parse_balance_sheet(pnl_file)


def test_french_balance_sheet_title_recognized(tmp_path: Path) -> None:
    """FR QBO exports title the balance sheet as 'Bilan' or 'État de situation financière'."""
    path = tmp_path / "bilan.xlsx"
    _write_xlsx(path, [
        ("Company Inc.",),
        ("Bilan",),
        ("Au 31 décembre, 2025",),
        (None,),
        (None, "Total"),
        ("Actif",),
        ("Total de l'actif", 100.0),
        ("Passif et capitaux propres",),
        ("Total du passif et des capitaux propres", 100.0),
        ("Mardi, 21 avr. 2026 - Base de caisse",),
    ])
    fs = parse_financial_statement(path)
    assert fs.report_type == REPORT_BALANCE_SHEET
    # amount_of_any should pick up FR labels.
    from src.parsers import labels as L
    assert fs.amount_of_any(*L.TOTAL_ASSETS) == Decimal("100")


def test_french_pnl_title_recognized(tmp_path: Path) -> None:
    path = tmp_path / "resultats.xlsx"
    _write_xlsx(path, [
        ("Company Inc.",),
        ("État des résultats",),
        ("Du 1 janvier au 31 décembre 2025",),
        (None,),
        (None, "Total"),
        ("   REVENUS",),
        ("      Ventes", 1000.0),
        ("   Total des revenus", 1000.0),
        ("BÉNÉFICE", 200.0),
    ])
    fs = parse_financial_statement(path)
    assert fs.report_type == REPORT_PNL
    from src.parsers import labels as L
    assert fs.amount_of_any(*L.TOTAL_INCOME) == Decimal("1000")
    assert fs.amount_of_any(*L.NET_PROFIT) == Decimal("200")


def test_unknown_report_title_raises(tmp_path: Path) -> None:
    path = tmp_path / "weird.xlsx"
    _write_xlsx(path, [
        ("Company",),
        ("Cash Flow",),
        ("As of Dec 31, 2025",),
        (None,),
        (None, "Total"),
    ])
    with pytest.raises(StatementParseError):
        parse_financial_statement(path)


def test_empty_workbook_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.xlsx"
    wb = Workbook()
    wb.save(path)
    # Default sheet has a single empty row; classifier will fail on title.
    with pytest.raises(StatementParseError):
        parse_financial_statement(path)


def test_bilingual_slash_label_resolves_either_language() -> None:
    """QBO FR PDFs often render accounts as 'English / French' bilingual
    strings. ``find`` must match on either segment so label lookups work
    regardless of which language a caller passes."""
    from src.parsers.financial_statement import FinancialStatement, StatementLine
    fs = FinancialStatement(
        company="Co.", report_type="balance_sheet", report_title="Bilan",
        period_label="", as_of=None, basis="",
        lines=[
            StatementLine(
                name="Retained Earnings / Bénéfices non répartis",
                level=3, amount=Decimal("-8468"), is_total=False, is_section=False,
            ),
        ],
    )
    # English side
    assert fs.amount_of("Retained Earnings") == Decimal("-8468")
    # French side
    assert fs.amount_of("Bénéfices non répartis") == Decimal("-8468")


@pytest.mark.parametrize("raw,expected", [
    # English variants
    ("$1,234.56",    Decimal("1234.56")),
    ("(1,234.56)",   Decimal("-1234.56")),
    ("12,345.67",    Decimal("12345.67")),
    # QBO French PDF variants (comma decimal, optional trailing $)
    ("-12,63",       Decimal("-12.63")),
    ("12,63$",       Decimal("12.63")),
    ("86946,39$",    Decimal("86946.39")),
    ("1 234,56",     Decimal("1234.56")),    # space thousands
    ("1.234,56",     Decimal("1234.56")),    # dot thousands (rarer but valid)
])
def test_coerce_amount_bilingual(raw: str, expected: Decimal) -> None:
    from src.parsers.financial_statement import _coerce_amount
    assert _coerce_amount(raw) == expected


def test_amount_like_regex_accepts_fr_formats() -> None:
    from src.parsers.financial_statement import _looks_like_amount
    assert _looks_like_amount("-12,63")
    assert _looks_like_amount("86946,39$")
    assert _looks_like_amount("1,234.56")
    assert _looks_like_amount("(1,234.56)")
    # Non-amounts
    assert not _looks_like_amount("Bilan")
    assert not _looks_like_amount("TOTAL")
    assert not _looks_like_amount("1/1")


def test_cash_account_is_not_mistaken_for_cash_basis() -> None:
    """A plain 'Cash' line in the account tree used to trip the basis
    regex and drop the row entirely. Regression: must survive as a line."""
    from src.parsers.financial_statement import _build_line
    ln = _build_line(("      Cash",))
    assert ln is not None
    assert ln.name == "Cash"


def test_fr_period_au_format_parses() -> None:
    from src.parsers.financial_statement import _parse_as_of
    assert _parse_as_of("Au 31 décembre 2025") == date(2025, 12, 31)
    assert _parse_as_of("En date du 31 déc., 2025") == date(2025, 12, 31)


def test_fr_pnl_period_without_spaces_parses() -> None:
    """QBO FR exports use 'janvier-décembre, 2025' (no spaces around the hyphen)."""
    from src.parsers.financial_statement import _parse_as_of
    assert _parse_as_of("janvier-décembre, 2025") == date(2025, 12, 31)
