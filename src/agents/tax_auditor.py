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
    journal_doc = store.latest_document(engagement.engagement_id, DOC_JOURNAL)
    bs_doc = store.latest_document(engagement.engagement_id, DOC_BALANCE_SHEET)

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
    findings.extend(_tax_refund_direction_audit(report))
    vendor_stats = _compute_vendor_stats(report)
    quick_method = _detect_quick_method_pattern(vendor_stats)
    findings.extend(_top_vendors(vendor_stats))
    findings.extend(_vendor_invoice_verification(vendor_stats, quick_method))
    findings.extend(_rate_outliers(vendor_stats, quick_method))

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


# Keywords that strongly indicate a transaction involves a government agency —
# CRA (federal, Agence du Revenu du Canada / ARC) or Revenu Québec (RQ) or a
# generic "government" descriptor. A line hitting a tax liability account with
# one of these in the memo is almost certainly a REMITTANCE TO or REFUND FROM
# the tax authority. Both should be DEBITS that reduce the liability; booking
# them as CREDITS is the classic direction-flip error.
_GOVT_TAX_KEYWORDS = (
    # English — CRA / ARC
    "cra ", " cra", "canada revenue", "canada revenue agency", "arc ",
    "gst refund", "hst refund", "tax refund", "income tax refund",
    # French — Revenu Québec
    "revenu quebec", "revenu québec", "rq ", "rq tps", "rq tvq",
    "gouvernement du québec", "gouvernement du quebec",
    "gouv. qc", "gouv. québec", "gouv. quebec",
    "paiement divers",
    # Generic
    "refund", "remboursement", "remb. ", "remb ", "rebate",
    "crédit d'impôt", "credit d'impot",
    "remise tps", "remise tvq", "remise gst", "remise hst",
)


def _tax_refund_direction_audit(report: JournalReport) -> list[Finding]:
    """Government refunds/remittances on tax accounts should reduce the
    liability (debit), not increase it (credit). Catch the classic direction-flip.

    Real-world incident (Cleany Québec, Sep 2024):
      - 16/09 Deposit "GOUV. QUÉBEC Paiement divers" $1,202.67 → credited to
        QST Suspense account (increased liability)
      - 19/09 Deposit "RQ TPS" $602.84 → same pattern
    CPA caught it during T2 prep; correction was to flip both lines to debits,
    dropping Suspense by $3,611.02 (2× the credit total).

    Signal:
      - Line hits a tax liability account (GST/HST/QST/TVQ/PST/TPS account).
      - Line is a CREDIT > 0 (increases the liability).
      - Description contains a government-keyword phrase.

    Credits to tax accounts are legitimate when they represent tax *collected*
    on a sale (e.g., "HST on customer invoice"). They are NOT legitimate when
    described in government-remittance / refund language.
    """
    suspicious: list[JournalLine] = []
    for line in report.lines:
        if not _matches(line.account, _ALL_TAX_TOKENS):
            continue
        if line.credit <= _ZERO:
            continue
        desc_lower = line.description.lower()
        if not any(kw in desc_lower for kw in _GOVT_TAX_KEYWORDS):
            continue
        suspicious.append(line)

    if not suspicious:
        return [Finding(
            agent=AGENT, check="tax_refund_direction", severity=SEVERITY_OK,
            title="No miscoded government refunds/remittances detected on tax accounts",
        )]

    wrong_credit_total = sum((l.credit for l in suspicious), _ZERO)
    correction_impact = wrong_credit_total * 2  # flipping a credit to debit swings by 2x

    detail_lines = [
        f"• {l.txn_date.isoformat()}  deposit ${l.credit:,.2f}  "
        f"posted to [{l.account}] as a CREDIT  — "
        f"{l.description[:90] + ('…' if len(l.description) > 90 else '')}"
        for l in suspicious
    ]

    return [Finding(
        agent=AGENT, check="tax_refund_direction", severity=SEVERITY_ERROR,
        title=(
            f"{len(suspicious)} government refund deposit(s) booked backwards — "
            f"tax liability is OVERSTATED by ~${correction_impact:,.2f}"
        ),
        detail=(
            "These are deposits FROM the government (Revenu Québec / CRA) — "
            "they're **reimbursements**, money coming INTO the business.\n\n"
            "Reimbursements from the government should **REDUCE** the tax liability, "
            "so the tax-account line of each deposit should be a **DEBIT**. Here they "
            "were posted as **CREDITS**, which makes the liability BIGGER instead of "
            "smaller — the opposite of what happened economically.\n\n"
            "Transactions flagged:\n"
            + "\n".join(detail_lines)
            + f"\n\n**Net effect:** the tax liability is overstated by "
              f"~${correction_impact:,.2f} "
              f"(2× the {len(suspicious)}-line credit total of "
              f"${wrong_credit_total:,.2f} — flipping each line to a debit swings "
              "the balance by 2× the amount)."
        ),
        proposed_fix=(
            "In QBO, open each deposit transaction and change the tax-account line "
            "from CREDIT to DEBIT. This makes the deposit behave as a refund that "
            "reduces the liability, which is what actually happened. Before making "
            "the change, quickly confirm each amount against the Revenu Québec / "
            "CRA statement (My Business Account → Statement of Account) so you're "
            "sure these are genuine refunds and not something else. Expected result "
            f"after both corrections: the tax Payable/Suspense balance drops by "
            f"${correction_impact:,.2f}."
        ),
    )]


