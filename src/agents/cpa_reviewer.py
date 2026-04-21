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


class LlmReviewOutput(BaseModel):
    """Structured output from Opus 4.7 synthesizing the deterministic findings."""

    executive_summary: str = Field(
        description=(
            "2-3 sentences in professional CPA tone. State the overall posture and the "
            "single most consequential issue. Direct, non-hedging."
        )
    )
    blocking_issues: list[str] = Field(
        description=(
            "Items that MUST be resolved before sign-off, in priority order. Apply "
            "CPA judgment — the deterministic agents may over-flag; trim false positives."
        )
    )
    judgment_notes: list[str] = Field(
        description=(
            "Warnings where the CPA must use judgment on whether each is material "
            "or systemic. Explain the trade-off in one sentence each."
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
            "Questions the CPA cannot answer from the numeric data alone and should "
            "ask the bookkeeper or the client directly."
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

# QuickBooks Online specifics (do NOT misdiagnose)

- **QBO tracks GST and QST via tax codes inside the Tax Center, not via separate GL accounts.** A correctly configured Quebec QBO file typically has ONE consolidated "GST/HST Payable" (or "Sales Tax Payable") account. Do NOT tell the bookkeeper to create a separate QST account in the chart of accounts. If Quebec activity is detected, the check is: is the Tax Center set up with both GST and QST registered, and are the correct tax codes being applied to Quebec purchases?
- **Interac e-Transfers recorded as deposits** are a documentation issue, not a coding error per se. The coding (Sales vs. Shareholder Advance) may be correct; the question is whether the supporting document is on file — a sales receipt/invoice for sales, or a written shareholder statement for advances. Phrase these as "please confirm supporting docs on file", not "this is an error."
- **COGS without an Inventory account** may be legitimate for a pure service business, or for one that fully consumed all materials within the period. Ask the client; don't assume the absence of an Inventory account is always a mistake.

# How to apply judgment

- **Error ≠ blocking.** The deterministic agents flag anything that fails a rule, but most flags are judgment calls. Apply CPA judgment: downgrade or drop findings that don't actually block sign-off. Something is a blocking issue only if (a) it's a hard arithmetic failure (BS doesn't balance, profit doesn't tie), (b) it's a clear CRA/RQ compliance problem (unfiled return with balance owing), or (c) it's a systemic coding error affecting many transactions.
- **Be vigilant but not alarmist.** Prefer "please confirm with client" / "please verify supporting documentation is on file" over "this is an error." Reserve error-level language for unambiguous failures.
- **Materiality.** Use ~5% of PROFIT or ~0.5% of revenue as a rough line for small owner-managed businesses.
- **Concrete over vague.** Propose adjusting journal entries with specific debit/credit accounts and amounts whenever the correction is clear. If the fix requires client input or judgment, put it in questions_for_client instead.
- **Compliance trumps hygiene.** Bookkeeping tidiness (duplicates, missing memos) is lower priority than CRA/RQ compliance.

Be professional, direct, and specific. Use Canadian dollar conventions and tax terminology. Prefer named accounts over vague references."""


def synthesize_memo_with_llm(
    memo: Memo,
    findings: list["Finding"],
    bs_highlights: dict[str, str] | None = None,
    pnl_highlights: dict[str, str] | None = None,
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

    user_message = _render_user_message(memo, findings, bs_highlights, pnl_highlights)

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


def _render_user_message(
    memo: Memo,
    findings: list["Finding"],
    bs_highlights: dict[str, str] | None,
    pnl_highlights: dict[str, str] | None,
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
