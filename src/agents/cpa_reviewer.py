"""Agent 4 — CPA Reviewer (deterministic v1).

Synthesizes findings from earlier agents (Rollforward, Reconciliation, Tax Auditor)
into a single review memo. A later v2 layer will wrap this in a call to Claude Opus
with the ``corp-taxprep-brain`` knowledge as system context to:

* filter false positives using CPA judgment,
* draft proposed adjusting journal entries,
* rewrite findings in professional prose for the client-facing memo.

For now, v1 is deterministic: it groups findings by severity, produces an executive
summary, and lists action items for the CPA to review before sign-off.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from pydantic import BaseModel, Field

from src.agents.base import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_OK,
    SEVERITY_WARN,
    Finding,
    sort_findings,
)
from src.store.engagement_db import Engagement

log = logging.getLogger(__name__)
AGENT = "cpa_reviewer"

LLM_MODEL = "claude-opus-4-7"
LLM_MAX_TOKENS = 16000

_AGENT_DISPLAY = {
    "rollforward": "Rollforward (Agent 5)",
    "reconciliation": "Reconciliation (Agent 3)",
    "tax_auditor": "Sales tax (Agent 2)",
    "cpa_reviewer": "CPA review (Agent 4)",
}


@dataclass(frozen=True)
class Memo:
    engagement_id: str
    company: str
    period: str
    total_checks: int
    n_errors: int
    n_warnings: int
    n_info: int
    n_ok: int
    executive_summary: str
    actions_required: list[str]   # error-severity items, rendered as markdown lines
    recommend_review: list[str]   # warn-severity
    context: list[str]            # info-severity, abbreviated
    sign_off_ready: bool          # True when no errors


def build_memo(
    engagement: Engagement,
    all_findings: list[Finding],
    *,
    company: str | None = None,
) -> Memo:
    """Aggregate every agent's findings into a CPA-ready memo object."""
    sorted_f = sort_findings(all_findings)

    n_err = sum(1 for f in sorted_f if f.severity == SEVERITY_ERROR)
    n_warn = sum(1 for f in sorted_f if f.severity == SEVERITY_WARN)
    n_info = sum(1 for f in sorted_f if f.severity == SEVERITY_INFO)
    n_ok = sum(1 for f in sorted_f if f.severity == SEVERITY_OK)

    actions = [
        _format_line(f) for f in sorted_f if f.severity == SEVERITY_ERROR
    ]
    recommend = [
        _format_line(f) for f in sorted_f if f.severity == SEVERITY_WARN
    ]
    context = [
        f"• **{_AGENT_DISPLAY.get(f.agent, f.agent)}** — {f.title}"
        for f in sorted_f if f.severity == SEVERITY_INFO
    ]

    sign_off_ready = n_err == 0

    if n_err > 0:
        exec_line = (
            f"**Not ready for CPA sign-off.** {n_err} blocking issue"
            + ("s" if n_err > 1 else "")
            + f" must be resolved before approval."
        )
    elif n_warn > 0:
        exec_line = (
            f"**Clean of errors** but {n_warn} warning"
            + ("s" if n_warn > 1 else "")
            + " warrant CPA judgment. Approve after review."
        )
    else:
        exec_line = "**Ready for CPA sign-off.** No errors or warnings detected."

    return Memo(
        engagement_id=engagement.engagement_id,
        company=company or engagement.client_id or "(client)",
        period=engagement.period_description or "(period)",
        total_checks=len(sorted_f),
        n_errors=n_err,
        n_warnings=n_warn,
        n_info=n_info,
        n_ok=n_ok,
        executive_summary=exec_line,
        actions_required=actions,
        recommend_review=recommend,
        context=context,
        sign_off_ready=sign_off_ready,
    )


def _format_line(f: Finding) -> str:
    agent = _AGENT_DISPLAY.get(f.agent, f.agent)
    line = f"• **{agent}** — {f.title}"
    if f.proposed_fix:
        # Keep the fix short in the memo (full detail still sits in the per-agent card).
        fix_short = f.proposed_fix.split("\n")[0]
        if len(fix_short) > 140:
            fix_short = fix_short[:137] + "…"
        line += f"\n  _Fix:_ {fix_short}"
    return line


# ---- LLM synthesis layer (optional; requires ANTHROPIC_API_KEY) --------------


