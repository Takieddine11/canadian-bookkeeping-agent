"""Parser for QBO **Balance Sheet** and **Profit and Loss** Excel exports.

Both reports share the same vertical-tree shape::

    Row 1: <Company>
    Row 2: <Report type>                 (e.g., "Balance Sheet", "Profit and Loss")
    Row 3: <Period>                      (e.g., "As of December 31, 2025",
                                                "January - December 2025")
    Row 4: <blank>
    Row 5:         | Total | <period columns if multi-period>
    ...
    Row N: <Section Header>              (no value, all-caps or Title-cased)
    Row N+1: ...  <Account>              (3-space indent per level; leaf = value)
    Row N+2: ...  Total <Section>        (rollup line, has a value)
    ...
    Row M: Tuesday, Apr. 21, 2026 ... - Accrual Basis

Indentation in column A is the only hierarchy signal QBO exposes — 3 spaces per level.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
from openpyxl import load_workbook

log = logging.getLogger(__name__)

INDENT_PER_LEVEL = 3
_BASIS_RE = re.compile(r"\b(Accrual|Cash)\s*Basis\b", re.IGNORECASE)
_AS_OF_RE = re.compile(r"As\s*of\s+(.+)", re.IGNORECASE)
_PERIOD_DATE_FORMATS = (
    "%B %d, %Y",     # "December 31, 2025"
    "%b %d, %Y",     # "Dec 31, 2025"
    "%Y-%m-%d",
)
REPORT_BALANCE_SHEET = "balance_sheet"
REPORT_PNL = "pnl"


@dataclass(frozen=True)
class StatementLine:
    name: str
    level: int
    amount: Decimal | None
    is_total: bool
    is_section: bool  # header row with no amount (e.g., "Assets", "EXPENSES")


@dataclass(frozen=True)
class FinancialStatement:
    company: str
    report_type: str        # "balance_sheet" | "pnl"
    report_title: str       # raw title from row 2
    period_label: str
    as_of: date | None
    basis: str              # "Accrual" | "Cash" | ""
    lines: list[StatementLine]

    def find(self, name: str) -> StatementLine | None:
        """First line whose trimmed name matches (case-insensitive)."""
        target = name.strip().lower()
        for line in self.lines:
            if line.name.lower() == target:
                return line
        return None

    def amount_of(self, name: str) -> Decimal | None:
        """Convenience: amount of the first matching line, or None."""
        line = self.find(name)
        return line.amount if line else None

    def amount_of_any(self, *names: str) -> Decimal | None:
        """Try each name in order; return the first matching line's amount.

        Used to accept English-or-French labels without branching in every agent.
        E.g. ``bs.amount_of_any("Total Assets", "Total de l'actif")``.
        """
        for n in names:
            v = self.amount_of(n)
            if v is not None:
                return v
        return None


class StatementParseError(Exception):
    pass


def parse_balance_sheet(path: Path | str) -> FinancialStatement:
    fs = parse_financial_statement(path)
    if fs.report_type != REPORT_BALANCE_SHEET:
        raise StatementParseError(
            f"Not a Balance Sheet (got report_title={fs.report_title!r})"
        )
    return fs


def parse_pnl(path: Path | str) -> FinancialStatement:
    fs = parse_financial_statement(path)
    if fs.report_type != REPORT_PNL:
        raise StatementParseError(f"Not a P&L (got report_title={fs.report_title!r})")
    return fs


def parse_financial_statement(path: Path | str) -> FinancialStatement:
    """Auto-detect Balance Sheet vs P&L from the file's own title row.

    Routes to the xlsx or PDF backend based on file extension.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext in {".xlsx", ".xlsm"}:
        return _parse_statement_xlsx(p)
    if ext == ".pdf":
        return _parse_statement_pdf(p)
    raise StatementParseError(f"Unsupported file extension for financial statement: {ext}")


# ---- xlsx backend -------------------------------------------------------------


