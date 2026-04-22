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
    journal_doc = store.latest_document(engagement.engagement_id, DOC_JOURNAL)
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
    findings.extend(_sparse_journal_entry_memos(report))
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


# Threshold below which same-amount same-day same-vendor repeats are almost
# always legitimate (bank fees, service charges, Interac fees, card-processor
# tokens, "trust-building" Facebook Ads micro-charges). Flagging these creates
# noise without catching real duplication.
_MIN_DUP_AMOUNT = Decimal("50")

# If 4+ identical entries exist, the pattern is recurring (weekly subcontractor
# payments, monthly software subscriptions, scheduled payouts) — not a bookkeeper
# error. Real double-entry mistakes are almost always 2× (rarely 3×).
_MAX_DUP_COPIES = 3


def _duplicates(report: JournalReport) -> list[Finding]:
    """Flag lines that are ≥95% likely to be the SAME transaction entered twice.

    Strict filters — every one must match before we raise it:

    1. **Amount ≥ $50.** Smaller repeating amounts are bank fees, Interac service
       charges, or processor tokens — legitimate recurring activity.
    2. **Non-blank, normalized memo.** Two blank-memo entries can't be confidently
       called duplicates; could be unrelated postings that happen to share an amount.
    3. **Same vendor name.** Two different vendors charging $X on the same day to
       the same account is coincidence, not duplication.
    4. **2 to 3 copies in the bucket.** 4+ instances is recurring (weekly subcontractor,
       monthly subscription), not a bookkeeper mistake.
    5. **Different journal entry group IDs.** Multiple lines of one journal entry
       that happen to match on these fields are legitimate entry components, not
       duplicates — the bookkeeper's error mode is entering a whole entry twice.
    """
    buckets: dict[_DupKey, list[JournalLine]] = defaultdict(list)
    for line in report.lines:
        amt = line.debit if line.debit != _ZERO else line.credit
        if amt == _ZERO or abs(amt) < _MIN_DUP_AMOUNT:
            continue
        memo_norm = _normalize_memo(line.description)
        if not memo_norm:
            continue  # blank memo — can't confidently call it a duplicate
        vendor_norm = line.name.strip().lower()
        if not vendor_norm:
            continue  # blank vendor — same reason
        key = _DupKey(
            txn_date=line.txn_date,
            account=line.account,
            amount=abs(amt),
            memo_norm=memo_norm + "|" + vendor_norm,  # vendor included in the key
        )
        buckets[key].append(line)

    dup_groups: list[list[JournalLine]] = []
    for lines in buckets.values():
        # 2 or 3 copies only — 4+ is recurring.
        if not (2 <= len(lines) <= _MAX_DUP_COPIES):
            continue
        # Must span multiple journal entries; same-entry lines aren't duplicates.
        if len({l.group_id for l in lines}) < 2:
            continue
        dup_groups.append(lines)

    if not dup_groups:
        return [Finding(
            agent=AGENT, check="duplicates", severity=SEVERITY_OK,
            title="No high-confidence duplicate journal entries detected",
        )]

    # Always WARN, never ERROR — the check is tight enough now that each match
    # is a concrete candidate for CPA review, but we still want the bookkeeper
    # to confirm before deleting anything.
    total_dup_lines = sum(len(g) for g in dup_groups)
    sample_lines: list[str] = []
    for group in dup_groups:  # no truncation — user wants every candidate visible
        first = group[0]
        amt = first.debit if first.debit != _ZERO else first.credit
        memo = first.description[:80] + ("…" if len(first.description) > 80 else "")
        entries = ", ".join(f"#{l.entry_number or l.group_id}" for l in group)
        sample_lines.append(
            f"• {len(group)}×  {first.txn_date.isoformat()}  ${amt:,.2f}  "
            f"[{first.account}]  vendor=**{first.name}**  "
            f"entries={entries}  — {memo}"
        )

    return [Finding(
        agent=AGENT, check="duplicates", severity=SEVERITY_WARN,
        title=(
            f"{len(dup_groups)} high-confidence duplicate candidate(s) "
            f"covering {total_dup_lines} lines"
        ),
        detail="\n".join(sample_lines),
        proposed_fix=(
            "Each candidate matched on date + vendor + account + amount + memo, "
            "across different journal-entry IDs — strong indicators of the same "
            "transaction entered twice. Please open each pair in QBO, confirm "
            "they're the same underlying transaction, and delete the duplicate. "
            "Small repeating amounts (bank fees, processor tokens) are already "
            "filtered out; any recurring 4+ pattern is treated as a subscription, "
            "not a duplicate."
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


_GENERIC_JE_MEMOS = frozenset({
    "", "adjustment", "adjustments", "adj", "je", "journal entry",
    "correction", "fix", "fixed", ".", "-", "reclass", "reclassification",
    "entry", "to correct", "see attached", "misc", "various",
})
_MIN_JE_MEMO_WORDS = 3  # memos shorter than this rarely tell the CPA what happened


def _sparse_journal_entry_memos(report: JournalReport) -> list[Finding]:
    """Manual Journal Entry groups should have enough description for a CPA to
    understand what happened — otherwise they're black boxes at review time.

    Only applies to ``txn_type == "Journal Entry"`` (manual adjustments). Regular
    Expenses / Invoices / Deposits have standardized memos from the source
    transaction and don't need extra narrative.

    Flags a group if:
    * The combined memo text across all lines is blank, or
    * The memo is a known-generic term ("adjustment", "fix", "reclass", ".", etc.), or
    * The memo has fewer than 3 meaningful words.
    """
    sparse: list[tuple[str, list[JournalLine], str]] = []
    for group_id, lines in report.groups().items():
        if not any(l.txn_type.lower() == "journal entry" for l in lines):
            continue
        memos = [l.description.strip() for l in lines if l.description.strip()]
        combined = " / ".join(dict.fromkeys(memos))  # dedupe keeping order
        if not combined:
            sparse.append((group_id, lines, "blank memo"))
            continue
        if combined.lower() in _GENERIC_JE_MEMOS:
            sparse.append((group_id, lines, f"generic memo: “{combined}”"))
            continue
        word_count = sum(len(m.split()) for m in memos)
        if word_count < _MIN_JE_MEMO_WORDS:
            sparse.append((group_id, lines, f"only {word_count} word(s): “{combined}”"))

    total_je_groups = sum(
        1 for lines in report.groups().values()
        if any(l.txn_type.lower() == "journal entry" for l in lines)
    )

    if not sparse:
        if total_je_groups == 0:
            return []  # no manual JEs at all → check is N/A, stay silent
        return [Finding(
            agent=AGENT, check="journal_entry_memos", severity=SEVERITY_OK,
            title=f"All {total_je_groups} manual journal entries have adequate memos",
        )]

    sample_lines: list[str] = []
    for group_id, lines, reason in sparse:
        first = lines[0]
        total_debit = sum((l.debit for l in lines), _ZERO)
        entry_ref = f"#{first.entry_number}" if first.entry_number else group_id
        accounts = ", ".join(sorted({l.account for l in lines if l.account}))[:120]
        sample_lines.append(
            f"• JE {entry_ref}  {first.txn_date.isoformat()}  ${total_debit:,.2f}  "
            f"accounts: {accounts or '—'}  — {reason}"
        )

    return [Finding(
        agent=AGENT, check="journal_entry_memos", severity=SEVERITY_WARN,
        title=(
            f"{len(sparse)} of {total_je_groups} manual journal entries have "
            f"insufficient memos for CPA review"
        ),
        detail="\n".join(sample_lines),
        proposed_fix=(
            "Manual journal entries are the bookkeeper's narrative to the CPA. "
            "Please open each flagged entry in QBO and add a memo explaining: "
            "(1) what transaction or event prompted the entry, (2) which source "
            "document supports it (invoice, contract, CRA notice, etc.), and "
            "(3) if it's a reclassification, which original transaction(s) it "
            "corrects. 'Adjustment', 'reclass', or a single word isn't enough."
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