class LlmAdjustingEntry(BaseModel):
    """One proposed adjusting journal entry drafted by the LLM reviewer."""

    debit_account: str = Field(description="GL account to debit")
    credit_account: str = Field(description="GL account to credit")
    amount: str = Field(description="Amount as a decimal string, e.g. '1234.56'")
    description: str = Field(description="Memo explaining the entry")


class LlmFinding(BaseModel):
    """One item in the review memo — blocking issue or judgment note.

    Each finding carries a priority rank, a responsible role, and a
    plain-language action line aimed at that role. A bookkeeper scanning
    the card should be able to filter to their role and see only what
    they should touch.
    """

    priority: int = Field(
        ge=1,
        description=(
            "1 = highest priority, count upward. Priority is assigned across the "
            "COMBINED list of blocking_issues + judgment_notes. Every finding in "
            "blocking_issues MUST have a lower priority number than every item in "
            "judgment_notes."
        ),
    )
    responsible: str = Field(
        description=(
            "Who should act on this finding. Must be one of: "
            "'bookkeeper' (fix in QBO, pull docs, obtain invoices), "
            "'CPA' (apply professional judgment, write-off, reclassification decision), "
            "'client' (confirm facts, provide documents, answer business questions), "
            "'shareholder' (sign a confirmation letter, inject capital, convert loan). "
            "Pick exactly one — the party PRIMARILY responsible for resolving."
        ),
    )
    title: str = Field(
        description="One-line headline. Include the specific dollar amount or entry # when known."
    )
    detail: str = Field(
        description="1-2 sentences of context or evidence. Skip if title says it all."
    )
    plain_language_action: str = Field(
        description=(
            "A single concrete action for the responsible party, in plain language. "
            "Examples: 'Open JE #3 in QBO and add a memo explaining the shareholder "
            "transfer.' 'Confirm with Cleany whether supplies were on hand at year-end.' "
            "'Sign a letter confirming the Nov 16 Interac transfer was a personal "
            "expense reimbursement.' NO CPA jargon when responsible='bookkeeper' or 'client' — "
            "write as if to a junior employee, not a senior colleague."
        ),
    )


class LlmReviewOutput(BaseModel):
    """Structured output from Opus 4.7 synthesizing the deterministic findings."""

    executive_summary: str = Field(
        description=(
            "2-3 sentences in professional CPA tone. State the overall posture and the "
            "single most consequential issue. Direct, non-hedging."
        )
    )
    blocking_issues: list[LlmFinding] = Field(
        description=(
            "Items that MUST be resolved before sign-off. Ordered by the 'priority' "
            "field on each finding. Apply CPA judgment — deterministic agents "
            "over-flag; trim false positives."
        )
    )
    judgment_notes: list[LlmFinding] = Field(
        description=(
            "Warnings where the CPA must use judgment on whether each is material "
            "or systemic. Priority numbers continue after blocking_issues."
        )
    )
    proposed_adjustments: list[LlmAdjustingEntry] = Field(
        description=(
            "Concrete adjusting journal entries where the correction is clear from the "
            "evidence. Skip judgment calls — those belong in judgment_notes."
        )
    )
    questions_for_client: list[str] = Field(
        description=(
            "Questions the client must answer directly — business facts the data can't "
            "reveal. Do NOT put bookkeeper tasks here; those belong in blocking_issues "
            "or judgment_notes with responsible='bookkeeper'."
        )
    )
    sign_off_ready: bool = Field(
        description="True only if no blocking issues remain after CPA judgment."
    )


