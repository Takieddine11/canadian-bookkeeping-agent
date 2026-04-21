"""Cleanup Coach — guided SOP for a Canadian QBO bookkeeping cleanup.

Different from the audit agents: this one doesn't run *against* data, it guides
the bookkeeper *through* the cleanup BEFORE any files exist. It's a linear
checklist walker driven by ``knowledge/bookkeeping_cleanup_sop.md``.

Interaction model:

1. Bookkeeper: ``new cleanup <period>``
2. Bot: posts **Step 1** (Client context) with the checklist
3. Bookkeeper works in QBO / with the client; when done, replies ``next`` (or ``done``)
4. Bot: advances to **Step 2**, posts it
5. …continues through 8 macro steps…
6. After step 8, bot posts "Cleanup complete — type ``new audit <period>`` and drop
   the three files" → engagement can be closed; bookkeeper starts a fresh audit run.

Supported commands in cleanup mode:

* ``next`` / ``done`` / ``continue``  → advance one step
* ``back`` / ``previous``             → go back one step
* ``repeat`` / ``show``               → re-render current step
* ``status``                          → show progress N/8
* ``skip`` (per-step)                 → mark current step N/A and advance
* anything else                       → posted as a generic "please clarify" for now
  (LLM-backed Q&A is a future layer — see docs/TODO)

The SOP content is hard-coded as structured step definitions here rather than parsed
from the markdown file. The markdown is the canonical reference, this module is the
runtime. They must stay in sync — see the knowledge file's notes section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.store.engagement_db import (
    MODE_AUDIT,
    PHASE_DELIVERED,
    PHASE_INTAKE,
    Engagement,
    EngagementStore,
)

log = logging.getLogger(__name__)
AGENT = "cleanup_coach"


@dataclass(frozen=True)
class CleanupStep:
    index: int
    title: str
    body: str                # markdown body of the step (checklist + guidance)
    non_skippable: bool = False


# The 8 macro-steps of the cleanup SOP. Kept deliberately close to the markdown
# knowledge file so both read the same way.
STEPS: list[CleanupStep] = [
    CleanupStep(
        index=0,
        title="Step 1 / 8 — Client context",
        body=(
            "Before any cleanup work, lock down the client's basics. Every downstream "
            "decision depends on these.\n\n"
            "- [ ] **Legal entity & Business Number** (BN) — confirm BN + CRA program accounts "
            "(RT for GST/HST, RP for payroll, RC for corp tax)\n"
            "- [ ] **Fiscal year-end** — confirm it matches QBO's fiscal-year setting\n"
            "- [ ] **Provinces of operation** — drives sales-tax rate structure\n"
            "- [ ] **Sales-tax registrations** — GST/HST, QST (Quebec), PST (BC/MB/SK); filing frequency for each\n"
            "- [ ] **Payroll program** — employer? Dividends only?\n"
            "- [ ] **Industry** — service / retail / construction / professional / etc.\n"
            "- [ ] **Prior-year status** — was it filed? Opening balances confirmed?\n\n"
            "Type **next** when you have this information documented."
        ),
        non_skippable=True,
    ),
    CleanupStep(
        index=1,
        title="Step 2 / 8 — Documents from the client",
        body=(
            "Request the core cleanup pack from the client. Nothing downstream works "
            "without these.\n\n"
            "- [ ] Bank statements for **every** active bank account, **full period**\n"
            "- [ ] Credit-card statements for every active card\n"
            "- [ ] Loan / line-of-credit statements (if any)\n"
            "- [ ] Expense receipts (digital copies organized by month)\n"
            "- [ ] Vendor invoices / bills\n"
            "- [ ] Customer invoices issued\n"
            "- [ ] Payroll source documents (if applicable)\n"
            "- [ ] Prior-year returns, filed GST/HST/QST returns\n"
            "- [ ] CRA / Revenu Québec correspondence (assessments, notices)\n"
            "- [ ] Material contracts (landlord, key vendors/customers)\n\n"
            "Type **next** once you have everything in hand, or **skip** if the client "
            "genuinely has nothing (document why)."
        ),
        non_skippable=True,
    ),
    CleanupStep(
        index=2,
        title="Step 3 / 8 — Bank & credit-card reconciliation",
        body=(
            "Every bank and CC account reconciled to the statement for every month in the "
            "period. Uncleared timing differences explicitly identified.\n\n"
            "- [ ] Every bank account shows ✓ reconciled in QBO for every month of the period\n"
            "- [ ] Every credit-card account reconciled\n"
            "- [ ] Timing differences (uncleared cheques, deposits in transit) listed with dates\n"
            "- [ ] No transactions in **Undeposited Funds** older than 30 days\n"
            "- [ ] Bank feeds connected and current\n\n"
            "This is non-negotiable — CRA audit defense depends on bank-reconciled books. "
            "Type **next** when all accounts are green."
        ),
        non_skippable=True,
    ),
    CleanupStep(
        index=3,
        title="Step 4 / 8 — Uncategorized → categorized",
        body=(
            "All transactions posted to a real GL account. At cleanup-end these should "
            "be **zero**:\n\n"
            "- [ ] **Uncategorized Income** — balance = $0\n"
            "- [ ] **Uncategorized Expense** — balance = $0\n"
            "- [ ] **Ask My Accountant** — balance = $0\n"
            "- [ ] **Opening Balance Equity** — only deliberate opening entries\n"
            "- [ ] **Interac e-Transfer deposits** — every one has either a sales receipt "
            "(revenue) or a written shareholder statement (advance) attached in QBO or filed\n\n"
            "The Interac documentation item is the #1 cause of CRA reassessment on small "
            "files — don't skip it. Type **next** when the suspense accounts are empty."
        ),
        non_skippable=True,
    ),
    CleanupStep(
        index=4,
        title="Step 5 / 8 — Chart of accounts cleanup",
        body=(
            "Tidy the COA. Duplicates merged, unused accounts archived, names consistent.\n\n"
            "- [ ] Duplicate accounts merged\n"
            "- [ ] Unused accounts archived (not deleted — preserves history)\n"
            "- [ ] Consistent naming (\"Telephone & Internet\" not \"phone\")\n"
            "- [ ] Parent/sub-account hierarchy is sensible\n"
            "- [ ] Tax-liability accounts match filings — typically **one** consolidated "
            "\"GST/HST Payable\" (QBO handles GST/QST split via tax codes in the Tax Center, "
            "**not** separate GL accounts)\n\n"
            "Type **next** when the COA is clean."
        ),
    ),
    CleanupStep(
        index=5,
        title="Step 6 / 8 — Sales-tax compliance",
        body=(
            "Highest-risk area. CRA (GST/HST) and Revenu Québec (QST) reassessments hit "
            "small files here more than anywhere else.\n\n"
            "- [ ] **QBO Tax Center verified** — right taxes registered, right filing "
            "frequency, Quebec clients have both GST and QST active\n"
            "- [ ] **Tax codes applied correctly** on purchases and sales (Quebec purchases "
            "use the combined GST+QST code, not GST-only)\n"
            "- [ ] **ITC (Input Tax Credits)** claimed on eligible expenses\n"
            "- [ ] Meals & entertainment ITC at 50% (CRA rule)\n"
            "- [ ] **Prior returns filed** — any \"Suspense\" balance traced to a filed return\n"
            "- [ ] Net tax on the most recent return reconciles to the BS GST/HST Payable\n\n"
            "Type **next** when the Tax Center is clean and prior returns are filed."
        ),
        non_skippable=True,
    ),
    CleanupStep(
        index=6,
        title="Step 7 / 8 — A/R, A/P, payroll review",
        body=(
            "Compliance review of working-capital and payroll accounts.\n\n"
            "- [ ] **A/R** aged (30/60/90/+); stale items >90 days written off or noted for collection\n"
            "- [ ] **Customer deposits** held separately from revenue if service not delivered\n"
            "- [ ] **A/P** current; vendor credits applied\n"
            "- [ ] **Subcontractors paid >$500/yr** identified for T4A at year-end\n"
            "- [ ] **Payroll (if applicable)** — CPP, EI, federal + provincial tax withheld correctly\n"
            "- [ ] Monthly **PD7A remittances** filed on time\n"
            "- [ ] T4 amounts for each employee will tie to the payroll register at year-end "
            "(Feb 28 deadline)\n\n"
            "Type **next** when these areas are reviewed."
        ),
    ),
    CleanupStep(
        index=7,
        title="Step 8 / 8 — Year-end closing adjustments",
        body=(
            "Final adjusting entries that turn in-progress books into \"ready for CPA review\".\n\n"
            "- [ ] **Inventory / COGS** — physical closing count on period-end, Inventory "
            "asset adjusted to actual, COGS trued up. (Service businesses: document that "
            "no inventory is correct.)\n"
            "- [ ] **Shareholder loans** cleaned up; personal-use transactions reclassed; "
            "opening balance agrees with last filed T2\n"
            "- [ ] **Dividends** (if declared) — DR Retained Earnings / CR Dividends Payable; "
            "T5 slip issued in the calendar year paid\n"
            "- [ ] **Accruals** — accrued salaries, professional fees, interest on loans\n"
            "- [ ] **Prepaid expenses** — identified and amortized\n"
            "- [ ] **Deferred revenue** — customer prepayments moved out of Sales\n"
            "- [ ] **CCA (depreciation)** — fixed-asset additions assigned CCA classes, "
            "depreciation booked per CRA\n"
            "- [ ] **Retained-earnings rollforward** — prior RE + current profit − dividends "
            "= current RE (must tie)\n\n"
            "Type **done** when every applicable adjustment is booked. I'll then tell you "
            "exactly what to export and hand off to the audit pipeline."
        ),
        non_skippable=True,
    ),
]

# Command vocabulary.
NEXT_COMMANDS = frozenset({"next", "done", "continue", "ok", "go", "proceed"})
BACK_COMMANDS = frozenset({"back", "previous", "prev"})
REPEAT_COMMANDS = frozenset({"repeat", "show", "current"})
STATUS_COMMANDS = frozenset({"status", "progress", "where"})
SKIP_COMMANDS = frozenset({"skip", "n/a", "not applicable"})


@dataclass(frozen=True)
class CoachResponse:
    text: str                                 # what to post back to the user
    step_index: int                           # where the engagement is now
    cleanup_complete: bool = False            # True when the user finished step 8
    unrecognized: bool = False                # True if we didn't understand the command


def opening_message(period: str) -> str:
    """The message the bot posts right after `new cleanup <period>`."""
    return (
        "📋 **Cleanup mode** started for period "
        f"`{period}`.\n\n"
        "I'll walk you through the 8 cleanup steps in sequence. After each step, "
        "type **next** to advance, **back** to revisit the previous step, "
        "**repeat** to see the current step again, or **status** for overall progress. "
        "If a step genuinely doesn't apply, type **skip** (non-skippable items will "
        "refuse).\n\n"
        "When all 8 steps are done I'll tell you exactly what to export and how to "
        "kick off the audit."
    )


def render_step(step: CleanupStep) -> str:
    return f"**{step.title}**\n\n{step.body}"


def handle_command(
    store: EngagementStore, engagement: Engagement, text: str
) -> CoachResponse:
    """Advance the cleanup state machine based on the bookkeeper's message.

    Persists ``cleanup_step_index`` back to the store when it changes. Transitions
    the engagement out of cleanup (mode → audit, phase → intake) when step 8 is
    completed.
    """
    command = text.strip().lower()
    idx = engagement.cleanup_step_index

    if command in REPEAT_COMMANDS or command == "":
        return CoachResponse(render_step(STEPS[idx]), idx)

    if command in STATUS_COMMANDS:
        return CoachResponse(
            f"Cleanup progress: step **{idx + 1} of {len(STEPS)}** — "
            f"_{STEPS[idx].title}_.\n\nType **repeat** to see the current checklist.",
            idx,
        )

    if command in BACK_COMMANDS:
        if idx == 0:
            return CoachResponse(
                "You're already on the first step.\n\n" + render_step(STEPS[0]), 0
            )
        new_idx = idx - 1
        store.advance_cleanup_step(engagement.engagement_id, new_idx)
        return CoachResponse(
            f"Moved back to step {new_idx + 1}.\n\n" + render_step(STEPS[new_idx]),
            new_idx,
        )

    if command in NEXT_COMMANDS:
        return _advance(store, engagement, idx, skipping=False)

    if command in SKIP_COMMANDS:
        if STEPS[idx].non_skippable:
            return CoachResponse(
                f"**{STEPS[idx].title}** is non-skippable — CRA/RQ audit defense "
                f"depends on it. Work through the checklist, then type **next**.",
                idx,
            )
        return _advance(store, engagement, idx, skipping=True)

    # Fallback — command we don't understand. In v2 this will route to an LLM Q&A.
    return CoachResponse(
        "I didn't catch a command. I understand **next** (advance), **back** "
        "(previous), **repeat** (current step), **status** (progress), **skip** "
        "(mark N/A — for optional steps only).\n\n"
        "Free-text Q&A about the current step is coming soon. For now, type one of "
        "the commands above.",
        idx,
        unrecognized=True,
    )


def _advance(
    store: EngagementStore, engagement: Engagement, idx: int, *, skipping: bool
) -> CoachResponse:
    if idx >= len(STEPS) - 1:
        # Cleanup finished.
        store.set_mode(engagement.engagement_id, MODE_AUDIT)
        store.update_phase(engagement.engagement_id, PHASE_DELIVERED)
        log.info(
            "cleanup.completed engagement=%s", engagement.engagement_id
        )
        return CoachResponse(
            "✅ **Cleanup complete.** The books should now be ready for the audit pipeline.\n\n"
            "**Next: kick off an audit run.** Type:\n\n"
            f"    new audit {engagement.period_description or '<period>'}\n\n"
            "Then drop the three files:\n"
            "- Journal Report (Detail) — CSV or PDF\n"
            "- Balance Sheet — PDF or XLSX\n"
            "- Profit & Loss — PDF or XLSX\n"
            "\nThe audit will run all four agents (Rollforward, Reconciliation, Sales Tax, "
            "CPA Review) and post a final memo for sign-off.",
            len(STEPS),
            cleanup_complete=True,
        )
    new_idx = idx + 1
    store.advance_cleanup_step(engagement.engagement_id, new_idx)
    lead = "Skipped. " if skipping else ""
    log.info(
        "cleanup.step_advanced engagement=%s from=%d to=%d skipped=%s",
        engagement.engagement_id, idx, new_idx, skipping,
    )
    return CoachResponse(lead + render_step(STEPS[new_idx]), new_idx)
