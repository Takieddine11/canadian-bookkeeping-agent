"""Agent 2 — Sales-tax auditor (deterministic v1, no LLM).

Canadian sales tax is the highest-risk area in a small-business audit: CRA
re-assessments for miscoded ITCs or under-remitted GST/HST/QST are expensive and
often traceable to bookkeeper coding errors rather than genuine non-compliance.

This v1 does **no LLM work**. It derives everything from the journal + balance
sheet and emits findings that a CPA can action. A later v2 will add a knowledge
base (``corp-taxprep-brain``) and per-vendor bill sampling.

Checks performed:

1. **Tax account inventory** — which of GST/HST, QST/TVQ, PST tax accounts exist.
   If the client has non-trivial activity and *only* a GST/HST account, the
   provincial tax is probably miscoded into it.
2. **Net tax position** — tax collected from customers (credits) minus tax paid
   to suppliers (debits). Compare against Balance Sheet ``GST/HST Payable``.
3. **Vendor-level tax rates** — top vendors by spend with implied tax rate.
4. **Rate outliers** — vendors whose implied rate differs from the most common
   rate in the file; likely coding errors that need a bill lookup.
5. **Missing tax on typical taxable purchases** — vendors with 0% tax and
   non-exempt names (helps the CPA decide which to spot-check).

Rate interpretation for Canadian context (for future UI/LLM layers):

* 5%           → GST only (AB, NT, NU, YT, or Quebec missing QST)
* 13%          → HST (ON)
* 14.975%      → GST (5%) + QST (9.975%) — Quebec combined
* 15%          → HST (NB, NS, NL, PE)
* 0%           → zero-rated / exempt (insurance, residential rent, basic groceries)
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from src.agents.base import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_OK,
    SEVERITY_WARN,
    Finding,
)
from src.parsers.financial_statement import parse_balance_sheet
from src.parsers.journal import JournalLine, JournalReport, parse_journal_csv
from src.store.engagement_db import (
    DOC_BALANCE_SHEET,
    DOC_JOURNAL,
    Engagement,
    EngagementStore,
)

log = logging.getLogger(__name__)
AGENT = "tax_auditor"
_ZERO = Decimal("0")

# Substrings identifying tax accounts.
_GST_HST_TOKENS = ("gst", "hst", "tps")
_QST_TOKENS = ("qst", "tvq")
_PST_TOKENS = ("pst",)
_ALL_TAX_TOKENS = _GST_HST_TOKENS + _QST_TOKENS + _PST_TOKENS

# Rate buckets (percentage, label).
_STANDARD_RATES: list[tuple[Decimal, str]] = [
    (Decimal("0"),        "exempt / zero-rated"),
    (Decimal("5"),        "GST only"),
    (Decimal("13"),       "HST (Ontario)"),
    (Decimal("14.975"),   "GST + QST (Quebec)"),
    (Decimal("15"),       "HST (Atlantic)"),
]
_RATE_TOLERANCE = Decimal("0.3")  # % points — 14.98% still counts as "14.975"
_MIN_VENDOR_SPEND = Decimal("100")  # ignore tiny vendors in the outlier check

# Transaction types representing purchases from suppliers — the ones where
# implied tax rate should match a Canadian standard bucket. Everything else
# (Invoice/Payment/Deposit/Transfer/most Journal Entries) is customer-side or
# bookkeeper adjustment and should NOT be in the vendor analysis.
_VENDOR_TXN_TYPES = frozenset({
    "Expense", "Bill", "Credit Card Expense", "Check", "Cheque",
    "Credit Card Credit", "Vendor Credit",
})


@dataclass(frozen=True)
class _VendorStats:
    name: str
    spend: Decimal
    tax: Decimal
    line_count: int

    @property
    def implied_rate(self) -> Decimal:
        if self.spend <= _ZERO:
            return _ZERO
        return (self.tax / self.spend * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def run(store: EngagementStore, engagement: Engagement) -> list[Finding]:
    docs = store.list_documents(engagement.engagement_id)
    journal_doc = next((d for d in docs if d.doc_type == DOC_JOURNAL), None)
    bs_doc = next((d for d in docs if d.doc_type == DOC_BALANCE_SHEET), None)

    if journal_doc is None:
        return [Finding(
            agent=AGENT, check="journal_present", severity=SEVERITY_ERROR,
            title="Journal not uploaded",
            detail="Tax audit requires the QBO Journal Detail export.",
        )]

    try:
        report = parse_journal_csv(Path(journal_doc.file_path))
    except Exception as exc:
        log.exception("tax_auditor.parse_failed path=%s", journal_doc.file_path)
        return [Finding(
            agent=AGENT, check="journal_parse", severity=SEVERITY_ERROR,
            title="Could not parse the journal",
            detail=f"{type(exc).__name__}: {exc}",
        )]

    findings: list[Finding] = []
    findings.extend(_tax_account_inventory(report))
    findings.extend(_net_tax_position(report, bs_doc))
    vendor_stats = _compute_vendor_stats(report)
    findings.extend(_top_vendors(vendor_stats))
    findings.extend(_rate_outliers(vendor_stats))

    log.info(
        "tax_auditor.done engagement=%s findings=%d error=%d warn=%d",
        engagement.engagement_id,
        len(findings),
        sum(1 for f in findings if f.severity == SEVERITY_ERROR),
        sum(1 for f in findings if f.severity == SEVERITY_WARN),
    )
    return findings


# ---- checks -------------------------------------------------------------------


def _tax_account_inventory(report: JournalReport) -> list[Finding]:
    """Inventory of tax accounts + Quebec-activity detection.

    IMPORTANT — QBO reality check: QuickBooks Online tracks GST and QST via
    *tax codes* inside the Tax Center, **not** via separate GL accounts. A
    Quebec-registered QBO file typically has ONE consolidated tax-payable
    account (often "GST/HST Payable" or "Sales Tax Payable"), with QBO
    computing the federal-vs-provincial split internally from the applied
    tax codes at filing time.

    So we surface Quebec activity as INFO for the CPA to verify in the Tax
    Center — we do NOT flag it as an error on the chart of accounts.
    """
    accounts = {l.account for l in report.lines if l.account}
    tax_accts = [a for a in accounts if _matches(a, _ALL_TAX_TOKENS)]
    detail = "Tax accounts in use:\n" + "\n".join(f"• {a}" for a in sorted(tax_accts)) \
        if tax_accts else "No tax-related accounts found in the journal."

    findings: list[Finding] = [Finding(
        agent=AGENT, check="tax_accounts_present",
        severity=SEVERITY_INFO,
        title=f"{len(tax_accts)} sales-tax account(s) in the chart",
        detail=detail,
    )]

    if _looks_like_quebec(report):
        findings.append(Finding(
            agent=AGENT, check="quebec_activity_detected",
            severity=SEVERITY_INFO,
            title="Quebec activity detected — verify GST+QST in QBO Tax Center",
            detail=(
                "The journal shows vendors with implied tax rates around 14.975% "
                "(5% GST + 9.975% QST), indicating the client operates in Quebec. "
                "QBO tracks GST and QST via tax codes inside the Tax Center, not "
                "via separate GL accounts — a single consolidated 'GST/HST Payable' "
                "account is expected and correct."
            ),
            proposed_fix=(
                "Open the QBO Tax Center and confirm: (1) both GST and QST are "
                "registered and active, (2) the QST registration number is on file, "
                "(3) filing frequency is set for each, and (4) the correct tax codes "
                "are being used on Quebec purchases (e.g., \"GST+QST\" / \"HST QC\"). "
                "No chart-of-accounts restructuring is required."
            ),
        ))

    return findings


def _net_tax_position(
    report: JournalReport, bs_doc
) -> list[Finding]:
    """Compare journal-derived net tax to Balance Sheet GST/HST Payable."""
    input_tax = _ZERO   # tax paid to suppliers (debits to tax accounts)
    output_tax = _ZERO  # tax collected from customers (credits to tax accounts)
    for line in report.lines:
        if _matches(line.account, _ALL_TAX_TOKENS):
            input_tax += line.debit
            output_tax += line.credit
    net = output_tax - input_tax

    detail = (
        f"• Output tax (credits, collected): ${output_tax:,.2f}\n"
        f"• Input tax (debits, ITC / refundable): ${input_tax:,.2f}\n"
        f"• Net payable per journal: ${net:,.2f}"
    )

    if bs_doc is None:
        return [Finding(
            agent=AGENT, check="net_tax_position", severity=SEVERITY_INFO,
            title=f"Net sales-tax position per journal: ${net:,.2f}",
            detail=detail,
            proposed_fix="Upload the Balance Sheet so this can be tied to GST/HST Payable.",
        )]

    try:
        bs = parse_balance_sheet(Path(bs_doc.file_path))
    except Exception:
        log.exception("tax_auditor.bs_parse_failed")
        return [Finding(
            agent=AGENT, check="net_tax_position", severity=SEVERITY_INFO,
            title=f"Net sales-tax position per journal: ${net:,.2f}",
            detail=detail,
        )]

    bs_payable = bs.amount_of("GST/HST Payable") or _ZERO
    diff = net - (bs_payable * -1)  # BS Payable is a liability (negative in rollforward); invert
    # Simpler: compare absolute magnitudes.
    diff_simple = abs(abs(net) - abs(bs_payable))

    tol = Decimal("1.00")
    if diff_simple <= tol:
        return [Finding(
            agent=AGENT, check="net_tax_position", severity=SEVERITY_OK,
            title=f"Journal tax ties to BS GST/HST Payable (${bs_payable:,.2f})",
            detail=detail,
        )]

    return [Finding(
        agent=AGENT, check="net_tax_position", severity=SEVERITY_WARN,
        title=f"Net tax per journal ${net:,.2f} vs BS Payable ${bs_payable:,.2f}",
        detail=(
            detail
            + f"\n• Gap: ${diff_simple:,.2f}"
        ),
        proposed_fix=(
            "A difference usually means: (a) tax returns filed but not matched against "
            "Payable, (b) prior-period opening balance, or (c) coding errors the tax "
            "auditor will surface vendor-by-vendor."
        ),
    )]


def _compute_vendor_stats(report: JournalReport) -> list[_VendorStats]:
    """Group vendor-side (purchase) journal entries by supplier name.

    Works at the group level: if any line in the group is a supplier txn type
    (Expense / Bill / Credit Card Expense / Check), the whole group is treated as
    a purchase, and the supplier name is taken from the first non-blank ``name``.
    """
    # Index lines by group so we can classify each journal entry as vendor-side or not.
    by_vendor: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"spend": _ZERO, "tax": _ZERO, "count": 0}
    )
    for group_id, lines in report.groups().items():
        if not any(l.txn_type in _VENDOR_TXN_TYPES for l in lines):
            continue
        vendor = next((l.name.strip() for l in lines if l.name.strip()), "")
        if not vendor:
            continue
        bucket = by_vendor[vendor]
        for l in lines:
            if _matches(l.account, _ALL_TAX_TOKENS):
                bucket["tax"] += l.debit - l.credit
            else:
                # Debit to non-tax account on a purchase = spend (expense or asset).
                if l.debit > 0:
                    bucket["spend"] += l.debit
                    bucket["count"] += 1

    out = [
        _VendorStats(
            name=name,
            spend=stats["spend"],
            tax=stats["tax"],
            line_count=stats["count"],
        )
        for name, stats in by_vendor.items()
    ]
    return sorted(out, key=lambda v: v.spend, reverse=True)


def _top_vendors(vendors: list[_VendorStats], n: int = 10) -> list[Finding]:
    top = [v for v in vendors if v.spend > _ZERO][:n]
    if not top:
        return []
    lines = [
        f"• {v.name[:30]:30}  spend ${v.spend:>10,.2f}  "
        f"tax ${v.tax:>8,.2f}  implied rate {v.implied_rate:>5.2f}%"
        for v in top
    ]
    return [Finding(
        agent=AGENT, check="top_vendors_by_spend", severity=SEVERITY_INFO,
        title=f"Top {len(top)} vendors by spend",
        detail="\n".join(lines),
        proposed_fix=(
            "These vendors drive most of the tax exposure. A v2 pass will request "
            "one sample bill per vendor to verify the tax codes on the actual invoices."
        ),
    )]


def _rate_outliers(vendors: list[_VendorStats]) -> list[Finding]:
    """Vendors whose implied rate doesn't match any standard Canadian bucket."""
    material = [v for v in vendors if v.spend >= _MIN_VENDOR_SPEND]
    if not material:
        return []

    outliers: list[_VendorStats] = []
    for v in material:
        if not _matches_any_standard_rate(v.implied_rate):
            outliers.append(v)

    if not outliers:
        return [Finding(
            agent=AGENT, check="rate_outliers", severity=SEVERITY_OK,
            title=f"All {len(material)} material vendors show a standard Canadian tax rate",
        )]

    lines = [
        f"• {v.name[:30]:30}  spend ${v.spend:>10,.2f}  rate {v.implied_rate:>5.2f}%  "
        f"(expected 0 / 5 / 13 / 14.975 / 15%)"
        for v in outliers[:15]
    ]
    if len(outliers) > 15:
        lines.append(f"… and {len(outliers) - 15} more")
    return [Finding(
        agent=AGENT, check="rate_outliers", severity=SEVERITY_WARN,
        title=f"{len(outliers)} vendor(s) with non-standard tax rate",
        detail="\n".join(lines),
        proposed_fix=(
            "Common causes:\n"
            "• Rate 5% where 14.975% expected → QST missing on Quebec purchase\n"
            "• Rate 0% on taxable purchase → tax not captured (no ITC claimed)\n"
            "• Rate between brackets → partial tax, refund, or coding split\n"
            "Spot-check one recent bill per vendor."
        ),
    )]


# ---- helpers ------------------------------------------------------------------


def _matches(account: str, tokens: tuple[str, ...]) -> bool:
    a = account.lower()
    return any(t in a for t in tokens)


def _matches_any_standard_rate(rate: Decimal) -> bool:
    return any(abs(rate - r) <= _RATE_TOLERANCE for r, _ in _STANDARD_RATES)


_RATE_NEAR_QST_COMBINED = Decimal("14.975")


def _looks_like_quebec(report: JournalReport) -> bool:
    """Very coarse: any vendor whose implied tax rate clusters around 14.975%."""
    by_vendor: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"spend": _ZERO, "tax": _ZERO})
    for line in report.lines:
        name = line.name.strip()
        if not name:
            continue
        if _matches(line.account, _ALL_TAX_TOKENS):
            by_vendor[name]["tax"] += line.debit
        elif line.debit > 0:
            by_vendor[name]["spend"] += line.debit
    for stats in by_vendor.values():
        if stats["spend"] >= _MIN_VENDOR_SPEND:
            rate = (stats["tax"] / stats["spend"] * 100) if stats["spend"] > 0 else _ZERO
            if abs(rate - _RATE_NEAR_QST_COMBINED) <= _RATE_TOLERANCE:
                return True
    return False