_LLM_SYSTEM_PROMPT = """You are a senior Canadian CPA reviewing a bookkeeper's close before client sign-off. A deterministic audit pipeline has run and produced findings; your job is to apply professional judgment to those findings and produce a structured review memo.

# Canadian sales-tax primer (essential context)

- **GST** (federal, 5%) applies to most goods and services.
- **HST** combines GST + provincial tax into one rate: 13% in Ontario; 15% in New Brunswick, Nova Scotia, Newfoundland, and PEI.
- **QST** (Quebec, 9.975%) stacks on GST (5%) for a 14.975% combined rate. Returns are filed separately (CRA vs. Revenu Québec).
- **PST** (BC 7%, Manitoba 7%, Saskatchewan 6%) is a provincial-only retail tax.
- **Zero-rated supplies** (basic groceries, prescription drugs, exports): 0% tax.
- **Exempt supplies** (most insurance, residential rent, most financial services, most educational services): no tax charged, no ITC claimable.

# Quick Method of GST/HST Accounting (CRA)

The **Quick Method** is a CRA election available to small businesses with annual
taxable sales ≤ $400,000. Under Quick Method:

- The business still charges regular GST/HST/QST on its sales (invoice amounts are unchanged).
- On the return, it remits a **reduced rate** on taxable sales — approximately 3.6% for services in HST provinces (ON / NB / NS / NL / PE), 1.8% for services outside HST provinces, and different rates for goods-sellers.
- It does **not** claim ITCs on ordinary operating expenses. The reduced remittance rate effectively bakes the ITCs in.
- It **may** still claim ITCs on capital purchases over $10,000 (vehicles, computers, equipment), zero-rated inputs, and some specified expenses.
- A plus 1% credit applies on the first $30,000 of eligible sales each fiscal year.

**Audit implication:** a business on Quick Method will show 0% implied tax rate across nearly all vendor bills. That is **correct**, not an error. When the deterministic agents flag a "Quick Method pattern detected" finding, confirm with the client whether they're actually registered for the election before treating 0% rates as errors or as missing ITCs.

# QuickBooks Online specifics (do NOT misdiagnose)

- **QBO tracks GST and QST via tax codes inside the Tax Center, not via separate GL accounts.** A correctly configured Quebec QBO file typically has ONE consolidated "GST/HST Payable" (or "Sales Tax Payable") account. Do NOT tell the bookkeeper to create a separate QST account in the chart of accounts. If Quebec activity is detected, the check is: is the Tax Center set up with both GST and QST registered, and are the correct tax codes being applied to Quebec purchases?
- **Interac e-Transfers recorded as deposits** are a documentation issue, not a coding error per se. The coding (Sales vs. Shareholder Advance) may be correct; the question is whether the supporting document is on file — a sales receipt/invoice for sales, or a written shareholder statement for advances. Phrase these as "please confirm supporting docs on file", not "this is an error."
- **COGS without an Inventory account** may be legitimate for a pure service business, or for one that fully consumed all materials within the period. Ask the client; don't assume the absence of an Inventory account is always a mistake.
- **Sales-tax journal movement ≠ BS Payable.** The journal shows every debit/credit hitting the tax account *during the period*. That activity mixes (a) current-period accruals (collected-on-sales and ITC-on-purchases) with (b) prior-period return payments that cleared last year's liability during this fiscal year. A business paying its prior-year GST/HST/QST return during the current year will show a large "input tax" debit that has nothing to do with current-year net tax owed. Do NOT frame a difference between journal net and BS Payable as a coding error. The correct rollforward is: prior-period closing Payable + current-year accrued − returns paid during the period = current-period closing Payable. Without prior-period data, treat the journal tax movement as informational only.
- **Government refunds/remittances booked in the wrong direction** are a blocking error. Tax accounts on the BS are liabilities — they go UP when tax is collected from customers (credit), and DOWN when refunds are received from government or remittances are paid (debit). Bookkeepers commonly mis-book a refund DEPOSIT from CRA or Revenu Québec as a CREDIT to the tax account, which inflates the liability instead of reducing it. The deterministic agent flags this under `tax_refund_direction`. When it appears, treat as a blocking issue (it materially misstates the BS Payable); do not soften. Real case: Cleany Québec had $1,805 of Revenu Québec refund deposits credited to QST Suspense — $3,611 misstatement (correction swings by 2× the credit since you're flipping direction). The fix is always: open the deposit, flip the tax-account line from credit to debit.

# How to apply judgment

**The bookkeeper sees only YOUR output.** The deterministic findings are raw input for you — they are NOT shown to the bookkeeper as separate cards. Your job is to be ruthlessly selective and to organize findings so each one is immediately actionable by the right person.

## The 90% rule (hard filter)

Include a finding in **blocking_issues** or **judgment_notes** ONLY if you're ≥90% confident it represents a real concern a CPA would act on. Specifically:

- **blocking_issues** = items that MUST be resolved before sign-off. Examples: BS doesn't balance, profit doesn't tie, material duplicate transactions, unfiled return with balance owing, untraceable material journal entries.
- **judgment_notes** = items requiring CPA judgment on whether material or systemic. Examples: Interac deposits needing doc-on-file confirmation, GST/HST Suspense non-zero, vendor rate outliers, Quick Method pattern to confirm.
- **SKIP as noise**: top-vendor lists, tax-account inventory, monthly activity, bank-balance snapshots, "all N checks passed" OK findings, rate-bucket summaries. These are raw input, NOT output.

## Role tagging (every finding gets one)

Every finding must be tagged with ONE responsible party via the `responsible` field:

- **`bookkeeper`** — actions in QBO (add memos to JEs, obtain sample invoices from vendors, delete confirmed duplicates, verify tax codes in the Tax Center, reconcile accounts). The bookkeeper owns the books; they fix hygiene and collect documentation.
- **`CPA`** — applies professional judgment (materiality calls, write-off decisions, reclassification choices, going-concern considerations, complex compliance calls). The CPA doesn't touch QBO but makes the decision the bookkeeper executes.
- **`client`** — confirms business facts or provides documents the data can't reveal (year-end inventory count, existence of contracts, registration numbers, whether a payment was personal vs business). This is the owner/manager of the audited business.
- **`shareholder`** — specifically personal actions by the owner-shareholder (signing a confirmation that an Interac transfer is a loan, converting a shareholder loan to equity, injecting capital). Use ONLY when the shareholder personally must act — if it's the business's action, use `client`.

Pick exactly one per finding — the party PRIMARILY responsible for the first action. Don't hedge.

## Priority

Assign `priority` as a rank number, 1 = most urgent, across the COMBINED blocking + judgment list. Every blocking item's priority number must be lower (more urgent) than every judgment item's. Priority within each list is by (a) compliance risk (CRA/RQ reassessment exposure) first, (b) materiality in dollars, (c) how long the fix takes.

## Plain-language action

For each finding, write a **one-line concrete action** in `plain_language_action` aimed at the responsible role:

- For `bookkeeper`: no CPA jargon. Tell them which screen to open in QBO, which number to enter, which vendor to email. Example: *"Open JE #3 in QBO (Company → Make General Journal Entries → search 3), click into the Memo field, and write 1-2 sentences explaining what this entry corrects."* NOT *"Populate the narrative field on the manual journal entry to satisfy documentation requirements."*
- For `client`: plain questions, no accounting terms. Example: *"Did you have any cleaning supplies or materials still on hand at December 31, 2024? Roughly what were they worth?"* NOT *"Please provide the closing inventory valuation."*
- For `shareholder`: describe what to sign and why. Example: *"Please sign a short letter saying the $52.50 Interac transfer you received on Nov 16 was a personal reimbursement from the business, not a loan or gift."*
- For `CPA`: full professional language is fine.

## Voice and length

- 2-3 sentence executive summary. Direct, non-hedging. State the posture and the single most consequential concern.
- Each finding title ≤1 line, with specific dollar amount or entry number when known.
- Detail ≤2 sentences.
- Proposed adjustments ONLY when the correction is concrete from the evidence (amount and accounts visible in the data). Don't invent numbers.
- `questions_for_client` = only things the CLIENT can answer (business facts). Bookkeeper tasks go in blocking/judgment with `responsible='bookkeeper'`, NOT in questions.

## Using the client profile (if provided)

If a client profile is included in the user message (province, Quick Method election, inventory tracked, payroll, prior-year status, etc.), use it as DEFINITIVE:

- Don't ask questions the profile already answers.
- Don't flag items the profile resolves (e.g., if profile says Quick Method is elected, 0% vendor rates are correct — don't flag them).
- Don't ask the client to "confirm" things the profile states.

If a field is missing from the profile, then yes — ask, or flag for verification.

## Special cases

- **Materiality.** Use ~5% of PROFIT or ~0.5% of revenue as a rough line. Small individual issues still matter if systemic or if they create compliance risk.
- **Error ≠ blocking.** Deterministic ERROR severity is a rule-failure indicator, not your final word. Apply your judgment.
- **Be vigilant but not alarmist.** Prefer "please confirm" / "please verify" over "this is wrong."
- **Compliance trumps hygiene.** Bookkeeping tidiness < CRA/RQ compliance.

Be professional, direct, and specific. Canadian dollar conventions and tax terminology. Prefer named accounts over vague references."""


