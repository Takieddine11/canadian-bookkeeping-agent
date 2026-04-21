"""Agent 3 — Reconciliation (deterministic v1).

This first cut runs pure-arithmetic / pattern checks on the journal. Bank matching
and description/account coding-sense checks need bank statements or an LLM and
are deferred to a later pass.

Checks performed:

1. **Duplicate lines** — same date, account, absolute amount, and normalized memo.
   Classic double-entry mistake from importing statements or copy/paste journals.
2. **Missing account coding** — lines where ``account`` is blank. These go to
   "Uncategorized" or "Ask My Accountant" in QBO and should be resolved before sign-off.
3. **Out-of-period entries** — transactions dated outside the engagement's period.
   Often bookkeeper error (wrong year) or period-closing drift.
4. **Monthly activity breakdown** — shape of the year; useful context for the CPA
   reviewer (seasonality, gaps) and for tax-auditor sanity checks later.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.agents.base import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_OK,
    SEVERITY_WARN,
    Finding,
)
from src.parsers.journal import JournalLine, JournalReport, parse_journal_csv
from src.store.engagement_db import DOC_JOURNAL, Engagement, EngagementStore

log = logging.getLogger(__name__)
AGENT = "reconciliation"
_ZERO = Decimal("0")

_WS_RE = re.compile(r"\s+")
_NONWORD_RE = re.compile(r"[^a-z0-9 ]+")


@dataclass(frozen=True)
class _DupKey:
    txn_date: date
    account: str
    amount: Decimal
    memo_norm: str


def run(store: EngagementStore, engagement: Engagement) -> list[Finding]:
    """Run reconciliation checks. Returns empty list if no journal is uploaded."""
    docs = store.list_documents(engagement.engagement_id)
    journal_doc = next((d for d in docs if d.doc_type == DOC_JOURNAL), None)
    if journal_doc is None:
        return [Finding(
            agent=AGENT, check="journal_present", severity=SEVERITY_ERROR,
            title="Journal not uploaded",
            detail="Reconciliation needs the QBO Journal Detail export.",
        )]

    try:
        report = parse_journal_csv(Path(journal_doc.file_path))
    except Exception as exc:
        log.exception("reconciliation.parse_failed path=%s", journal_doc.file_path)
        return [Finding(
            agent=AGENT, check="journal_parse", severity=SEVERITY_ERROR,
            title="Could not parse the journal",
            detail=f"{type(exc).__name__}: {exc}",
        )]

    findings: list[Finding] = []
    findings.extend(_duplicates(report))
    findings.extend(_missing_account(report))
    findings.extend(_out_of_period(report, engagement.period_description))
    findings.extend(_interac_deposits(report))
    findings.extend(_monthly_breakdown(report))

    log.info(
        "reconciliation.done engagement=%s findings=%d error=%d warn=%d",
        engagement.engagement_id,
        len(findings),
        sum(1 for f in findings if f.severity == SEVERITY_ERROR),
        sum(1 for f in findings if f.severity == SEVERITY_WARN),
    )
    return findings


# ---- checks -------------------------------------------------------------------


def _duplicates(report: JournalReport) -> list[Finding]:
    """Identical (date, account, amount, memo) appearing more than once."""
    buckets: dict[_DupKey, list[JournalLine]] = defaultdict(list)
    for line in report.lines:
        amt = line.debit if line.debit != _ZERO else line.credit
        if amt == _ZERO:
            continue
        key = _DupKey(
            txn_date=line.txn_date,
            account=line.account,
            amount=abs(amt),
            memo_norm=_normalize_memo(line.description),
        )
        buckets[key].append(line)

    dup_groups = [lines for lines in buckets.values() if len(lines) >= 2]
    if not dup_groups:
        return [Finding(
            agent=AGENT, check="duplicates", severity=SEVERITY_OK,
            title="No exact duplicate journal lines detected",
        )]

    total_dup_lines = sum(len(g) for g in dup_groups)
    severity = SEVERITY_WARN if len(dup_groups) <= 5 else SEVERITY_ERROR
    sample_lines: list[str] = []
    for group in dup_groups[:5]:
        first = group[0]
        amt = first.debit if first.debit != _ZERO else first.credit
        memo = first.description[:60] + ("…" if len(first.description) > 60 else "")
        sample_lines.append(
            f"• {len(group)}× {first.txn_date.isoformat()}  ${amt:,.2f}  "
            f"{first.account} — {memo}"
        )
    if len(dup_groups) > 5:
        sample_lines.append(f"… and {len(dup_groups) - 5} more duplicate groups")

    return [Finding(
        agent=AGENT, check="duplicates", severity=severity,
        title=f"{len(dup_groups)} duplicate group(s) totalling {total_dup_lines} lines",
        detail="\n".join(sample_lines),
        proposed_fix=(
            "Each duplicate group is likely one transaction entered twice. Delete the "
            "extra line(s) in QBO and re-run the audit."
        ),
    )]


def _missing_account(report: JournalReport) -> list[Finding]:
    # Only flag missing-account on lines that actually move money. QBO often emits
    # $0 invoice line-items (the service description on a parent invoice) with no
    # account — those are cosmetic, not audit issues.
    missing = [
        l for l in report.lines
        if not l.account.strip() and (l.debit != _ZERO or l.credit != _ZERO)
    ]
    if not missing:
        return [Finding(
            agent=AGENT, check="missing_account", severity=SEVERITY_OK,
            title="Every journal line has an account coded",
        )]

    total = sum((l.debit + l.credit for l in missing), _ZERO)
    sample = [
        f"• {l.txn_date.isoformat()}  ${(l.debit or l.credit):,.2f}  "
        f"{l.description[:70]}"
        for l in missing[:5]
    ]
    if len(missing) > 5:
        sample.append(f"… and {len(missing) - 5} more")
    return [Finding(
        agent=AGENT, check="missing_account", severity=SEVERITY_ERROR,
        title=f"{len(missing)} line(s) with no account (${total:,.2f} total)",
        detail="\n".join(sample),
        proposed_fix=(
            "Open each transaction in QBO and assign a proper GL account. "
            "'Ask My Accountant' and 'Uncategorized' count as missing for audit purposes."
        ),
    )]


def _out_of_period(report: JournalReport, period_label: str | None) -> list[Finding]:
    """Flag lines whose date falls outside the reporting period.

    Without a parsed period, we fall back to the year extracted from ``period_label``.
    If we can't infer a year, this check is skipped (returns OK).
    """
    year = _infer_year(period_label)
    if year is None:
        return []

    out = [l for l in report.lines if l.txn_date.year != year]
    if not out:
        return [Finding(
            agent=AGENT, check="out_of_period", severity=SEVERITY_OK,
            title=f"All {len(report.lines):,} lines fall inside {year}",
        )]

    sample_years = Counter(l.txn_date.year for l in out).most_common(3)
    spread = ", ".join(f"{y}:{n}" for y, n in sample_years)
    return [Finding(
        agent=AGENT, check="out_of_period", severity=SEVERITY_WARN,
        title=f"{len(out)} line(s) dated outside {year}",
        detail=f"Year distribution of outliers: {spread}",
        proposed_fix=(
            "Usually caused by running the journal with the wrong 'Report period' in QBO. "
            "If the out-of-period rows are genuine (e.g., prior-year adjustments), "
            "confirm and re-run with the correct period. Otherwise delete or reclassify."
        ),
    )]


_INTERAC_RE = re.compile(r"(interac|e[- ]?transfer)", re.IGNORECASE)
_SALES_ACCOUNT_TOKENS = ("sales", "revenue", "income")
_SHAREHOLDER_ACCOUNT_TOKENS = ("shareholder",)
# Accounts that are bank operations — Interac activity here is bank-side
# movement (service charges, debit memos, bank-to-bank transfers), NOT a
# classification problem the bookkeeper should be prompted to review.
_BANK_CASH_TAX_TOKENS = (
    "bank", "checking", "savings", "cash", "undeposited",
    "scotia", "rbc", "td canada", "td bank", "bmo", "cibc",
    "national bank", "desjardins", "laurentian",
    "gst", "hst", "qst", "tvq", "pst", "tps",
    "credit card",
)


def _is_bank_cash_or_tax(account: str) -> bool:
    a = account.lower()
    return any(tok in a for tok in _BANK_CASH_TAX_TOKENS)


def _interac_deposits(report: JournalReport) -> list[Finding]:
    """Interac transfers recorded as deposits need supporting documentation.

    Scope notes driven by real-world QBO use:

    * We only look at ``txn_type == "Deposit"`` — Interac mentions on other
      transaction types are usually bank service charges or transfers, not
      classification problems.
    * We filter out bank/cash/tax accounts from the "credit side" we look at.
      The bank-side line of a deposit is a debit to an asset and isn't what
      the bookkeeper needs to audit; we only want the revenue/equity line.
    * Severity is **WARN** — this is a documentation check, not a hard
      error. The coding may be correct; the bookkeeper just needs the
      receipt (for sales) or the signed owner statement (for shareholder
      advances) on file before the CPA signs off.
    """
    interac_lines = [
        l for l in report.lines
        if _INTERAC_RE.search(l.description)
        and l.credit > _ZERO
        and l.txn_type == "Deposit"
        and not _is_bank_cash_or_tax(l.account)
    ]
    if not interac_lines:
        return [Finding(
            agent=AGENT, check="interac_deposits", severity=SEVERITY_OK,
            title="No Interac deposits needing documentation review",
        )]

    sales_lines: list[JournalLine] = []
    shareholder_lines: list[JournalLine] = []
    other_lines: list[JournalLine] = []
    for line in interac_lines:
        account_lower = line.account.lower()
        if any(k in account_lower for k in _SHAREHOLDER_ACCOUNT_TOKENS):
            shareholder_lines.append(line)
        elif any(k in account_lower for k in _SALES_ACCOUNT_TOKENS):
            sales_lines.append(line)
        else:
            other_lines.append(line)

    def _render(lines: list[JournalLine]) -> str:
        """Show every line — no truncation. Each line includes the QBO entry # so
        the bookkeeper can locate the transaction directly in QBO."""
        out: list[str] = []
        for l in lines:
            entry_ref = f"#{l.entry_number}" if l.entry_number else "—"
            desc = l.description[:80] + ("…" if len(l.description) > 80 else "")
            out.append(
                f"• {l.txn_date.isoformat()}  {entry_ref:>6}  "
                f"${l.credit:,.2f}  [{l.account}]  {desc}"
            )
        return "\n".join(out)

    findings: list[Finding] = []
    if sales_lines:
        total = sum((l.credit for l in sales_lines), _ZERO)
        findings.append(Finding(
            agent=AGENT, check="interac_deposits_sales", severity=SEVERITY_WARN,
            title=(
                f"{len(sales_lines)} Interac deposits coded as Sales (${total:,.2f}) "
                "— confirm a receipt/invoice is on file for each"
            ),
            detail=_render(sales_lines),
            proposed_fix=(
                "Please confirm each Interac deposit coded to Sales has the underlying "
                "receipt or invoice on file. CRA requires supporting documentation for "
                "revenue recognition. If the receipts are attached in QBO or stored in "
                "your working papers, this check is satisfied."
            ),
        ))

    if shareholder_lines:
        total = sum((l.credit for l in shareholder_lines), _ZERO)
        findings.append(Finding(
            agent=AGENT, check="interac_deposits_shareholder", severity=SEVERITY_WARN,
            title=(
                f"{len(shareholder_lines)} Interac deposits coded as Shareholder advances "
                f"(${total:,.2f}) — confirm with the owner"
            ),
            detail=_render(shareholder_lines),
            proposed_fix=(
                "Please ask the shareholder to confirm in writing that each Interac "
                "transfer is a personal advance to the company (debt, not income). "
                "Without written confirmation on file, CRA may reclassify as unreported "
                "revenue during audit. A signed email or statement kept in the working "
                "papers is sufficient."
            ),
        ))

    if other_lines:
        total = sum((l.credit for l in other_lines), _ZERO)
        findings.append(Finding(
            agent=AGENT, check="interac_deposits_other", severity=SEVERITY_WARN,
            title=f"{len(other_lines)} Interac deposits coded to other accounts (${total:,.2f})",
            detail=_render(other_lines),
            proposed_fix=(
                "These deposits are coded to accounts that aren't typical revenue or "
                "shareholder-advance accounts. Please review each and confirm the "
                "classification is intentional. If correct, no action needed."
            ),
        ))
    return findings


def _monthly_breakdown(report: JournalReport) -> list[Finding]:
    """Informational: count + total by month. Helps spot gaps or closing drift."""
    if not report.lines:
        return []
    by_month: dict[tuple[int, int], tuple[int, Decimal]] = defaultdict(lambda: (0, _ZERO))
    for line in report.lines:
        key = (line.txn_date.year, line.txn_date.month)
        count, total = by_month[key]
        by_month[key] = (count + 1, total + line.debit + line.credit)

    rows = [
        f"• {y}-{m:02d}: {count:>4} lines, ${total:>12,.2f}"
        for (y, m), (count, total) in sorted(by_month.items())
    ]
    return [Finding(
        agent=AGENT, check="monthly_activity", severity=SEVERITY_INFO,
        title=f"Activity spans {len(by_month)} month(s)",
        detail="\n".join(rows),
        proposed_fix=(
            "Look for missing months or a spike that doesn't match the business's "
            "seasonality — both are early signs of closing or posting issues."
        ),
    )]


# ---- helpers ------------------------------------------------------------------


def _normalize_memo(memo: str) -> str:
    """Lowercased, punctuation-stripped, whitespace-normalized memo for dup matching."""
    m = memo.lower()
    m = _NONWORD_RE.sub(" ", m)
    m = _WS_RE.sub(" ", m).strip()
    return m


def _infer_year(period_label: str | None) -> int | None:
    if not period_label:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", period_label)
    return int(m.group(0)) if m else None