def _parse_statement_xlsx(path: Path) -> FinancialStatement:
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb.active
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    if not rows:
        raise StatementParseError(f"empty workbook: {path}")

    company = _first_str(rows, 0)
    title = _first_str(rows, 1)
    period_label = _first_str(rows, 2)
    report_type = _classify_report(title)

    as_of = _parse_as_of(period_label)
    basis = _find_basis(rows)

    data_start = _find_data_start(rows)
    lines = [ln for ln in (_build_line(r) for r in rows[data_start:]) if ln is not None]

    return FinancialStatement(
        company=company,
        report_type=report_type,
        report_title=title,
        period_label=period_label,
        as_of=as_of,
        basis=basis,
        lines=lines,
    )


# ---- PDF backend --------------------------------------------------------------

# QBO PDF exports use a right-aligned amount column at roughly x0 > 500 and a
# left account-name column starting at x0 ~= 28. Indentation steps are about
# 3pt per level. We snap to the nearest level using the narrowest observed
# ``x0`` as "level 0".
_PDF_AMOUNT_X_THRESHOLD = 400
_PDF_LEVEL_STEP = 3


def _parse_statement_pdf(path: Path) -> FinancialStatement:
    with pdfplumber.open(path) as pdf:
        all_lines: list[_PdfLine] = []
        for page in pdf.pages:
            words = page.extract_words()
            all_lines.extend(_group_words_into_lines(words))

    if not all_lines:
        raise StatementParseError(f"no text extracted from PDF: {path}")

    # Preamble: first 3 non-empty lines with no amount = company, title, period.
    company = title = period_label = ""
    header_lines: list[_PdfLine] = []
    for ln in all_lines:
        if ln.amount is None and len(header_lines) < 3:
            header_lines.append(ln)
        else:
            break
    if len(header_lines) >= 1:
        company = header_lines[0].name
    if len(header_lines) >= 2:
        title = header_lines[1].name
    if len(header_lines) >= 3:
        period_label = header_lines[2].name

    report_type = _classify_report(title)
    as_of = _parse_as_of(period_label)

    # Footer with basis (and the "TOTAL" column header) are non-data lines to skip.
    body = all_lines[len(header_lines):]
    basis = ""
    cleaned: list[_PdfLine] = []
    for ln in body:
        if _BASIS_RE.search(ln.name):
            m = _BASIS_RE.search(ln.name)
            if m and not basis:
                basis = m.group(1).capitalize()
            continue
        if ln.name.strip().upper() == "TOTAL" and ln.amount is None:
            continue  # column header
        cleaned.append(ln)

    if not cleaned:
        raise StatementParseError(f"no data lines in PDF: {path}")

    min_x = min(ln.x0 for ln in cleaned)
    lines: list[StatementLine] = []
    for ln in cleaned:
        level = max(0, round((ln.x0 - min_x) / _PDF_LEVEL_STEP))
        name = ln.name.strip()
        if not name:
            continue
        is_total = name.lower().startswith("total ") or name.lower() == "total"
        is_section = ln.amount is None and not is_total
        lines.append(StatementLine(
            name=name, level=level, amount=ln.amount,
            is_total=is_total, is_section=is_section,
        ))

    return FinancialStatement(
        company=company,
        report_type=report_type,
        report_title=title,
        period_label=period_label,
        as_of=as_of,
        basis=basis,
        lines=lines,
    )


@dataclass(frozen=True)
class _PdfLine:
    """One reconstructed row from a PDF: name (left) + optional amount (right)."""

    name: str
    amount: Decimal | None
    x0: float  # left-most x of the name; used to infer indent level


def _group_words_into_lines(words: list[dict]) -> list[_PdfLine]:
    """Cluster pdfplumber words by y-coordinate into lines, splitting name/amount by x."""
    if not words:
        return []

    buckets: dict[int, list[dict]] = defaultdict(list)
    for w in words:
        buckets[round(w["top"])].append(w)

    out: list[_PdfLine] = []
    for y in sorted(buckets):
        sorted_words = sorted(buckets[y], key=lambda w: w["x0"])
        name_parts: list[str] = []
        amount: Decimal | None = None
        for w in sorted_words:
            if w["x0"] >= _PDF_AMOUNT_X_THRESHOLD and _looks_like_amount(w["text"]):
                amount = _coerce_amount(w["text"])
                break  # amount is always the last column
            name_parts.append(w["text"])
        if not name_parts and amount is None:
            continue
        name = " ".join(name_parts)
        x0 = sorted_words[0]["x0"]
        out.append(_PdfLine(name=name, amount=amount, x0=x0))
    return out