def synthesize_memo_with_llm(
    memo: Memo,
    findings: list["Finding"],
    bs_highlights: dict[str, str] | None = None,
    pnl_highlights: dict[str, str] | None = None,
    client_profile: dict | None = None,
) -> LlmReviewOutput | None:
    """Run Opus 4.7 over the deterministic memo to produce a CPA-reviewed version.

    Returns ``None`` if the API key isn't set, the anthropic SDK isn't available,
    or the API call fails. The caller should fall back to rendering the
    deterministic memo alone — this function must never raise.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.info("cpa_reviewer.llm_skipped reason=no_api_key")
        return None

    try:
        import anthropic  # local import — keeps the import cost out of cold-path code
    except ImportError:
        log.warning("cpa_reviewer.llm_skipped reason=anthropic_not_installed")
        return None

    user_message = _render_user_message(
        memo, findings, bs_highlights, pnl_highlights, client_profile
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.parse(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=_LLM_SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": user_message}],
            output_format=LlmReviewOutput,
        )
    except anthropic.AuthenticationError:
        log.warning("cpa_reviewer.llm_auth_error key_rejected")
        return None
    except anthropic.RateLimitError:
        log.warning("cpa_reviewer.llm_rate_limited")
        return None
    except anthropic.APIStatusError as e:
        log.warning("cpa_reviewer.llm_api_error status=%s", e.status_code)
        return None
    except Exception:  # last line of defense — must not crash the handler
        log.exception("cpa_reviewer.llm_unexpected_error")
        return None

    log.info(
        "cpa_reviewer.llm_done blocking=%d judgment=%d adjustments=%d questions=%d",
        len(response.parsed_output.blocking_issues),
        len(response.parsed_output.judgment_notes),
        len(response.parsed_output.proposed_adjustments),
        len(response.parsed_output.questions_for_client),
    )
    return response.parsed_output


_PROFILE_FIELD_LABELS = {
    "legal_name":          "Legal entity name",
    "industry":            "Industry",
    "province":            "Primary province of operation",
    "fiscal_year_end":     "Fiscal year end",
    "gst_hst_registered":  "GST/HST registered",
    "qst_registered":      "QST registered (Quebec)",
    "pst_registered":      "PST registered (BC/MB/SK)",
    "quick_method_elected": "Quick Method elected (reduced-rate remittance, no ITCs on ordinary expenses)",
    "has_inventory":       "Carries inventory",
    "has_payroll":         "Runs payroll (T4 employees)",
    "prior_year_filed":    "Prior-year return filed",
    "notes":               "Additional notes",
}


def _render_user_message(
    memo: Memo,
    findings: list["Finding"],
    bs_highlights: dict[str, str] | None,
    pnl_highlights: dict[str, str] | None,
    client_profile: dict | None = None,
) -> str:
    """Compose the structured context sent to the LLM as the user message."""
    lines: list[str] = [
        "# Engagement",
        f"- Client: {memo.company}",
        f"- Period: {memo.period}",
        f"- Engagement ID: {memo.engagement_id}",
        "",
        "# Deterministic audit summary",
        f"- Total checks: {memo.total_checks}",
        f"- Errors: {memo.n_errors}  ·  Warnings: {memo.n_warnings}  ·  "
        f"Info: {memo.n_info}  ·  OK: {memo.n_ok}",
    ]

    if client_profile:
        lines.append("")
        lines.append("# Client profile (DEFINITIVE — treat as confirmed facts, do not re-ask)")
        for key, label in _PROFILE_FIELD_LABELS.items():
            val = client_profile.get(key)
            if val:
                lines.append(f"- **{label}:** {val}")

    if bs_highlights:
        lines.append("")
        lines.append("# Balance Sheet highlights")
        for k, v in bs_highlights.items():
            lines.append(f"- {k}: {v}")

    if pnl_highlights:
        lines.append("")
        lines.append("# P&L highlights")
        for k, v in pnl_highlights.items():
            lines.append(f"- {k}: {v}")

    lines.append("")
    lines.append("# All findings (from earlier agents)")
    for f in findings:
        agent = _AGENT_DISPLAY.get(f.agent, f.agent)
        lines.append(f"## [{f.severity.upper()}] {agent} — {f.check}")
        lines.append(f"**Title:** {f.title}")
        if f.detail:
            lines.append(f"**Detail:**\n{f.detail}")
        if f.proposed_fix:
            lines.append(f"**Proposed fix (automated):** {f.proposed_fix}")
        lines.append("")

    return "\n".join(lines)