def _net_tax_position(
    report: JournalReport, bs_doc
) -> list[Finding]:
    """Surface journal-level tax movement for CPA review.

    IMPORTANT — this check used to COMPARE the journal net to the BS Payable
    and flag a WARNING on any gap. That was wrong: journal tax movement during
    a fiscal year includes **prior-period return payments** (debits to the tax
    account clearing last year's liability) plus **current-period accruals**
    (credits from sales, debits from purchase ITCs). The BS Payable balance
    is the cumulative net liability at period-end, not the sum of in-period
    activity. For example, a business paying its 2023-24 return during 2025
    has a big debit to the tax account that has nothing to do with 2025 net
    tax owed.

    The correct rollforward requires the **prior-period** BS balance, which we
    don't capture yet. Until we do, this finding surfaces the period numbers
    as INFO for the CPA to verify against filed returns — no comparison, no
    false alarm.
    """
    input_tax = _ZERO   # debits to tax accounts (ITCs + prior-period payments mixed)
    output_tax = _ZERO  # credits to tax accounts (tax collected + prior-period reversals)
    for line in report.lines:
        if _matches(line.account, _ALL_TAX_TOKENS):
            input_tax += line.debit
            output_tax += line.credit
    net = output_tax - input_tax

    bs_line = ""
    if bs_doc is not None:
        try:
            bs = parse_balance_sheet(Path(bs_doc.file_path))
            bs_payable = bs.amount_of("GST/HST Payable")
            if bs_payable is not None:
                bs_line = f"\n• BS GST/HST Payable at period-end: ${bs_payable:,.2f}"
        except Exception:
            log.exception("tax_auditor.bs_parse_failed")

    detail = (
        f"Tax-account activity **during the period** (includes prior-period "
        f"return payments clearing last year's balance):\n"
        f"• Total credits (collected on sales + adjustments): ${output_tax:,.2f}\n"
        f"• Total debits (ITCs + prior-period remittances): ${input_tax:,.2f}\n"
        f"• Net journal movement: ${net:,.2f}"
        + bs_line
    )

    return [Finding(
        agent=AGENT, check="net_tax_position", severity=SEVERITY_INFO,
        title="Sales-tax movement during the period — CPA to reconcile against filed returns",
        detail=detail,
        proposed_fix=(
            "The journal net and the BS balance are NOT directly comparable — the "
            "journal net mixes current-period accruals with prior-period payments made "
            "during this period, while the BS balance is the cumulative liability at "
            "period-end. To tie properly, the CPA needs: (1) prior-period closing "
            "Payable balance, (2) returns filed during this period with amounts, "
            "(3) current-period accrued tax. The rollforward is: "
            "prior balance + current accrued − returns paid = current balance."
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


def _rate_outliers(
    vendors: list[_VendorStats], quick_method_suspected: bool
) -> list[Finding]:
    """Vendors whose implied rate doesn't match any standard Canadian bucket.

    Quick-Method-aware: when the business looks like it's using the Quick Method
    of GST/HST Accounting (no ITCs claimed on ordinary purchases), 0% rates are
    expected and correct — we don't flag them. Only truly non-standard rates
    (stuck between brackets, mixed tax codes) get flagged as review items.
    """
    material = [v for v in vendors if v.spend >= _MIN_VENDOR_SPEND]
    if not material:
        return []

    outliers: list[_VendorStats] = []
    for v in material:
        if _matches_any_standard_rate(v.implied_rate):
            continue
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
            "• Rate between brackets → partial tax, refund, or mixed coding\n"
            "Please verify one recent invoice per flagged vendor to confirm the "
            "coding matches the actual tax charged."
        ),
    )]


