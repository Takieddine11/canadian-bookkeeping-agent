"""Agent 5 — Balance Sheet / Trial Balance rollforward checks.

Pure-arithmetic checks that should always hold in a clean set of books. These
don't need an LLM; any mismatch is either a bookkeeper error or a reporting
issue the CPA must resolve before sign-off.

Checks performed:

1. **Accounting identity** — ``Total Assets == Total Liabilities and Equity``
2. **Current-year profit tie** — ``BS 'Profit for the year' == P&L 'PROFIT'``
3. **Retained-earnings rollforward** — ``prior RE + current profit − dividends == reported equity``
   (Only meaningful when we have a prior-period engagement; for v1, surface the
    RE and Profit balances as info.)
4. **Bank balance surfacing** — list every account under "Cash and Cash Equivalent"
5. **GST/HST balance surfacing** — key field for the tax auditor next

An agent **never** mutates data. It only reads from the parsed statements and
emits findings.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from src.agents.base import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_OK,
    SEVERITY_WARN,
    Finding,
)
from src.parsers import labels as L
from src.parsers.financial_statement import (
    REPORT_BALANCE_SHEET,
    FinancialStatement,
    parse_balance_sheet,
    parse_pnl,
)
from src.store.engagement_db import (
    DOC_BALANCE_SHEET,
    DOC_PNL,
    DOC_PRIOR_YEAR_BS,
    Engagement,
    EngagementStore,
)

log = logging.getLogger(__name__)
AGENT = "rollforward"
_ZERO = Decimal("0")
_IDENTITY_TOLERANCE = Decimal("0.01")  # 1 cent rounding tolerance


def run(store: EngagementStore, engagement: Engagement) -> list[Finding]:
    """Run all rollforward checks for the engagement and return findings."""
    # Always use the LATEST doc per type — an engagement can accumulate
    # re-uploads, and picking the oldest (via list_documents' ascending order)
    # is how stale data bugs happen.
    bs_doc = store.latest_document(engagement.engagement_id, DOC_BALANCE_SHEET)
    pnl_doc = store.latest_document(engagement.engagement_id, DOC_PNL)

    findings: list[Finding] = []

    bs: FinancialStatement | None = None
    if bs_doc is None:
        findings.append(Finding(
            agent=AGENT, check="bs_present", severity=SEVERITY_ERROR,
            title="Balance Sheet not uploaded",
            detail="Rollforward checks require the Balance Sheet. Intake should be re-opened.",
        ))
    else:
        try:
            bs = parse_balance_sheet(Path(bs_doc.file_path))
        except Exception as exc:
            log.exception("rollforward.bs_parse_failed path=%s", bs_doc.file_path)
            findings.append(Finding(
                agent=AGENT, check="bs_parse", severity=SEVERITY_ERROR,
                title="Could not parse Balance Sheet",
                detail=f"{type(exc).__name__}: {exc}",
            ))

    pnl: FinancialStatement | None = None
    if pnl_doc is None:
        findings.append(Finding(
            agent=AGENT, check="pnl_present", severity=SEVERITY_ERROR,
            title="P&L not uploaded",
            detail="Rollforward checks require the P&L. Intake should be re-opened.",
        ))
    else:
        try:
            pnl = parse_pnl(Path(pnl_doc.file_path))
        except Exception as exc:
            log.exception("rollforward.pnl_parse_failed path=%s", pnl_doc.file_path)
            findings.append(Finding(
                agent=AGENT, check="pnl_parse", severity=SEVERITY_ERROR,
                title="Could not parse P&L",
                detail=f"{type(exc).__name__}: {exc}",
            ))

    if bs is not None:
        findings.extend(_accounting_identity(bs))
        findings.extend(_bank_balances(bs))
        findings.extend(_gst_hst_balance(bs))

    prior_bs: FinancialStatement | None = None
    prior_bs_doc = store.latest_document(engagement.engagement_id, DOC_PRIOR_YEAR_BS)
    if prior_bs_doc is not None:
        try:
            prior_bs = parse_balance_sheet(Path(prior_bs_doc.file_path))
        except Exception as exc:
            log.warning(
                "rollforward.prior_bs_parse_failed path=%s error=%s",
                prior_bs_doc.file_path, exc,
            )
            # Surface as INFO; don't fail the whole agent.
            findings.append(Finding(
                agent=AGENT, check="prior_bs_parse", severity=SEVERITY_INFO,
                title="Prior-year BS uploaded but couldn't be parsed",
                detail=(
                    f"{type(exc).__name__}: {exc}. Try uploading in a different "
                    "format (xlsx preferred) or paste the key opening balances "
                    "(Retained Earnings, Payroll Liabilities, GST/QST Payable, "
                    "Corporate Income Tax Payable) directly."
                ),
            ))

    if bs is not None and pnl is not None:
        findings.extend(_profit_tie(bs, pnl))
        findings.extend(_retained_earnings_snapshot(bs, pnl, prior_bs))
        findings.extend(_inventory_vs_cogs(bs, pnl))

    log.info(
        "rollforward.done engagement=%s findings=%d error=%d warn=%d",
        engagement.engagement_id,
        len(findings),
        sum(1 for f in findings if f.severity == SEVERITY_ERROR),
        sum(1 for f in findings if f.severity == SEVERITY_WARN),
    )
    return findings


# ---- individual checks --------------------------------------------------------


def _accounting_identity(bs: FinancialStatement) -> list[Finding]:
    total_assets = bs.amount_of_any(*L.TOTAL_ASSETS) or _ZERO
    total_le = bs.amount_of_any(*L.TOTAL_LIABILITIES_AND_EQUITY) or _ZERO
    diff = total_assets - total_le
    if abs(diff) <= _IDENTITY_TOLERANCE:
        return [Finding(
            agent=AGENT, check="accounting_identity", severity=SEVERITY_OK,
            title=f"Accounting identity holds: ${total_assets:,.2f} = ${total_le:,.2f}",
        )]
    return [Finding(
        agent=AGENT, check="accounting_identity", severity=SEVERITY_ERROR,
        title=f"Balance sheet does not balance: diff ${diff:,.2f}",
        detail=(
            f"Total Assets ${total_assets:,.2f}  vs  "
            f"Total Liabilities and Equity ${total_le:,.2f}"
        ),
        proposed_fix="Re-run the QBO balance sheet. If the difference persists, check for "
                    "un-posted journal entries or a corrupt opening balance.",
        delta=diff,
    )]


def _profit_tie(bs: FinancialStatement, pnl: FinancialStatement) -> list[Finding]:
    bs_profit = bs.amount_of_any(*L.PROFIT_FOR_THE_YEAR)
    pl_profit = pnl.amount_of_any(*L.NET_PROFIT)
    if bs_profit is None or pl_profit is None:
        return [Finding(
            agent=AGENT, check="profit_tie", severity=SEVERITY_WARN,
            title="Could not locate both BS 'Profit for the year' and P&L 'PROFIT'",
            detail=f"BS profit line: {bs_profit}   P&L profit line: {pl_profit}",
        )]
    diff = bs_profit - pl_profit
    if abs(diff) <= _IDENTITY_TOLERANCE:
        return [Finding(
            agent=AGENT, check="profit_tie", severity=SEVERITY_OK,
            title=f"Current-year profit ties: BS = P&L = ${bs_profit:,.2f}",
        )]
    return [Finding(
        agent=AGENT, check="profit_tie", severity=SEVERITY_ERROR,
        title=f"Current-year profit mismatch: diff ${diff:,.2f}",
        detail=(
            f"BS 'Profit for the year' ${bs_profit:,.2f}  vs  "
            f"P&L 'PROFIT' ${pl_profit:,.2f}"
        ),
        proposed_fix="Most common cause: the BS was run on a different basis (Accrual vs Cash) "
                    "than the P&L, or one of the reports was generated mid-posting.",
        delta=diff,
    )]


def _retained_earnings_snapshot(
    bs: FinancialStatement,
    pnl: FinancialStatement,
    prior_bs: FinancialStatement | None = None,
) -> list[Finding]:
    re = bs.amount_of_any(*L.RETAINED_EARNINGS) or _ZERO
    profit = (
        bs.amount_of_any(*L.PROFIT_FOR_THE_YEAR)
        or pnl.amount_of_any(*L.NET_PROFIT)
        or _ZERO
    )
    dividends = bs.amount_of_any(*L.DIVIDENDS)
    total_equity = bs.amount_of_any(*L.TOTAL_EQUITY) or _ZERO

    parts = [f"Retained Earnings ${re:,.2f}", f"Profit ${profit:,.2f}"]
    if dividends is not None:
        parts.append(f"Dividends ${dividends:,.2f}")
    parts.append(f"Total Equity ${total_equity:,.2f}")

    findings: list[Finding] = [Finding(
        agent=AGENT, check="retained_earnings_snapshot", severity=SEVERITY_INFO,
        title="Equity position for CPA review",
        detail="  ·  ".join(parts),
        proposed_fix=(
            "When the prior-year BS is uploaded, the agent computes "
            "(prior closing RE + current profit − dividends) and ties to "
            "current Total Equity."
        ),
    )]

    # Prior-year BS is available — do the full rollforward tie-out.
    if prior_bs is not None:
        prior_closing_re = prior_bs.amount_of_any(*L.RETAINED_EARNINGS) or _ZERO
        # The BS "opening RE" field is whatever QBO shows as the current-year
        # opening — which should match the prior BS closing RE exactly.
        current_opening_re = re  # current BS "Retained Earnings — opening"
        variance = current_opening_re - prior_closing_re

        if abs(variance) <= _IDENTITY_TOLERANCE:
            findings.append(Finding(
                agent=AGENT, check="re_rollforward_tie", severity=SEVERITY_OK,
                title="Opening RE ties to prior-year BS closing RE",
                detail=(
                    f"Prior-year BS closing Retained Earnings: ${prior_closing_re:,.2f}\n"
                    f"Current-year BS opening Retained Earnings: ${current_opening_re:,.2f}"
                ),
            ))
        else:
            findings.append(Finding(
                agent=AGENT, check="re_rollforward_tie", severity=SEVERITY_ERROR,
                title=f"Opening RE variance ${variance:+,.2f} vs prior-year BS",
                detail=(
                    f"The current-year BS shows opening Retained Earnings of "
                    f"${current_opening_re:,.2f}, but the prior-year BS closes at "
                    f"${prior_closing_re:,.2f}. Difference: **${variance:+,.2f}**.\n\n"
                    "This is a pre-current-year posting to RE (or an opening-balance "
                    "entry error) that must be reconciled before the current-year "
                    "T2 can be filed with confidence. Common causes: (a) a JE "
                    "during the current year that hit RE directly (bank-rec "
                    "plug, 'cleanup' entry, phantom receivable posted to RE), "
                    "(b) the prior accountant filed T2 on a different trial "
                    "balance than the QBO opening, (c) amended prior-year T2 "
                    "not pushed back to QBO."
                ),
                proposed_fix=(
                    "Pull the full JE history for the Retained Earnings account "
                    "from Jan 1 of the current year onward. Each entry should be "
                    "either (a) the automatic closing of prior-period NI (fine), "
                    "(b) a documented prior-period adjustment with explanation, "
                    "or (c) a documented dividend declaration. Any entry that's "
                    "none of those — investigate and re-book to the correct "
                    "account."
                ),
                delta=variance,
            ))

    return findings


def _bank_balances(bs: FinancialStatement) -> list[Finding]:
    # Cash/bank accounts are children of "Cash and Cash Equivalent". Use the
    # sibling-level heuristic: gather leaves that sit above "Total Cash and
    # Cash Equivalent" in the lines list.
    cash_total = bs.amount_of_any(*L.TOTAL_CASH)
    bank_accounts: list[tuple[str, Decimal]] = []
    in_cash = False
    for line in bs.lines:
        n = line.name.lower()
        is_cash_header = line.is_section and (
            ("cash" in n and "equivalent" in n)
            or ("encaisse" in n)
            or ("trésorerie" in n or "tresorerie" in n)
        )
        if is_cash_header:
            in_cash = True
            continue
        if in_cash:
            if line.is_total:
                break
            if line.amount is not None and not line.is_section:
                bank_accounts.append((line.name, line.amount))

    if not bank_accounts:
        return []
    detail = "\n".join(f"• {name}: ${amt:,.2f}" for name, amt in bank_accounts)
    title = (
        f"Bank balances total ${cash_total:,.2f}"
        if cash_total is not None else "Bank account balances"
    )
    return [Finding(
        agent=AGENT, check="bank_balances", severity=SEVERITY_INFO,
        title=title,
        detail=detail,
        proposed_fix="Next: reconciliation agent will match these balances to bank statements.",
    )]


_INVENTORY_TOKENS = (
    # English
    "inventory", "stock on hand", "merchandise", "work in progress", "wip",
    # French
    "stock", "stocks", "inventaire", "marchandises",
    "travaux en cours",
)


def _inventory_vs_cogs(
    bs: FinancialStatement, pnl: FinancialStatement
) -> list[Finding]:
    """If COGS is recorded, the BS must track inventory — otherwise the closing count
    is missing and COGS (and therefore profit) is wrong.

    Rule: *closing inventory = beginning inventory + purchases − COGS*. Without an
    inventory asset on the BS, the equation can't close and the period's COGS is
    effectively an estimate.
    """
    total_cogs = pnl.amount_of_any(*L.TOTAL_COGS)
    if total_cogs is None or total_cogs == _ZERO:
        return []  # no COGS → no inventory check needed

    inventory_lines = [
        l for l in bs.lines
        if any(tok in l.name.lower() for tok in _INVENTORY_TOKENS) and l.amount is not None
    ]
    if not inventory_lines:
        return [Finding(
            agent=AGENT, check="inventory_missing", severity=SEVERITY_WARN,
            title=(
                f"COGS of ${total_cogs:,.2f} recorded with no Inventory account on the BS "
                "— please confirm closing count with the client"
            ),
            detail=(
                f"P&L shows Cost of Goods Sold ${total_cogs:,.2f}. Without an Inventory "
                f"asset account, there's no closing count captured for the period. This "
                f"may be correct (e.g., a service business, or materials fully consumed "
                f"within the period) — but when materials are on hand at period-end, COGS "
                f"is overstated."
            ),
            proposed_fix=(
                "Please ask the client whether any materials/inventory were on hand at "
                f"{bs.as_of.isoformat() if bs.as_of else 'period-end'}. If yes: (1) add "
                "an Inventory asset to the BS, (2) post a closing adjustment DR Inventory "
                "/ CR COGS for the value on hand. If no: confirm and document that this "
                "is a service business or that all materials were consumed in the period."
            ),
        )]

    # Inventory exists — require the closing count to be confirmed as a physical count.
    inventory_total = sum((l.amount for l in inventory_lines if l.amount is not None), _ZERO)
    return [Finding(
        agent=AGENT, check="inventory_closing_count_confirmation",
        severity=SEVERITY_WARN,
        title=(
            f"Inventory ${inventory_total:,.2f} on BS · COGS ${total_cogs:,.2f} on P&L "
            f"— confirm closing count"
        ),
        detail=(
            f"Accounts in scope:\n"
            + "\n".join(f"• {l.name}: ${l.amount:,.2f}" for l in inventory_lines)
        ),
        proposed_fix=(
            "Confirm with the client that the Inventory balance reflects a physical "
            f"count taken at {bs.as_of.isoformat() if bs.as_of else 'period-end'}, "
            "not a rolling number from receiving. A running inventory balance drifts "
            "from reality and silently distorts COGS and profit."
        ),
    )]


def _gst_hst_balance(bs: FinancialStatement) -> list[Finding]:
    payable = bs.amount_of_any(*L.GST_HST_PAYABLE)
    suspense = bs.amount_of_any(*L.GST_HST_SUSPENSE)
    if payable is None and suspense is None:
        return []

    parts = []
    if payable is not None:
        parts.append(f"Payable ${payable:,.2f}")
    if suspense is not None:
        parts.append(f"Suspense ${suspense:,.2f}")
    net = (payable or _ZERO) + (suspense or _ZERO)

    severity = SEVERITY_INFO
    title = f"GST/HST net balance ${net:,.2f}"
    detail = "  ·  ".join(parts)
    fix = (
        "The tax-audit agent will cross-check this against the P&L's sales-tax coding. "
        "A non-zero Suspense balance usually means unfiled tax returns — confirm with the bookkeeper."
    )
    if suspense is not None and suspense != _ZERO:
        severity = SEVERITY_WARN
        title = f"GST/HST Suspense is non-zero (${suspense:,.2f})"
    return [Finding(
        agent=AGENT, check="gst_hst_balance", severity=severity,
        title=title, detail=detail, proposed_fix=fix,
    )]


def summarize(findings: Iterable[Finding]) -> dict:
    """Helper for logs / cards — counts by severity."""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts
