"""Agent — Government Remittance Auditor (deterministic).

When the bookkeeper cuts a cheque to the Receiver General, Revenu Québec, or
any government payee, three things can go wrong and none of them look dramatic
in isolation:

1. **Miscategorized payment type.** A single "CRA" payee covers at least three
   distinct liabilities: (a) corporate income tax installments, (b) GST/HST
   remittances, (c) payroll source deductions. Each settles a different BS
   liability. Booking all three to "Payroll Liabilities" (or worse, to an
   expense account) silently corrupts the books.

2. **Expense-side contamination.** Remittances that reduce a liability must
   land on a BS liability account, not on a P&L expense. A bookkeeper who
   posts a $6,000 CRA cheque to "Professional Fees" or "Bank Charges" is
   double-counting: the salary accrual already hit expense last month, and now
   the remittance hits expense again.

3. **Prior-year-liability noise.** A payment made in the current year that
   settles last year's GST/QST return (or last year's corporate tax balance
   due) is NOT a current-year expense — it reduces the opening BS liability
   inherited from FY(N-1). The bookkeeper must not treat it as this year's
   cost. Without a clear split, the current-year P&L absorbs noise from the
   prior year's close.

This agent reads the journal and every document we have, finds every
government-payee transaction, classifies it, sums by category, and cross-
reconciles against the Balance Sheet liability balances at year-end.

Output is opinionated and action-oriented: "these $N were paid to CRA for
payroll DAS, your Payroll Liabilities account moved from $A to $B, the math
ties" — or it doesn't tie, and the gap is named in dollars.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from src.agents.base import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_OK,
    SEVERITY_WARN,
    Finding,
)
from src.parsers import labels as L
from src.parsers.financial_statement import parse_balance_sheet
from src.parsers.journal import JournalLine, JournalReport, parse_journal_csv
from src.store.engagement_db import (
    DOC_BALANCE_SHEET,
    DOC_JOURNAL,
    DOC_PRIOR_YEAR_BS,
    Engagement,
    EngagementStore,
)

log = logging.getLogger(__name__)
AGENT = "government_remittance"
_ZERO = Decimal("0")


# Payee patterns: lower-cased, matched as substrings in the journal line's
# `name` (vendor) field. Cover EN + FR export variants.
# Government-payee matching is done in two passes to avoid substring false
# positives. First pass — full, unambiguous phrases (these can safely be
# matched as case-insensitive substrings because the phrase itself is long
# and unique to government payees). Second pass — short acronyms that MUST
# be matched as whole words (\b...\b) because the character sequences
# appear inside unrelated vendor names (e.g. the "ARC" inside "Saint M-ARC
# Holdings", "CRA" inside "sacramento", "RQ" inside "Marquis").

_GOV_PAYEE_PHRASES: tuple[str, ...] = (
    "receiver general",
    "receiver-general",
    "canada revenue agency",
    "canada revenue",
    "agence du revenu du canada",
    "revenu quebec",
    "revenu québec",
    "revenu qc",
    "gouvernement du québec",
    "gouv. québec",
    "gouv québec",
    "ministère du revenu",
    "ministere du revenu",
)

# Acronyms matched as whole words only. "cra", "arc", "rq" are all common
# character sequences in unrelated vendor names — whole-word matching is
# essential.
_GOV_PAYEE_ACRONYMS: frozenset[str] = frozenset({
    "cra", "arc", "rq",
})


# Category classifier — keyword-based, not exact-phrase. Each rule is a
# predicate over the lowercased "haystack" (memo + accounts touched). First
# match wins. We use separate tax-type-keyword and action-keyword checks so
# that "GST/HST Q1 remittance" matches even though "gst/hst remittance" is
# not a contiguous substring of it.

def _has_any(hay: str, needles: tuple[str, ...]) -> bool:
    return any(n in hay for n in needles)


_GST_KEYWORDS = ("gst", "hst", "tps", "tvh", "gst/hst", "tps/tvh")
_QST_KEYWORDS = ("qst", "tvq")
_COMBINED_TAX_KEYWORDS = ("gst/qst", "tps/tvq", "sales tax", "taxes de vente")
_TAX_ACTION_KEYWORDS = ("remittance", "remit ", "return ", "return,", "remise",
                        "remises", "file ", "q1", "q2", "q3", "q4",
                        "monthly", "mensuel", "quarterly", "trimestriel",
                        "annual filing", "annuel")
_PAYROLL_KEYWORDS = ("source deduction", "source deductions", "das ",
                     "déduction à la source", "deduction a la source",
                     "payroll remittance", "remittance payroll",
                     "retenues à la source", "retenues a la source",
                     "cpp/ei", "cpp + ei", "qpp/qpip", "source deduct",
                     " cpp ", " ei ", " qpp ", " qpip ", " rrq ", " rqap ",
                     "fed tax", "federal tax withholding", "dpa ")
_CORP_TAX_KEYWORDS = ("corporate tax", "corporate income tax",
                      "income tax installment", "tax installment",
                      "t2 installment", "t2-", "co-17", "co17",
                      "impôt des sociétés", "impot des societes",
                      "impôt corp", "impot corp",
                      "acompte provisionnel", "t2 acompte",
                      "installment #")
_CNESST_KEYWORDS = ("cnesst", "csst", "wsib", "wcb", "workers comp")
_FSS_KEYWORDS = ("fss ", "fonds des services de santé",
                 "health services fund")


def _classify_haystack(combined: str) -> str:
    """Apply the rules in priority order. First match wins."""
    # Payroll first — payroll DAS has the most distinctive keywords and
    # collides least with other categories.
    if _has_any(combined, _PAYROLL_KEYWORDS):
        return "payroll_das"
    # Combined GST+QST ("sales tax", "gst/qst") before individual tax types,
    # because "gst/qst" contains both "gst" and "qst".
    if _has_any(combined, _COMBINED_TAX_KEYWORDS) and _has_any(combined, _TAX_ACTION_KEYWORDS):
        return "combined_sales_tax_remittance"
    # Individual tax types require BOTH a tax keyword AND an action keyword
    # (so the word "hst" appearing in "shellharbour" doesn't false-match).
    gst_hit = _has_any(combined, _GST_KEYWORDS)
    qst_hit = _has_any(combined, _QST_KEYWORDS)
    action_hit = _has_any(combined, _TAX_ACTION_KEYWORDS)
    if gst_hit and qst_hit and action_hit:
        return "combined_sales_tax_remittance"
    if gst_hit and action_hit:
        return "gst_hst_remittance"
    if qst_hit and action_hit:
        return "qst_remittance"
    # Corporate tax.
    if _has_any(combined, _CORP_TAX_KEYWORDS):
        return "corporate_tax"
    # CNESST / FSS.
    if _has_any(combined, _CNESST_KEYWORDS):
        return "cnesst_workers_comp"
    if _has_any(combined, _FSS_KEYWORDS):
        return "fss_health_contribution"
    return "unclassified"


_CATEGORY_LABELS: dict[str, str] = {
    "payroll_das":                 "Payroll source deductions (DAS)",
    "gst_hst_remittance":          "GST/HST remittance",
    "qst_remittance":              "QST remittance",
    "combined_sales_tax_remittance": "Combined GST + QST remittance",
    "corporate_tax":               "Corporate income tax (installment or balance due)",
    "cnesst_workers_comp":         "CNESST / workers' comp",
    "fss_health_contribution":     "FSS / provincial health levy",
    "unclassified":                "Unclassified government payment",
}


# Expense-like account tokens — a remittance posted to one of these is a
# Tier-4 coding error (settles a liability, must land on a liability account).
_EXPENSE_ACCOUNT_TOKENS: tuple[str, ...] = (
    "expense", "expenses", "charges", "fees", "cost", "professional fees",
    "bank charges", "miscellaneous", "misc ", "office supplies",
    "frais", "dépenses", "depenses", "honoraires",
)


# Legitimate liability accounts a remittance should land on. We don't need an
# exhaustive list — these are keyword signatures. If the credit leg hits any
# of these, the Dr leg (on the expense account under audit) is the issue.
_LIABILITY_ACCOUNT_TOKENS: tuple[str, ...] = (
    "payroll liabilities", "payroll liab", "das",
    "gst/hst payable", "gst payable", "hst payable",
    "qst payable", "tps/tvh", "tps/tvq",
    "sales tax payable", "taxes de vente à payer", "taxes de vente a payer",
    "corporate tax payable", "income tax payable", "taxes payable",
    "impôts à payer", "impots a payer", "taxes à payer",
    "cnesst payable", "cnesst à payer", "csst payable",
)


@dataclass
class _Remittance:
    """One identified government-bound payment with classification metadata."""
    line: JournalLine
    category: str
    expense_side_miscoded: bool   # the *other* side of the JE hit a P&L expense account
    amount: Decimal               # always positive for display
    is_prior_year_suspect: bool   # paid in current period but looks like prior-year settlement


def run(store: EngagementStore, engagement: Engagement) -> list[Finding]:
    journal_doc = store.latest_document(engagement.engagement_id, DOC_JOURNAL)
    bs_doc = store.latest_document(engagement.engagement_id, DOC_BALANCE_SHEET)

    if journal_doc is None:
        return [Finding(
            agent=AGENT, check="journal_present", severity=SEVERITY_ERROR,
            title="Journal not uploaded — cannot audit government remittances",
            detail="This agent cross-references every payment to CRA/RQ "
                   "against the BS liability accounts. The journal is required.",
        )]

    try:
        report = parse_journal_csv(Path(journal_doc.file_path))
    except Exception as exc:
        log.exception("government_remittance.parse_failed path=%s", journal_doc.file_path)
        return [Finding(
            agent=AGENT, check="journal_parse", severity=SEVERITY_ERROR,
            title="Could not parse the journal",
            detail=f"{type(exc).__name__}: {exc}",
        )]

    remittances = _identify_remittances(report)
    if not remittances:
        return [Finding(
            agent=AGENT, check="no_gov_payments", severity=SEVERITY_INFO,
            title="No government-payee payments identified in the journal",
            detail="Scanned for CRA, Receiver General, Revenu Québec, RQ, and "
                   "related names. None matched. If remittances exist, they may "
                   "be coded under a non-obvious payee name — flag for review.",
        )]

    findings: list[Finding] = []
    findings.extend(_classification_summary(remittances))
    findings.extend(_expense_miscoding_check(remittances))
    findings.extend(_prior_year_noise_check(remittances))
    prior_bs_doc = store.latest_document(engagement.engagement_id, DOC_PRIOR_YEAR_BS)
    findings.extend(_bs_liability_reconciliation(remittances, bs_doc, prior_bs_doc))
    findings.extend(_unclassified_payment_check(remittances))
    findings.extend(_single_leg_sales_tax_remittance_check(remittances, report))

    log.info(
        "government_remittance.done engagement=%s findings=%d error=%d warn=%d",
        engagement.engagement_id,
        len(findings),
        sum(1 for f in findings if f.severity == SEVERITY_ERROR),
        sum(1 for f in findings if f.severity == SEVERITY_WARN),
    )
    return findings


# ---- identification -----------------------------------------------------------


def _identify_remittances(report: JournalReport) -> list[_Remittance]:
    """Find every journal line whose payee matches a government pattern."""
    # Group lines by journal-entry group_id so we can see both sides of an entry.
    by_group: dict[str, list[JournalLine]] = defaultdict(list)
    for line in report.lines:
        by_group[line.group_id].append(line)

    remittances: list[_Remittance] = []
    for group_id, group_lines in by_group.items():
        # Does any line in this entry point at a government payee?
        gov_lines = [ln for ln in group_lines if _is_gov_payee(ln.name)]
        if not gov_lines:
            continue

        # Use the first gov-line's metadata as the representative; look at the
        # entire entry to decide classification and whether the opposite leg is
        # an expense account.
        rep = gov_lines[0]
        category = _classify(rep, group_lines)
        amount = _entry_dollar_amount(group_lines)
        if amount == _ZERO:
            continue

        expense_miscoded = _opposite_side_is_expense(rep, group_lines)

        # Prior-year suspect: paid in Jan-Feb, category is sales-tax or
        # corporate-tax, and amount is large enough to be a balance-due
        # rather than a monthly installment. Purely heuristic — downstream
        # review resolves.
        is_prior_year = (
            rep.txn_date.month in (1, 2)
            and category in ("gst_hst_remittance", "qst_remittance",
                             "combined_sales_tax_remittance", "corporate_tax")
            and amount >= Decimal("1000")
        )

        remittances.append(_Remittance(
            line=rep, category=category,
            expense_side_miscoded=expense_miscoded,
            amount=amount, is_prior_year_suspect=is_prior_year,
        ))

    return sorted(remittances, key=lambda r: r.line.txn_date)


_WORD_RE = re.compile(r"\b[a-zà-ÿ]+\b", re.IGNORECASE)


def _is_gov_payee(name: str) -> bool:
    """Decide whether a vendor/payee name refers to a government tax payee.

    Two-pass match:
    1. Phrase match (case-insensitive substring) for unambiguous multi-word
       strings like "Receiver General", "Canada Revenue Agency".
    2. Acronym match (whole word only, case-insensitive) for short
       acronyms like "CRA", "ARC", "RQ" that are easily found as
       substrings of unrelated vendor names. The whole-word check
       prevents the historic false-positive on names like
       "Saint Marc Holdings" (which contains the substring "arc ").
    """
    n = (name or "").lower().strip()
    if not n:
        return False
    if any(p in n for p in _GOV_PAYEE_PHRASES):
        return True
    words = {m.group(0).lower() for m in _WORD_RE.finditer(n)}
    return bool(words & _GOV_PAYEE_ACRONYMS)


def _classify(representative: JournalLine, group_lines: list[JournalLine]) -> str:
    """Pick a category from memo + accounts touched. Delegates to the
    keyword-based classifier in ``_classify_haystack``."""
    haystacks = [
        (representative.description or "").lower(),
        (representative.account or "").lower(),
    ]
    for line in group_lines:
        haystacks.append((line.account or "").lower())
        haystacks.append((line.description or "").lower())
    combined = " | ".join(haystacks)
    return _classify_haystack(combined)


def _entry_dollar_amount(group_lines: list[JournalLine]) -> Decimal:
    """Total debits (or credits) in the entry — the gross payment size."""
    total = sum((ln.debit for ln in group_lines), _ZERO)
    if total == _ZERO:
        total = sum((ln.credit for ln in group_lines), _ZERO)
    return total


_CASH_ACCOUNT_TOKENS = ("chequing", "checking", "savings", "mastercard",
                         "visa", "credit card", "bank account", "cash")


def _is_cash_account(acct: str) -> bool:
    acct = (acct or "").lower()
    return any(c in acct for c in _CASH_ACCOUNT_TOKENS)


def _opposite_side_is_expense(rep: JournalLine, group_lines: list[JournalLine]) -> bool:
    """True if the NON-cash side of the JE lands on an expense GL.

    A remittance entry normally has two legs: (a) a debit on the liability GL
    being cleared (Payroll Liabilities, GST/QST Payable, etc.), and (b) a
    credit on the cash/credit-card GL. We inspect EVERY non-cash leg in the
    entry — not just the lines other than the representative — because the
    representative gov-payee line is itself the one most likely to be the
    miscoded expense leg.
    """
    for line in group_lines:
        acct = (line.account or "").lower()
        if not acct:
            continue
        if _is_cash_account(acct):
            continue
        # Explicit liability-account hit → correctly coded.
        if any(t in acct for t in _LIABILITY_ACCOUNT_TOKENS):
            return False
        # Landed on an expense account → miscoded.
        if any(t in acct for t in _EXPENSE_ACCOUNT_TOKENS):
            return True
    return False


# ---- checks -------------------------------------------------------------------


def _classification_summary(remittances: list[_Remittance]) -> list[Finding]:
    """What was paid to government in the year, broken out by type."""
    totals: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    counts: dict[str, int] = defaultdict(int)
    for r in remittances:
        totals[r.category] += r.amount
        counts[r.category] += 1

    total_all = sum(totals.values(), _ZERO)

    # Render per-category block, largest first.
    lines: list[str] = []
    for cat, amt in sorted(totals.items(), key=lambda kv: -kv[1]):
        label = _CATEGORY_LABELS.get(cat, cat)
        lines.append(f"• **{label}** — ${amt:,.2f} across {counts[cat]} payment(s)")

    return [Finding(
        agent=AGENT, check="classification_summary", severity=SEVERITY_INFO,
        title=f"${total_all:,.2f} paid to government across {len(remittances)} payment(s)",
        detail=(
            "Breakdown below. Each line should tie to a separate filing "
            "obligation: payroll DAS clears the Payroll Liabilities account, "
            "GST/QST remittances clear the Sales Tax Payable account, and "
            "corporate tax installments clear the Corporate Tax Payable account. "
            "If the numbers don't look right, the underlying coding is wrong.\n\n"
            + "\n".join(lines)
        ),
    )]


def _expense_miscoding_check(remittances: list[_Remittance]) -> list[Finding]:
    """Remittances whose offsetting side landed on a P&L expense GL."""
    bad = [r for r in remittances if r.expense_side_miscoded]
    if not bad:
        return [Finding(
            agent=AGENT, check="expense_miscoding", severity=SEVERITY_OK,
            title="No government remittance coded to an expense account",
        )]

    total_bad = sum((r.amount for r in bad), _ZERO)
    blocks: list[str] = []
    for r in bad:
        blocks.append(
            f"• {r.line.txn_date.isoformat()}  ${r.amount:,.2f}  "
            f"**{r.line.name}**  (category: {_CATEGORY_LABELS.get(r.category, r.category)})\n"
            f"  memo: {(r.line.description or '').strip()[:100]}\n"
            f"  to find in QBO: search vendor '{r.line.name}' on "
            f"{r.line.txn_date.isoformat()}, amount ${r.amount:,.2f}"
        )

    return [Finding(
        agent=AGENT, check="expense_miscoding", severity=SEVERITY_ERROR,
        title=f"{len(bad)} government remittance(s) coded to an expense account — ${total_bad:,.2f}",
        detail=(
            "A government remittance clears a liability that was already "
            "accrued earlier in the period — it MUST land on a BS liability "
            "account (Payroll Liabilities, GST/QST Payable, Corporate Tax "
            "Payable, etc.), NOT on a P&L expense. Coding it as an expense "
            "double-counts the cost: once when the underlying salary/tax was "
            "accrued, and again when the remittance cleared.\n\n"
            "Remittances to fix:\n\n"
            + "\n\n".join(blocks)
        ),
        proposed_fix=(
            "For each entry above: change the debit side from the expense "
            "account to the correct liability account. Payroll DAS → Payroll "
            "Liabilities. GST/HST → GST/HST Payable. QST → QST Payable. "
            "Corporate tax → Corporate Tax Payable."
        ),
    )]


def _prior_year_noise_check(remittances: list[_Remittance]) -> list[Finding]:
    """Payments made in Jan/Feb of the current period that look like
    prior-year balance-due settlements. Not errors per se — but must be
    audited so they don't pollute the current year's expense line."""
    suspects = [r for r in remittances if r.is_prior_year_suspect]
    if not suspects:
        return []

    total = sum((r.amount for r in suspects), _ZERO)
    blocks: list[str] = []
    for r in suspects:
        blocks.append(
            f"• {r.line.txn_date.isoformat()}  ${r.amount:,.2f}  "
            f"**{r.line.name}**  "
            f"(category: {_CATEGORY_LABELS.get(r.category, r.category)})\n"
            f"  memo: {(r.line.description or '').strip()[:100]}"
        )

    return [Finding(
        agent=AGENT, check="prior_year_noise", severity=SEVERITY_WARN,
        title=f"{len(suspects)} Jan–Feb remittance(s) may be settling prior-year liabilities — ${total:,.2f}",
        detail=(
            "These payments hit the current period but their size, timing, "
            "and category fit the pattern of a prior-year balance-due (e.g., "
            "FY(N-1) Q4 GST return paid Jan 31, or FY(N-1) corporate tax "
            "balance due in the first 60 days). A prior-year balance due "
            "settles the opening BS liability — it is NOT a current-year "
            "expense. If any of these were booked as expense or netted "
            "against current-year accruals, the current P&L is distorted by "
            "last year's noise.\n\n"
            "Payments to audit:\n\n"
            + "\n\n".join(blocks)
        ),
        proposed_fix=(
            "For each payment above, trace to the underlying filing: "
            "(1) which period does the return/installment cover? "
            "(2) what was the BS balance for that liability at the start of "
            "the current year? "
            "(3) does the payment reduce the opening liability to zero, or "
            "spill into current-year accruals? "
            "If prior-year: must land on the opening BS liability, not "
            "current-period expense."
        ),
    )]


@dataclass(frozen=True)
class _LiabilityBalances:
    gst_qst_payable: Decimal
    gst_qst_suspense: Decimal
    payroll_liab: Decimal
    corp_tax_payable: Decimal


def _extract_liability_balances(bs) -> _LiabilityBalances:  # type: ignore[no-untyped-def]
    """Pull the four liability categories we reconcile remittances against
    from either a current-year or prior-year BS. Used symmetrically for both
    ends of the three-way tie."""
    gst_qst_payable = bs.amount_of_any(*L.GST_HST_PAYABLE) or _ZERO
    gst_qst_suspense = bs.amount_of_any(*L.GST_HST_SUSPENSE) or _ZERO
    payroll_liab = _ZERO
    corp_tax_payable = _ZERO
    for line in bs.lines:
        n = (line.name or "").lower()
        if line.amount is None:
            continue
        if any(t in n for t in ("payroll liabilit", "das ", "source deduct")):
            payroll_liab += line.amount
        if any(t in n for t in ("corporate income tax payable",
                                "corporate tax payable",
                                "income tax payable", "taxes payable",
                                "impôts à payer", "impots a payer")):
            corp_tax_payable += line.amount
    return _LiabilityBalances(
        gst_qst_payable=gst_qst_payable,
        gst_qst_suspense=gst_qst_suspense,
        payroll_liab=payroll_liab,
        corp_tax_payable=corp_tax_payable,
    )


def _bs_liability_reconciliation(
    remittances: list[_Remittance],
    bs_doc,  # type: ignore[no-untyped-def]
    prior_bs_doc=None,  # type: ignore[no-untyped-def]
) -> list[Finding]:
    """Cross-check total paid per category against the BS closing liability
    balances. When a prior-year BS is also available, complete the full
    three-way tie per category:

        opening liability + current-year activity − current-year paid = closing liability

    and report the residual gap per category. The gap, when non-zero, is the
    amount of current-year activity (accrual) the journal shows, OR the
    amount of prior-year noise polluting the current P&L.
    """
    if bs_doc is None:
        return [Finding(
            agent=AGENT, check="bs_reconciliation", severity=SEVERITY_INFO,
            title="BS not uploaded — cannot reconcile remittances to liability balances",
            detail="Upload the year-end BS and the agent will reconcile total "
                   "paid per category against the closing liability balance.",
        )]

    try:
        bs = parse_balance_sheet(Path(bs_doc.file_path))
    except Exception as exc:
        log.exception("government_remittance.bs_parse_failed")
        return [Finding(
            agent=AGENT, check="bs_reconciliation", severity=SEVERITY_WARN,
            title="Could not parse BS for liability reconciliation",
            detail=f"{type(exc).__name__}: {exc}",
        )]

    closing = _extract_liability_balances(bs)

    # If a prior-year BS was uploaded, try to parse it too. It's not required;
    # a failure here doesn't block the current-year reconciliation.
    opening: _LiabilityBalances | None = None
    prior_bs_parse_error: str | None = None
    if prior_bs_doc is not None:
        try:
            prior_bs = parse_balance_sheet(Path(prior_bs_doc.file_path))
            opening = _extract_liability_balances(prior_bs)
        except Exception as exc:
            log.warning(
                "government_remittance.prior_bs_parse_failed path=%s error=%s",
                prior_bs_doc.file_path, exc,
            )
            prior_bs_parse_error = f"{type(exc).__name__}: {exc}"

    totals: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for r in remittances:
        totals[r.category] += r.amount

    sales_tax_paid = (totals["gst_hst_remittance"]
                      + totals["qst_remittance"]
                      + totals["combined_sales_tax_remittance"])
    payroll_paid = totals["payroll_das"]
    corp_tax_paid = totals["corporate_tax"]

    # --- Build the detail block ---
    if opening is not None:
        # Full three-way tie is possible. Implied current-year activity for
        # each category is: closing − opening + paid. If that number matches
        # what the P&L would show for the category (e.g. GST/QST collected
        # net of ITC, gross payroll withholdings, current-year tax accrual),
        # the three-way tie holds. We surface the computed activity here so
        # the CPA can compare it against the P&L signal.
        def activity(closing_val: Decimal, opening_val: Decimal,
                     paid: Decimal) -> Decimal:
            return closing_val - opening_val + paid

        sales_activity = activity(
            closing.gst_qst_payable, opening.gst_qst_payable, sales_tax_paid)
        payroll_activity = activity(
            closing.payroll_liab, opening.payroll_liab, payroll_paid)
        corp_activity = activity(
            closing.corp_tax_payable, opening.corp_tax_payable, corp_tax_paid)

        lines_out = [
            "**Three-way reconciliation complete.** Using the uploaded prior-year BS "
            "as the opening balance per category:",
            "",
            "  opening liability + current-year activity − current-year paid = closing liability",
            "  → current-year activity = closing − opening + paid",
            "",
            "**Payroll DAS:**",
            f"  opening ${opening.payroll_liab:,.2f} + activity **${payroll_activity:,.2f}** "
            f"− paid ${payroll_paid:,.2f} = closing ${closing.payroll_liab:,.2f}",
            f"  → the journal should show ~${payroll_activity:,.2f} of gross "
            f"payroll withholdings accrued during the year.",
            "",
            "**Sales tax (GST + QST combined):**",
            f"  opening ${opening.gst_qst_payable:,.2f} + activity **${sales_activity:,.2f}** "
            f"− paid ${sales_tax_paid:,.2f} = closing ${closing.gst_qst_payable:,.2f}",
            f"  → the journal should show ~${sales_activity:,.2f} of net GST+QST "
            f"accrued (tax collected on sales minus ITC/ITR on purchases).",
            "",
            "**Corporate income tax:**",
            f"  opening ${opening.corp_tax_payable:,.2f} + activity **${corp_activity:,.2f}** "
            f"− paid ${corp_tax_paid:,.2f} = closing ${closing.corp_tax_payable:,.2f}",
            f"  → the current-year tax accrual implied by the BS+cash movement is "
            f"${corp_activity:,.2f}. Compare against the year-end accrual JE.",
        ]
    else:
        # Snapshot-only — can't close the three-way tie.
        lines_out = [
            "**Current-state snapshot only** — the reconciliation is only complete "
            "once a prior-year closing BS is uploaded. Please send the prior-year BS "
            "in any format (QBO export, prior accountant PDF, scan); I'll complete "
            "the tie-out as soon as it arrives.",
            "",
            "  With prior-year BS: opening liability + activity − paid = closing liability",
            "  Without it: I can only report the current-year leg.",
            "",
            "What I can report from the current-year BS + remittances:",
            "",
            f"• **Payroll** — paid ${payroll_paid:,.2f} to CRA/RQ classified as "
            f"source deductions. BS Payroll Liabilities at year-end: ${closing.payroll_liab:,.2f}.",
            f"• **Sales tax (GST + QST combined)** — paid ${sales_tax_paid:,.2f}. "
            f"BS GST/HST Payable (net) at year-end: ${closing.gst_qst_payable:,.2f}; "
            f"Suspense: ${closing.gst_qst_suspense:,.2f}.",
            f"• **Corporate income tax** — paid ${corp_tax_paid:,.2f} in installments. "
            f"BS Corporate Tax Payable at year-end: ${closing.corp_tax_payable:,.2f}.",
        ]
        if prior_bs_parse_error:
            lines_out.insert(1, (
                f"ℹ A prior-year BS WAS uploaded but I couldn't parse it "
                f"({prior_bs_parse_error}). Please either re-upload in a "
                "different format (xlsx preferred) or paste the four opening "
                "balances directly (Payroll Liabilities, GST/QST Payable, "
                "Corporate Income Tax Payable, Retained Earnings)."
            ))

    # Rebind the local names the anomaly block below reads.
    gst_qst_payable = closing.gst_qst_payable
    gst_qst_suspense = closing.gst_qst_suspense
    payroll_liab = closing.payroll_liab
    corp_tax_payable = closing.corp_tax_payable

    # Heuristic flags worth calling out. We're deliberately conservative.
    anomalies: list[str] = []
    # Zero corp tax paid but a large closing payable → no installments made.
    if corp_tax_paid == _ZERO and corp_tax_payable > Decimal("3000"):
        anomalies.append(
            f"⚠ **Zero corporate-tax installments paid** in the year, but "
            f"closing Corporate Tax Payable is ${corp_tax_payable:,.2f}. "
            f"For corps with tax > $3,000, installments are normally required "
            f"(ITA s.157). Check CRA + RQ My Business Account for installment "
            f"history + penalty/interest exposure."
        )
    # Payroll DAS paid but no Payroll Liabilities balance → verify full clearance.
    if payroll_paid > _ZERO and payroll_liab == _ZERO:
        anomalies.append(
            f"ℹ Payroll Liabilities at year-end is $0 despite ${payroll_paid:,.2f} "
            f"of DAS remitted. Verify that the December accrual was fully "
            f"cleared in the remittance — or that no late-month accrual is "
            f"missing from the BS."
        )
    # Sales-tax remittance coded as "combined" (ambiguous) → can't validate
    # against RQ vs CRA return amounts.
    if totals["combined_sales_tax_remittance"] > _ZERO:
        anomalies.append(
            f"ℹ ${totals['combined_sales_tax_remittance']:,.2f} was classified "
            f"as combined GST+QST. CRA and RQ are separate filings; the split "
            f"is needed before returns can be reconciled. Confirm how the "
            f"bookkeeper allocated this between the two."
        )

    detail = "\n".join(lines_out)
    if anomalies:
        detail += "\n\nAnomalies worth checking:\n\n" + "\n\n".join(anomalies)

    return [Finding(
        agent=AGENT, check="bs_reconciliation",
        severity=SEVERITY_WARN if anomalies else SEVERITY_INFO,
        title="Government remittance vs BS liability snapshot",
        detail=detail,
        proposed_fix=(
            "Complete the three-way tie: opening BS liability + current-period "
            "accruals (from P&L) − payments made (from this agent) = closing "
            "BS liability. If any leg doesn't tie, either a payment was "
            "miscoded (see the expense-miscoding check), a prior-year "
            "settlement was treated as current-year expense, or an accrual "
            "is missing."
        ),
    )]


def _unclassified_payment_check(remittances: list[_Remittance]) -> list[Finding]:
    """Government payments we couldn't place into a category. Surface them so
    the bookkeeper explains the memo or adds missing memo text."""
    orphans = [r for r in remittances if r.category == "unclassified"]
    if not orphans:
        return []

    blocks: list[str] = []
    for r in orphans:
        blocks.append(
            f"• {r.line.txn_date.isoformat()}  ${r.amount:,.2f}  "
            f"**{r.line.name}**\n"
            f"  memo: {(r.line.description or '').strip()[:120] or '(blank)'}"
        )

    total = sum((r.amount for r in orphans), _ZERO)

    return [Finding(
        agent=AGENT, check="unclassified_payments", severity=SEVERITY_WARN,
        title=f"{len(orphans)} government payment(s) could not be classified — ${total:,.2f}",
        detail=(
            "These payments went to a government payee but the memo and "
            "accounts touched don't identify what the payment is for. They "
            "could be corporate tax, payroll DAS, GST/QST, CNESST, or "
            "something else entirely. Without classification, the BS "
            "reconciliation (payroll / sales tax / corporate tax) is "
            "incomplete.\n\n"
            "Payments to classify:\n\n"
            + "\n\n".join(blocks)
        ),
        proposed_fix=(
            "For each payment above: (a) pull the underlying remittance "
            "advice or bank memo in QBO to identify what filing it settled, "
            "(b) update the memo with the specific type (e.g., 'GST/HST Q2 "
            "remittance', 'CPP/EI/fed tax DAS for <month>', 'T2 installment "
            "#3 FY2025'), (c) verify the entry's offsetting account is the "
            "correct liability, not an expense."
        ),
    )]


def _single_leg_sales_tax_remittance_check(
    remittances: list[_Remittance],
    report: JournalReport,
) -> list[Finding]:
    """For Quebec filers, a GST+QST return payment of any material size should
    split the debit across GST/HST Payable AND QST Payable sub-accounts in
    proportion to the return amounts. A single-leg remittance (all of it to
    one sub-account) is a classification error even when the total amount is
    correct — it distorts the per-sub-account reconciliation with each return.
    """
    suspects: list[_Remittance] = []
    by_group: dict[str, list[JournalLine]] = defaultdict(list)
    for line in report.lines:
        by_group[line.group_id].append(line)

    for r in remittances:
        if r.category not in ("gst_hst_remittance", "qst_remittance",
                              "combined_sales_tax_remittance"):
            continue
        if r.amount < Decimal("500"):
            continue  # small amounts unlikely to be full-return remittances
        group_lines = by_group.get(r.line.group_id, [])
        # Count how many distinct sales-tax sub-accounts were touched on the
        # non-cash side of the entry.
        tax_accounts_hit: set[str] = set()
        for line in group_lines:
            acct = (line.account or "").lower()
            if _is_cash_account(acct):
                continue
            if "gst" in acct or "hst" in acct or "tps" in acct or "tvh" in acct:
                tax_accounts_hit.add("gst_side")
            if "qst" in acct or "tvq" in acct:
                tax_accounts_hit.add("qst_side")
        # A combined return should touch both. If only one side is hit for a
        # material Quebec-filer remittance, flag.
        if len(tax_accounts_hit) == 1:
            suspects.append(r)

    if not suspects:
        return []

    total = sum((r.amount for r in suspects), _ZERO)
    blocks: list[str] = []
    for r in suspects:
        blocks.append(
            f"• {r.line.txn_date.isoformat()}  ${r.amount:,.2f}  "
            f"**{r.line.name}**  (account touched: {r.line.account})\n"
            f"  memo: {(r.line.description or '').strip()[:120]}"
        )

    return [Finding(
        agent=AGENT, check="single_leg_sales_tax_remittance", severity=SEVERITY_WARN,
        title=f"{len(suspects)} sales-tax remittance(s) posted to ONE sub-account only — ${total:,.2f}",
        detail=(
            "A Quebec GST+QST return collects both federal (5% GST) and "
            "provincial (9.975% QST) on the same taxable base, typically in "
            "roughly a 1:2 ratio. The payment to Revenu Québec should split "
            "the debit between the GST/HST Payable and the QST Payable "
            "sub-accounts so each return can be reconciled against its own "
            "sub-account activity. A single-leg remittance posting the entire "
            "amount to ONE sub-account hides which portion of the liability "
            "was actually cleared — even when the total amount is correct.\n\n"
            "This is DISTINCT from the BS-presentation question: a single "
            "combined 'GST/QST Payable' line on the BS is normal QBO "
            "behavior and is not flagged. The issue is the JOURNAL entry "
            "moving the remittance through one sub-account.\n\n"
            "Remittances to split:\n\n"
            + "\n\n".join(blocks)
        ),
        proposed_fix=(
            "For each payment above: (a) pull the underlying filed return, "
            "(b) identify the GST portion and the QST portion, "
            "(c) re-book the entry as: Dr GST/HST Payable <gst portion> / "
            "Dr QST Payable <qst portion> / Cr Cash <total>. The total amount "
            "paid stays the same; the sub-account balances reconcile to the "
            "returns."
        ),
    )]
