"""Parser for QuickBooks Online **Journal Detail** CSV exports.

The export Intuit produces in ``Reports → Journal → Export to CSV`` has this shape::

    Journal,,,,,,,,
    <Company Name>,,,,,,,,
    "<Period>",,,,,,,,

    ,Transaction date,Transaction type,#,Name,Description,Full name,Debit,Credit
    <group_id>,,,,,,,,
    ,<date>,<type>,<entry #>,<name>,<description>,<account>,<debit>,<credit>
    ,<date>,<type>,<entry #>,<name>,<description>,<account>,<debit>,<credit>
    Total for <group_id>,,,,,,,$<sum debit>,$<sum credit>
    <group_id>,,,,,,,,
    ...

A "group" is one journal entry (multi-line, debits must equal credits). ``entry #``
is the user-facing entry number the bookkeeper sees in QBO; ``group_id`` is the
internal QBO identifier and is what we use to tie lines back together.

Dates are in ``DD/MM/YYYY`` (Canadian). Amounts are strings with ``$``, thousand
separators, and optional parenthesized negatives. We parse into ``Decimal`` so
rounding is never a mystery in a bookkeeping context.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

HEADER_SIGNATURE = ("", "Transaction date", "Transaction type", "#")
TOTAL_PREFIX = "Total for "
GRAND_TOTAL_LABEL = "TOTAL"

# Column-name synonyms — different QBO export configs use different labels for
# the same field. The parser looks up each column by trying these names in order.
_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "date":    ("Transaction date",),
    "type":    ("Transaction type",),
    "num":     ("#", "Num"),
    "name":    ("Name",),
    "desc":    ("Description", "Memo"),
    "account": ("Account full name", "Full name", "Split"),
    "debit":   ("Debit",),
    "credit":  ("Credit",),
}

# QBO exports from Excel are typically cp1252 on Canadian/French locales — try
# UTF-8 first (covers modern exports + the BOM), fall back to cp1252.
_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

_AMOUNT_CLEAN_RE = re.compile(r"[,$\s]")
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y")


@dataclass(frozen=True)
class JournalLine:
    """One side of one journal entry line (a single debit or credit)."""

    group_id: str              # QBO internal entry group identifier
    entry_number: str          # user-facing "#" column (may be blank for non-JE types)
    txn_date: date
    txn_type: str              # "Journal Entry", "Expense", "Bill", etc.
    name: str                  # vendor / customer
    description: str           # memo
    account: str               # "Full name" column — full account path
    debit: Decimal
    credit: Decimal

    @property
    def amount_signed(self) -> Decimal:
        """Debit positive, credit negative. Convenient for running totals on a single account."""
        return self.debit - self.credit


@dataclass(frozen=True)
class JournalGroupTotals:
    """The ``Total for <group>`` row values as QBO reported them (for cross-check)."""

    group_id: str
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True)
class JournalReport:
    company: str
    period: str
    lines: list[JournalLine]
    reported_totals: dict[str, JournalGroupTotals] = field(default_factory=dict)

    def groups(self) -> dict[str, list[JournalLine]]:
        out: dict[str, list[JournalLine]] = {}
        for line in self.lines:
            out.setdefault(line.group_id, []).append(line)
        return out

    def unbalanced_groups(self) -> list[str]:
        """Groups where sum(debit) != sum(credit). Should be empty in a clean export."""
        bad: list[str] = []
        for gid, lines in self.groups().items():
            if sum(l.debit for l in lines) != sum(l.credit for l in lines):
                bad.append(gid)
        return bad


class JournalParseError(Exception):
    pass


def parse_journal_csv(path: Path | str) -> JournalReport:
    """Parse a QBO Journal Detail CSV into a ``JournalReport``.

    Raises :class:`JournalParseError` if the file doesn't look like a Journal export.
    Individual malformed data rows are logged and skipped — the parser is tolerant so
    a single garbage row doesn't fail an entire engagement's intake.
    """
    path = Path(path)
    rows = _read_csv_rows(path)
    return parse_journal_rows(rows)


def _read_csv_rows(path: Path) -> list[list[str]]:
    """Read the CSV with encoding autodetection.

    QBO exports on Canadian/French Excel default to cp1252, on newer builds to UTF-8.
    We try in order so client names with accents (é, à, ç) don't come through as mojibake.
    """
    last_err: Exception | None = None
    for enc in _ENCODINGS:
        try:
            with path.open("r", encoding=enc, newline="") as f:
                return list(csv.reader(f))
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise JournalParseError(
        f"Could not decode {path} with any of {_ENCODINGS}: {last_err}"
    )


@dataclass(frozen=True)
class _ColumnMap:
    """Position of each named column in the CSV, discovered from the header row.

    Making the parser header-driven (rather than fixed-index) lets it handle QBO
    export variants — e.g. the extra "Distribution account number" column that
    some files include between "Description" and "Account full name".
    """

    date: int
    type: int
    num: int
    name: int
    desc: int
    account: int
    debit: int
    credit: int
    width: int   # pad rows to at least this many columns


def parse_journal_rows(rows: list[list[str]]) -> JournalReport:
    if not rows:
        raise JournalParseError("empty file")

    company, period, header_idx, col = _parse_preamble(rows)
    lines: list[JournalLine] = []
    reported: dict[str, JournalGroupTotals] = {}
    current_group: str | None = None

    for row in rows[header_idx + 1:]:
        row = _pad_row(row, col.width)
        first_cell = (row[0] or "").strip()
        second_cell = (row[1] or "").strip()

        if not any(cell.strip() for cell in row):
            continue

        # Grand-total row at end of report.
        if first_cell == "" and second_cell == GRAND_TOTAL_LABEL:
            current_group = None
            continue

        if first_cell.startswith(TOTAL_PREFIX):
            gid = first_cell[len(TOTAL_PREFIX):].strip()
            try:
                reported[gid] = JournalGroupTotals(
                    group_id=gid,
                    debit=_parse_amount(row[col.debit]),
                    credit=_parse_amount(row[col.credit]),
                )
            except ValueError as e:
                log.warning("journal.bad_total_amount group=%s err=%s", gid, e)
            current_group = None
            continue

        # Group header: first cell has the group id, the rest are empty.
        if first_cell and not any(c.strip() for c in row[1:]):
            current_group = first_cell
            continue

        # Detail row: first cell empty, date in the date column.
        if first_cell == "" and second_cell:
            if current_group is None:
                log.warning("journal.detail_before_group row=%s", row)
                continue
            line = _build_line(current_group, row, col)
            if line is not None:
                lines.append(line)
            continue

        log.debug("journal.row_skipped row=%s", row)

    return JournalReport(
        company=company, period=period, lines=lines, reported_totals=reported
    )


# ---- helpers ------------------------------------------------------------------


def _parse_preamble(rows: list[list[str]]) -> tuple[str, str, int, _ColumnMap]:
    """Return (company, period, header_row_index, column_map).

    Finds the header row by signature match on the first 4 cells, then builds a
    ``_ColumnMap`` by looking up each expected logical column via ``_COLUMN_SYNONYMS``.
    """
    company = ""
    period = ""
    for i, row in enumerate(rows[:15]):
        header_cells = [str(c).strip() if c is not None else "" for c in row]
        if len(header_cells) >= 4 and tuple(header_cells[:4]) == HEADER_SIGNATURE:
            return company, period, i, _build_column_map(header_cells)
        # Row 0 = title ("Journal"); row 1 = company; row 2 = period.
        first = header_cells[0] if header_cells else ""
        if i == 1 and first:
            company = first
        elif i == 2 and first:
            period = first.strip('"')
    raise JournalParseError(
        "Could not find QBO Journal header row "
        f"(expected {HEADER_SIGNATURE!r} in first 15 rows)"
    )


def _build_column_map(header_cells: list[str]) -> _ColumnMap:
    """Resolve each logical column to its position in this particular export."""
    def find(logical: str) -> int:
        synonyms = _COLUMN_SYNONYMS[logical]
        for i, cell in enumerate(header_cells):
            if cell in synonyms:
                return i
        raise JournalParseError(
            f"Journal header is missing a column matching {logical!r} "
            f"(tried {synonyms}); got header={header_cells!r}"
        )

    return _ColumnMap(
        date=find("date"),
        type=find("type"),
        num=find("num"),
        name=find("name"),
        desc=find("desc"),
        account=find("account"),
        debit=find("debit"),
        credit=find("credit"),
        width=len(header_cells),
    )


def _pad_row(row: list[str], width: int) -> list[str]:
    if len(row) >= width:
        return row
    return list(row) + [""] * (width - len(row))


def _build_line(group_id: str, row: list[str], col: _ColumnMap) -> JournalLine | None:
    try:
        txn_date = _parse_date(row[col.date])
    except ValueError as e:
        log.warning("journal.bad_date group=%s value=%r err=%s", group_id, row[col.date], e)
        return None

    try:
        debit = _parse_amount(row[col.debit])
        credit = _parse_amount(row[col.credit])
    except ValueError as e:
        log.warning(
            "journal.bad_amount group=%s debit=%r credit=%r err=%s",
            group_id, row[col.debit], row[col.credit], e,
        )
        return None

    return JournalLine(
        group_id=group_id,
        entry_number=row[col.num].strip(),
        txn_date=txn_date,
        txn_type=row[col.type].strip(),
        name=row[col.name].strip(),
        description=_clean_description(row[col.desc]),
        account=row[col.account].strip(),
        debit=debit,
        credit=credit,
    )


def _clean_description(raw: str) -> str:
    """Collapse multi-line descriptions (QBO embeds newlines inside quoted memos) into one line."""
    return " ".join(part.strip() for part in raw.splitlines() if part.strip())


def _parse_date(raw: str) -> date:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {raw!r}")


def _parse_amount(raw: str) -> Decimal:
    """Parse QBO's money strings. Handles ``$``, thousand separators, parenthesized negatives."""
    s = (raw or "").strip()
    if not s:
        return Decimal("0")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = _AMOUNT_CLEAN_RE.sub("", s)
    if not s:
        return Decimal("0")
    try:
        value = Decimal(s)
    except Exception as e:
        raise ValueError(f"unparseable amount: {raw!r}") from e
    return -value if negative else value


def iter_accounts(report: JournalReport) -> Iterable[str]:
    """All distinct accounts touched across the report, sorted."""
    return sorted({l.account for l in report.lines if l.account})
