"""Unit tests for the QBO Journal Detail CSV parser."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.parsers.journal import (
    JournalParseError,
    _parse_amount,
    iter_accounts,
    parse_journal_csv,
    parse_journal_rows,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "journal_sample.csv"


def test_parses_preamble() -> None:
    r = parse_journal_csv(FIXTURE)
    assert r.company == "Test Client Inc."
    assert r.period == "January-March, 2026"


def test_extracts_all_detail_lines() -> None:
    r = parse_journal_csv(FIXTURE)
    # 2 lines in group 324 + 4 lines in 132 + 4 lines in 473 = 10
    assert len(r.lines) == 10


def test_groups_lines_by_group_id() -> None:
    r = parse_journal_csv(FIXTURE)
    g = r.groups()
    assert set(g) == {"324", "132", "473"}
    assert len(g["324"]) == 2
    assert len(g["132"]) == 4
    assert len(g["473"]) == 4


def test_every_group_balances() -> None:
    r = parse_journal_csv(FIXTURE)
    assert r.unbalanced_groups() == []


def test_reported_totals_captured() -> None:
    r = parse_journal_csv(FIXTURE)
    assert r.reported_totals["132"].debit == Decimal("1025")
    assert r.reported_totals["132"].credit == Decimal("1025")


def test_reported_totals_match_computed() -> None:
    r = parse_journal_csv(FIXTURE)
    for gid, lines in r.groups().items():
        computed_debit = sum((l.debit for l in lines), Decimal("0"))
        computed_credit = sum((l.credit for l in lines), Decimal("0"))
        assert computed_debit == r.reported_totals[gid].debit
        assert computed_credit == r.reported_totals[gid].credit


def test_date_parsed_dd_mm_yyyy() -> None:
    r = parse_journal_csv(FIXTURE)
    jan_rent = r.groups()["324"][0]
    assert jan_rent.txn_date == date(2026, 1, 1)


def test_multiline_description_collapsed() -> None:
    r = parse_journal_csv(FIXTURE)
    paiement_lines = [l for l in r.groups()["132"] if "PAIEMENT" in l.description]
    assert len(paiement_lines) == 1
    # Newline inside the quoted memo should be collapsed to a single space.
    assert "\n" not in paiement_lines[0].description
    assert "REVENU QUEBEC" in paiement_lines[0].description


def test_different_transaction_types() -> None:
    r = parse_journal_csv(FIXTURE)
    types = {l.txn_type for l in r.lines}
    assert types == {"Journal Entry", "Expense"}


def test_amount_with_comma_parsed() -> None:
    r = parse_journal_csv(FIXTURE)
    paiement = [l for l in r.groups()["132"] if "PAIEMENT" in l.description][0]
    assert paiement.debit == Decimal("1010")  # parsed from "1,010.00"
    assert paiement.credit == Decimal("0")


def test_account_listing() -> None:
    r = parse_journal_csv(FIXTURE)
    accounts = list(iter_accounts(r))
    assert "Building Rent" in accounts
    assert "GST/HST Payable" in accounts
    assert "Finance Cost:Bank Charges" in accounts  # nested account preserved


@pytest.mark.parametrize("raw,expected", [
    ("330.00", Decimal("330")),
    ("$330.00", Decimal("330")),
    ("1,034.78", Decimal("1034.78")),
    ("$1,034.78", Decimal("1034.78")),
    ("", Decimal("0")),
    (" ", Decimal("0")),
    ("(25.00)", Decimal("-25")),
    ("$-15.00", Decimal("-15")),
])
def test_parse_amount(raw: str, expected: Decimal) -> None:
    assert _parse_amount(raw) == expected


def test_missing_header_raises() -> None:
    rows = [
        ["some", "random", "file"],
        ["with", "no", "QBO", "header"],
    ]
    with pytest.raises(JournalParseError):
        parse_journal_rows(rows)


def test_empty_file_raises() -> None:
    with pytest.raises(JournalParseError):
        parse_journal_rows([])


def test_bad_date_row_is_skipped_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    rows = [
        ["Journal", "", "", "", "", "", "", "", ""],
        ["Test", "", "", "", "", "", "", "", ""],
        ["Period", "", "", "", "", "", "", "", ""],
        ["", "", "", "", "", "", "", "", ""],
        ["", "Transaction date", "Transaction type", "#", "Name", "Description", "Full name", "Debit", "Credit"],
        ["100", "", "", "", "", "", "", "", ""],
        ["", "NOT A DATE", "Journal Entry", "1", "", "Desc", "Account", "10.00", ""],
        ["", "01/01/2026", "Journal Entry", "1", "", "Desc", "Account", "", "10.00"],
        ["Total for 100", "", "", "", "", "", "", "$10.00", "$10.00"],
    ]
    report = parse_journal_rows(rows)
    # Bad date dropped, good row kept.
    assert len(report.lines) == 1
    assert any("journal.bad_date" in rec.message for rec in caplog.records)