_AMOUNT_LIKE_RE = re.compile(r"^-?\$?-?[0-9,]+\.\d{2}$|^\([0-9,]+\.\d{2}\)$")


def _looks_like_amount(s: str) -> bool:
    return bool(_AMOUNT_LIKE_RE.match(s.strip()))


def _first_str(rows: list[tuple], idx: int) -> str:
    if idx >= len(rows):
        return ""
    cell = rows[idx][0] if rows[idx] else None
    return str(cell).strip() if cell is not None else ""


def _classify_report(title: str) -> str:
    """Detect Balance Sheet vs P&L from the title row. Accepts English and French
    QBO labels — Quebec bookkeepers commonly export in French."""
    t = title.lower()
    # --- Balance Sheet ---
    bs_markers = (
        "balance sheet",
        "bilan",
        "situation financière", "situation financiere",
        "état de situation",  "etat de situation",
    )
    if any(m in t for m in bs_markers):
        return REPORT_BALANCE_SHEET
    # --- P&L ---
    pnl_markers = (
        "profit and loss", "income statement",
        "résultat", "resultat",
        "état des résultats", "etat des resultats",
        "état du résultat", "etat du resultat",
        "profits et pertes", "pertes et profits",
    )
    if any(m in t for m in pnl_markers):
        return REPORT_PNL
    raise StatementParseError(f"Unknown report type in title: {title!r}")


def _parse_as_of(period_label: str) -> date | None:
    m = _AS_OF_RE.match(period_label)
    if m:
        raw = m.group(1).strip().rstrip(".,")
        for fmt in _PERIOD_DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    # P&L period like "January - December 2025": return Dec 31 of that year as as_of.
    year_match = re.search(r"\b(\d{4})\b", period_label)
    if year_match and " - " in period_label:
        return date(int(year_match.group(1)), 12, 31)
    return None


def _find_basis(rows: list[tuple]) -> str:
    for row in rows[-15:]:  # basis is always in the footer
        for cell in row:
            if isinstance(cell, str):
                m = _BASIS_RE.search(cell)
                if m:
                    return m.group(1).capitalize()
    return ""


def _find_data_start(rows: list[tuple]) -> int:
    """Return the row index after the column-header row (the one with 'Total')."""
    for i, row in enumerate(rows[:10]):
        if row and any(isinstance(c, str) and c.strip() == "Total" for c in row[1:]):
            return i + 1
    # fallback: skip the 3 preamble rows + 1 blank
    return 4


def _build_line(row: tuple) -> StatementLine | None:
    if not row or row[0] is None:
        return None
    name_raw = row[0]
    if not isinstance(name_raw, str):
        return None

    # Footer row (timestamp + basis) is text with a weekday/month. Skip it.
    if _BASIS_RE.search(name_raw):
        return None

    indent = len(name_raw) - len(name_raw.lstrip(" "))
    level = indent // INDENT_PER_LEVEL
    name = name_raw.strip()
    if not name:
        return None

    amount = _coerce_amount(row[1] if len(row) > 1 else None)
    is_total = name.lower().startswith("total ") or name.lower() == "total"
    is_section = amount is None and not is_total

    return StatementLine(
        name=name, level=level, amount=amount, is_total=is_total, is_section=is_section
    )


def _coerce_amount(cell) -> Decimal | None:  # noqa: ANN001 — openpyxl cell value
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        return Decimal(str(cell))
    if isinstance(cell, Decimal):
        return cell
    if isinstance(cell, str):
        s = cell.strip()
        if not s:
            return None
        negative = s.startswith("(") and s.endswith(")")
        if negative:
            s = s[1:-1]
        s = s.replace("$", "").replace(",", "").replace(" ", "")
        try:
            value = Decimal(s)
            return -value if negative else value
        except Exception:
            return None
    return None