def _detect_quick_method_pattern(vendors: list[_VendorStats]) -> bool:
    """Return True when the vendor tax profile looks like Quick Method accounting.

    Quick Method (CRA) lets eligible businesses (annual taxable sales ≤ $400K)
    remit a reduced rate on sales instead of tracking ITCs on ordinary purchases.
    Signature: nearly all material vendors show 0% implied tax rate because no
    ITCs are claimed. We call it "suspected" — the CPA still needs to confirm
    the client is actually registered for the election.
    """
    material = [v for v in vendors if v.spend >= _MIN_VENDOR_SPEND]
    if len(material) < 3:
        return False  # too few material vendors to tell
    zero_rate_count = sum(1 for v in material if v.implied_rate == _ZERO)
    # 80%+ of material vendors at 0% is a strong signal.
    return zero_rate_count / len(material) >= Decimal("0.8") if material else False


def _vendor_invoice_verification(
    vendors: list[_VendorStats], quick_method_suspected: bool
) -> list[Finding]:
    """Per-vendor verification request + Quick Method context.

    Emits two findings:

    1. **Quick Method context** (if the pattern matches) — tells the CPA to
       confirm the Quick Method election before treating 0% rates as correct.
       Without confirmation, 0% across all vendors could also mean ITCs are
       being systematically missed — the same signature, very different outcome.
    2. **Sample invoice list** — a single finding listing the top 10 material
       vendors, asking the bookkeeper to obtain one recent invoice per vendor
       so the tax coding can be spot-checked against the actual document.
    """
    material = [v for v in vendors if v.spend >= _MIN_VENDOR_SPEND]
    if not material:
        return []

    findings: list[Finding] = []

    if quick_method_suspected:
        total_spend = sum((v.spend for v in material), _ZERO)
        total_tax = sum((v.tax for v in material if v.tax > _ZERO), _ZERO)
        findings.append(Finding(
            agent=AGENT, check="quick_method_pattern_detected",
            severity=SEVERITY_INFO,
            title="Pattern suggests Quick Method of GST/HST Accounting — please confirm",
            detail=(
                f"Across {len(material)} material vendors (spend ≥ "
                f"${_MIN_VENDOR_SPEND:,.0f}), total ITCs recorded are "
                f"${total_tax:,.2f} on ${total_spend:,.2f} of spend. Nearly every "
                f"vendor shows a 0% implied tax rate.\n\n"
                "This signature is consistent with the **Quick Method of GST/HST "
                "Accounting** (CRA election for businesses with taxable sales ≤ "
                "$400K/yr). Under Quick Method:\n"
                "• The business charges regular GST/HST/QST on sales as normal.\n"
                "• It remits a **reduced rate** on those sales (e.g. 3.6% for "
                "most services in ON/NB/NS/NL/PE; 1.8% for services outside HST "
                "provinces) instead of the full rate.\n"
                "• It does **not** claim ITCs on ordinary operating expenses — so "
                "0% on most vendor bills is correct.\n"
                "• It **can** still claim ITCs on capital purchases > $10,000 "
                "(computers, vehicles, equipment) and on certain zero-rated purchases.\n"
            ),
            proposed_fix=(
                "Confirm with the client: is the business registered for the "
                "Quick Method election?\n"
                "• **Yes** → 0% on ordinary purchases is correct. Review any "
                "capital purchase > $10,000 to make sure an ITC was claimed.\n"
                "• **No** → ITCs are being systematically missed. Every vendor "
                "invoice should have the tax portion captured; re-process the "
                "period and file an amended return if material."
            ),
        ))

    # Per-vendor invoice verification request (one consolidated finding).
    sample = material[:10]
    header_hint = (
        "0% is expected under Quick Method — use these checks to confirm the coding "
        "matches the actual invoices."
        if quick_method_suspected else
        "Any vendor with 0% but material spend is a candidate for a missed ITC; any "
        "non-standard rate is a candidate for a coding error."
    )
    lines = [
        f"• **{v.name[:40]}** — spend ${v.spend:,.2f} over {v.line_count} lines, "
        f"implied rate {v.implied_rate:.2f}%"
        for v in sample
    ]
    findings.append(Finding(
        agent=AGENT, check="vendor_invoice_verification",
        severity=SEVERITY_INFO,
        title=f"Please verify one sample invoice per top-{len(sample)} vendor",
        detail=header_hint + "\n\n" + "\n".join(lines),
        proposed_fix=(
            "For each vendor above, please obtain **one recent invoice** (any month "
            "within the period) and confirm the tax amount charged on the invoice "
            "matches what was recorded in QBO. Attach the invoices to the working "
            "papers. If you spot a mismatch, flag the vendor for a full-period "
            "review — the pattern usually repeats across all that vendor's transactions."
        ),
    ))

    return findings


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
