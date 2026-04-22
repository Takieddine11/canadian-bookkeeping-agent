"""Teams activity handler for the Bookkeeping Audit bot.

Phase 1 responsibilities (personal-scope 1:1 DM):

* Recognize the ``new audit <period>`` trigger in a 1:1 chat, create an engagement,
  reply with the intake adaptive card.
* Receive file attachments dragged into the chat, download them from the Teams
  download URL, persist to ``.tmp/engagements/<id>/`` and register in the engagement DB.

Personal scope matters: Teams reliably forwards user-initiated file attachments to a
bot in 1:1 DMs; channel-scope drops them silently. The bot still works in channels
(trigger phrases route by ``conversation.id``) but file intake is expected in 1:1.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

_ZERO = Decimal("0")

import aiohttp
from botbuilder.core import CardFactory, MessageFactory, TurnContext
from botbuilder.core.teams import TeamsActivityHandler
from botbuilder.schema import Attachment

from src.agents import cleanup_coach as agent_cleanup_coach
from src.agents import cpa_reviewer as agent_cpa_reviewer
from src.agents import reconciliation as agent_reconciliation
from src.agents import rollforward as agent_rollforward
from src.agents import tax_auditor as agent_tax_auditor
from src.agents.base import (
    SEVERITY_ERROR,
    SEVERITY_ICONS,
    SEVERITY_WARN,
    Finding,
    sort_findings,
)
from src.orchestrator.state_machine import (
    CORE_INTAKE_DOCS,
    advance_from_intake,
    intake_status,
    is_ready_trigger,
)
from src.parsers.financial_statement import (
    REPORT_BALANCE_SHEET,
    REPORT_PNL,
    FinancialStatement,
    parse_balance_sheet,
    parse_pnl,
)
from src.parsers.journal import JournalReport, parse_journal_csv
from src.store.engagement_db import (
    CONV_CHANNEL,
    CONV_GROUP,
    CONV_PERSONAL,
    DOC_BALANCE_SHEET,
    DOC_BANK_STATEMENT,
    DOC_JOURNAL,
    DOC_PNL,
    MODE_CLEANUP,
    PHASE_DELIVERED,
    Engagement,
    EngagementStore,
)

log = logging.getLogger(__name__)

TEAMS_FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"
_TRIGGER_RE = re.compile(r"^\s*new\s+audit\s+(.+?)\s*$", re.IGNORECASE)
_CLEANUP_TRIGGER_RE = re.compile(r"^\s*new\s+cleanup\s+(.+?)\s*$", re.IGNORECASE)
# Matches `new audit` / `new cleanup` with NO period — so we can prompt for one.
_BARE_TRIGGER_RE = re.compile(r"^\s*new\s+(audit|cleanup)\s*$", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_CARDS_DIR = Path(__file__).parent / "cards"
INTAKE_CARD_PATH = _CARDS_DIR / "intake_request.json"
INTAKE_PROGRESS_CARD_PATH = _CARDS_DIR / "intake_progress.json"
JOURNAL_SUMMARY_CARD_PATH = _CARDS_DIR / "journal_summary.json"
STATEMENT_SUMMARY_CARD_PATH = _CARDS_DIR / "statement_summary.json"
CLIENT_PROFILE_CARD_PATH = _CARDS_DIR / "client_profile.json"
AGENT_FINDINGS_CARD_PATH = _CARDS_DIR / "agent_findings.json"
CPA_MEMO_CARD_PATH = _CARDS_DIR / "cpa_memo.json"
CPA_MEMO_LLM_CARD_PATH = _CARDS_DIR / "cpa_memo_llm.json"

_DOC_LABELS = {
    DOC_JOURNAL: "Journal",
    DOC_BALANCE_SHEET: "Balance Sheet",
    DOC_PNL: "P&L",
    DOC_BANK_STATEMENT: "Bank Statements",
}


class AuditBot(TeamsActivityHandler):
    def __init__(self, store: EngagementStore, uploads_root: Path) -> None:
        super().__init__()
        self.store = store
        self.uploads_root = uploads_root

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        # Adaptive-card button submits arrive as message activities with the
        # button payload in ``activity.value``. Route them first.
        value = turn_context.activity.value
        if isinstance(value, dict) and value.get("verb"):
            await self._handle_card_action(turn_context, value)
            return

        text = self._clean_text(turn_context)
        raw_attachments = turn_context.activity.attachments or []
        for a in raw_attachments:
            content_keys = (
                list(a.content.keys()) if isinstance(a.content, dict) else type(a.content).__name__
            )
            log.info(
                "attachment.seen content_type=%s name=%s content_url=%s content_keys=%s",
                a.content_type, a.name, a.content_url, content_keys,
            )
        attachments = self._non_mention_attachments(raw_attachments)
        log.info(
            "message.received text=%r raw_attachments=%d candidate=%d conversation_id=%s type=%s",
            text,
            len(raw_attachments),
            len(attachments),
            turn_context.activity.conversation.id,
            self._conversation_type(turn_context),
        )

        # Bare `new audit` / `new cleanup` with no period — prompt for one.
        bare = _BARE_TRIGGER_RE.match(text) if text else None
        if bare:
            mode_name = bare.group(1).lower()
            await turn_context.send_activity(MessageFactory.text(
                f"I need a period to start `{mode_name}`. Examples:\n\n"
                f"- `new {mode_name} Q3 2026`\n"
                f"- `new {mode_name} 2025`\n"
                f"- `new {mode_name} FY2026` or `new {mode_name} Jan-Mar 2026`\n\n"
                "Please resend with the period."
            ))
            return

        # `new cleanup <period>` — start the cleanup coach flow
        cleanup_match = _CLEANUP_TRIGGER_RE.match(text) if text else None
        if cleanup_match:
            await self._start_cleanup(turn_context, period=cleanup_match.group(1))
            return

        # `new audit <period>` — start the (existing) audit flow
        match = _TRIGGER_RE.match(text) if text else None
        if match:
            # Always auto-close any active engagement before starting a new one.
            # If the bookkeeper is typing `new audit`, they mean a fresh start — a
            # different client, a new period, or a clean re-run. Carrying the old
            # engagement forward is how we end up with two clients' files glued
            # together in a single engagement (real incident, 2026-04-22).
            conversation_id = turn_context.activity.conversation.id
            existing = self.store.get_active_engagement(conversation_id)
            if existing is not None:
                self.store.update_phase(existing.engagement_id, PHASE_DELIVERED)
                mode_label = (
                    f"cleanup (step {existing.cleanup_step_index + 1} of "
                    f"{len(agent_cleanup_coach.STEPS)})"
                    if existing.mode == MODE_CLEANUP
                    else f"audit (phase `{existing.phase}`)"
                )
                await turn_context.send_activity(MessageFactory.text(
                    f"✅ Previous engagement `{existing.engagement_id}` "
                    f"({mode_label}) closed. Starting fresh."
                ))
                log.info(
                    "engagement.auto_closed_on_new_audit prior=%s mode=%s phase=%s",
                    existing.engagement_id, existing.mode, existing.phase,
                )
            await self._start_engagement(turn_context, period=match.group(1))
            if attachments:
                await self._handle_uploads(turn_context, attachments)
                await self._post_intake_progress(turn_context)
            return

        # Messages against an already-active engagement
        conversation_id = turn_context.activity.conversation.id
        engagement = self.store.get_active_engagement(conversation_id)

        # Cleanup-mode engagement: every non-trigger message is a coach command
        if engagement is not None and engagement.mode == MODE_CLEANUP:
            await self._handle_cleanup_message(turn_context, engagement, text)
            return

        if attachments:
            await self._handle_uploads(turn_context, attachments)
            if engagement is not None:
                await self._post_intake_progress(turn_context)
            return

        if engagement is not None and is_ready_trigger(text):
            await self._start_audit(turn_context, engagement)
            return

        if engagement is not None:
            await turn_context.send_activity(
                MessageFactory.text(
                    "Got it. Drop more documents any time, or type **ready** when you want "
                    "me to start the audit."
                )
            )
            return

        await turn_context.send_activity(MessageFactory.text(self._help_text()))

    # ---- triggers ---------------------------------------------------------

    async def _start_cleanup(self, turn_context: TurnContext, period: str) -> None:
        """Open a new engagement in cleanup mode and post the opening message + step 1."""
        conversation_id = turn_context.activity.conversation.id
        conversation_type = self._conversation_type(turn_context)
        user_aad_id = self._user_aad_id(turn_context)

        # Same auto-close policy as `new audit` — explicit `new cleanup` means fresh start.
        existing = self.store.get_active_engagement(conversation_id)
        if existing is not None:
            self.store.update_phase(existing.engagement_id, PHASE_DELIVERED)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"✅ Previous engagement `{existing.engagement_id}` closed. "
                    "Starting cleanup."
                )
            )
            log.info(
                "engagement.auto_closed_on_new_cleanup prior=%s mode=%s",
                existing.engagement_id, existing.mode,
            )

        engagement = self.store.create_engagement(
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            user_aad_id=user_aad_id,
            client_id=None,
            period_description=period,
            mode=MODE_CLEANUP,
        )
        log.info(
            "cleanup.engagement_created id=%s conversation=%s period=%s",
            engagement.engagement_id, conversation_id, period,
        )

        await turn_context.send_activity(
            MessageFactory.text(agent_cleanup_coach.opening_message(period))
        )
        await turn_context.send_activity(
            MessageFactory.text(agent_cleanup_coach.render_step(
                agent_cleanup_coach.STEPS[0]
            ))
        )

    async def _handle_cleanup_message(
        self, turn_context: TurnContext, engagement: Engagement, text: str
    ) -> None:
        """Route a message to the cleanup coach and post its response."""
        response = agent_cleanup_coach.handle_command(self.store, engagement, text)
        await turn_context.send_activity(MessageFactory.text(response.text))
        if response.cleanup_complete:
            log.info(
                "cleanup.handed_off engagement=%s period=%s",
                engagement.engagement_id, engagement.period_description,
            )

    async def _start_engagement(self, turn_context: TurnContext, period: str) -> None:
        conversation_id = turn_context.activity.conversation.id
        conversation_type = self._conversation_type(turn_context)
        user_aad_id = self._user_aad_id(turn_context)

        existing = self.store.get_active_engagement(conversation_id)
        if existing is not None:
            await turn_context.send_activity(
                MessageFactory.text(
                    f"An engagement is already active here "
                    f"(id `{existing.engagement_id}`, period `{existing.period_description}`, "
                    f"phase `{existing.phase}`). Finish or close it before starting a new one."
                )
            )
            return

        engagement = self.store.create_engagement(
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            user_aad_id=user_aad_id,
            client_id=None,
            period_description=period,
        )
        log.info(
            "engagement.created id=%s conversation=%s type=%s period=%s",
            engagement.engagement_id, conversation_id, conversation_type, period,
        )

        card = self._render_intake_card(engagement)
        await turn_context.send_activity(
            MessageFactory.attachment(CardFactory.adaptive_card(card))
        )
        # Profile form is NOT posted here anymore — we post it after all core
        # docs arrive, pre-filled from the files so the bookkeeper only confirms
        # or fills the gaps. Much faster for monthly bookkeeping cycles.

    # ---- uploads ----------------------------------------------------------

    async def _handle_uploads(
        self, turn_context: TurnContext, attachments: list[Attachment]
    ) -> None:
        conversation_id = turn_context.activity.conversation.id
        engagement = self.store.get_active_engagement(conversation_id)
        if engagement is None:
            await turn_context.send_activity(
                MessageFactory.text(
                    "No active engagement here yet. Start one with `new audit <period>` "
                    "(example: `new audit Q3 2026`) before uploading files."
                )
            )
            return

        file_attachments = [a for a in attachments if a.content_type == TEAMS_FILE_DOWNLOAD_INFO]
        if not file_attachments:
            unsupported = ", ".join(sorted({a.content_type for a in attachments}))
            await turn_context.send_activity(
                MessageFactory.text(
                    f"I can't read those attachments (content types: {unsupported}). "
                    "In a 1:1 DM with me, drag-and-drop PDF / XLSX / CSV files directly."
                )
            )
            return

        for attachment in file_attachments:
            try:
                saved = await self._download_attachment(engagement, attachment)
            except Exception:
                log.exception("upload.failed name=%s", attachment.name)
                await turn_context.send_activity(
                    MessageFactory.text(f"Failed to download `{attachment.name}`.")
                )
                continue

            doc_type = _classify_doc_type(attachment.name)
            doc_id = self.store.attach_document(
                engagement_id=engagement.engagement_id,
                doc_type=doc_type,
                file_path=saved,
                original_filename=attachment.name,
            )
            log.info(
                "engagement.document_attached engagement=%s doc_id=%s type=%s path=%s",
                engagement.engagement_id, doc_id, doc_type, saved,
            )
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Received `{attachment.name}` (classified as **{doc_type}**)."
                )
            )

            if doc_type == DOC_JOURNAL:
                await self._summarize_journal(turn_context, saved)
            elif doc_type == DOC_BALANCE_SHEET:
                await self._summarize_statement(turn_context, saved, DOC_BALANCE_SHEET)
            elif doc_type == DOC_PNL:
                await self._summarize_statement(turn_context, saved, DOC_PNL)

    async def _summarize_journal(self, turn_context: TurnContext, path: Path) -> None:
        """Parse the uploaded journal and post a summary adaptive card."""
        try:
            report = parse_journal_csv(path)
        except Exception:
            log.exception("journal.parse_failed path=%s", path)
            await turn_context.send_activity(
                MessageFactory.text(
                    "I saved the journal but couldn't parse it. Check the file is a QBO "
                    "Journal Detail export (not Summary) and re-upload."
                )
            )
            return

        card = self._render_journal_summary(report)
        await turn_context.send_activity(
            MessageFactory.attachment(CardFactory.adaptive_card(card))
        )

    async def _post_intake_progress(self, turn_context: TurnContext) -> None:
        """Post a status card showing which core docs are in and what's still missing."""
        conversation_id = turn_context.activity.conversation.id
        engagement = self.store.get_active_engagement(conversation_id)
        if engagement is None:
            return

        status = intake_status(self.store, engagement)
        present_labels = [_DOC_LABELS.get(d, d) for d in CORE_INTAKE_DOCS if d in status.core_present]
        missing_labels = [_DOC_LABELS.get(d, d) for d in CORE_INTAKE_DOCS if d in status.core_missing]
        present_list = "**Received:**  " + (", ".join(present_labels) if present_labels else "—")

        if not missing_labels:
            missing_prompt = (
                "✓ All core documents in. Type **ready** to start the audit, "
                "or drop bank statements first if you have them."
            )
            missing_color = "good"
        else:
            missing_prompt = "**Still missing:**  " + ", ".join(missing_labels)
            missing_color = "warning"

        card = _substitute(
            json.loads(INTAKE_PROGRESS_CARD_PATH.read_text(encoding="utf-8")),
            {
                "presentList": present_list,
                "missingPrompt": missing_prompt,
                "missingColor": missing_color,
            },
        )
        await turn_context.send_activity(
            MessageFactory.attachment(CardFactory.adaptive_card(card))
        )

        # Post the client profile form ONCE, the first time all core docs are in.
        # Pre-fill fields inferred from the uploaded files so the bookkeeper only
        # confirms or corrects them rather than typing from scratch.
        should_post_profile = (
            status.has_all_core
            and engagement.client_profile_json is None  # not yet prompted or saved
        )
        if should_post_profile:
            inferred = self._infer_profile_from_docs(engagement)
            profile_card = self._render_profile_form(engagement, inferred)
            await turn_context.send_activity(
                MessageFactory.attachment(CardFactory.adaptive_card(profile_card))
            )
            log.info(
                "client_profile.form_posted engagement=%s inferred_fields=%s",
                engagement.engagement_id, sorted(inferred.keys()),
            )

    async def _start_audit(self, turn_context: TurnContext, engagement: Engagement) -> None:
        """Advance from intake to audit phases and post the audit-started card."""
        status = intake_status(self.store, engagement)
        if not status.has_all_core:
            missing = [_DOC_LABELS.get(d, d) for d in status.core_missing]
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Still missing {', '.join(missing)}. Upload those first, or confirm "
                    f"with **skip missing** to proceed anyway."
                )
            )
            return

        new_phase = advance_from_intake(self.store, engagement)
        log.info(
            "audit.started engagement=%s phase=%s",
            engagement.engagement_id, new_phase,
        )

        # Brief "running" text so the bookkeeper sees the bot is working. A full
        # memo card follows after all agents + LLM synthesis complete.
        await turn_context.send_activity(MessageFactory.text(
            f"🔍 Running audit on engagement `{engagement.engagement_id}` "
            f"({engagement.period_description or 'period unspecified'}). "
            "One moment — analyzing journal, BS, and P&L with four agents and "
            "Opus 4.7 synthesis…"
        ))

        # Run all three deterministic agents SILENTLY — no per-agent cards. The
        # findings all feed into Agent 4 (CPA Reviewer) which produces the single
        # unified card the bookkeeper actually sees.
        collected: list[Finding] = []
        collected.extend(agent_rollforward.run(self.store, engagement))
        collected.extend(agent_reconciliation.run(self.store, engagement))
        collected.extend(agent_tax_auditor.run(self.store, engagement))
        log.info(
            "audit.findings_collected engagement=%s total=%d error=%d warn=%d info=%d ok=%d",
            engagement.engagement_id, len(collected),
            sum(1 for f in collected if f.severity == SEVERITY_ERROR),
            sum(1 for f in collected if f.severity == SEVERITY_WARN),
            sum(1 for f in collected if f.severity == "info"),
            sum(1 for f in collected if f.severity == "ok"),
        )

        # Agent 4 — unified memo. Prefers Opus 4.7 synthesis (filters to 90%+
        # confidence items, professional CPA prose). Falls back to the
        # deterministic rollup if the LLM isn't available.
        await self._run_cpa_reviewer(turn_context, engagement, collected)

    async def _run_rollforward(
        self, turn_context: TurnContext, engagement: Engagement
    ) -> list[Finding]:
        return await self._run_agent(
            turn_context, engagement,
            agent_name="rollforward",
            runner=agent_rollforward.run,
            card_label="Rollforward (Agent 5) — balance-sheet ties",
        )

    async def _run_reconciliation(
        self, turn_context: TurnContext, engagement: Engagement
    ) -> list[Finding]:
        return await self._run_agent(
            turn_context, engagement,
            agent_name="reconciliation",
            runner=agent_reconciliation.run,
            card_label="Reconciliation (Agent 3) — journal hygiene",
        )

    async def _run_tax_auditor(
        self, turn_context: TurnContext, engagement: Engagement
    ) -> list[Finding]:
        return await self._run_agent(
            turn_context, engagement,
            agent_name="tax_auditor",
            runner=agent_tax_auditor.run,
            card_label="Sales tax (Agent 2) — GST/HST/QST coding review",
        )

    async def _run_agent(
        self,
        turn_context: TurnContext,
        engagement: Engagement,
        *,
        agent_name: str,
        runner,
        card_label: str,
    ) -> list[Finding]:
        log.info("agent.%s.start engagement=%s", agent_name, engagement.engagement_id)
        try:
            findings = runner(self.store, engagement)
        except Exception:
            log.exception("agent.%s.crashed engagement=%s", agent_name, engagement.engagement_id)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"The {agent_name} agent crashed. Engagement state unchanged; "
                    f"error logged."
                )
            )
            return []

        card = self._render_findings_card(agent_label=card_label, findings=findings)
        await turn_context.send_activity(
            MessageFactory.attachment(CardFactory.adaptive_card(card))
        )
        log.info(
            "agent.%s.done engagement=%s findings=%d",
            agent_name, engagement.engagement_id, len(findings),
        )
        return findings

    async def _handle_card_action(
        self, turn_context: TurnContext, value: dict[str, Any]
    ) -> None:
        """Dispatch for adaptive-card Action.Submit payloads."""
        verb = value.get("verb", "")
        engagement_id = value.get("engagementId")
        log.info("card.action verb=%s engagement=%s", verb, engagement_id)
        if verb == "cpa_approve":
            if engagement_id:
                self.store.update_phase(engagement_id, PHASE_DELIVERED)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"✅ Engagement `{engagement_id}` approved and marked **delivered**. "
                    f"Final memo archived."
                )
            )
            return
        if verb == "save_client_profile":
            if engagement_id:
                # Extract only the profile keys from the submit payload; verb/engagementId
                # etc. are not part of the profile.
                profile_keys = (
                    "legal_name", "industry", "province", "fiscal_year_end",
                    "gst_hst_registered", "qst_registered", "pst_registered",
                    "quick_method_elected", "has_inventory", "has_payroll",
                    "prior_year_filed", "notes",
                )
                profile = {k: (value.get(k) or "").strip() for k in profile_keys if value.get(k)}
                self.store.set_client_profile(engagement_id, json.dumps(profile, ensure_ascii=False))
                filled = [k for k, v in profile.items() if v]
                log.info(
                    "client_profile.saved engagement=%s fields=%d",
                    engagement_id, len(filled),
                )
                await turn_context.send_activity(
                    MessageFactory.text(
                        f"✅ Client profile saved ({len(filled)} fields). The audit will "
                        "use these facts as definitive — fewer questions, fewer false "
                        "flags. Drop your Journal / BS / P&L and type `ready`."
                    )
                )
            return

        if verb == "skip_client_profile":
            # Mark as prompted (empty profile) so we don't re-post the form on
            # subsequent doc uploads to the same engagement.
            if engagement_id:
                self.store.set_client_profile(engagement_id, json.dumps({}))
                log.info("client_profile.skipped engagement=%s", engagement_id)
            await turn_context.send_activity(
                MessageFactory.text(
                    "Skipped — proceeding without a profile. The audit will ask for "
                    "any facts it needs. Type `ready` when you're set."
                )
            )
            return

        if verb == "cpa_request_changes":
            # Close the engagement so the bookkeeper can't accidentally drop the
            # revised files into the same (now-stale) engagement. Force an
            # explicit `new audit <period>` to start the rework with a clean slate.
            if engagement_id:
                self.store.update_phase(engagement_id, PHASE_DELIVERED)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"🔄 Engagement `{engagement_id}` sent back. Fix the flagged "
                    f"items in QBO, then start the rework with a fresh engagement:\n\n"
                    f"    new audit <period>\n\n"
                    "Drop the re-exported Journal / BS / P&L and type `ready` to run "
                    "the audit again. Starting fresh prevents old and new files from "
                    "getting mixed in the same engagement."
                )
            )
            log.info(
                "card.action.request_changes engagement=%s closed=True",
                engagement_id,
            )
            return
        log.warning("card.action.unknown_verb verb=%s", verb)
        await turn_context.send_activity(MessageFactory.text("Unknown action."))

    async def _run_cpa_reviewer(
        self,
        turn_context: TurnContext,
        engagement: Engagement,
        all_findings: list[Finding],
    ) -> None:
        """Post the unified CPA review memo.

        Prefers Opus 4.7 synthesis — filters to 90%+ confidence items, professional
        CPA prose. Falls back to the deterministic rollup card only when the LLM
        isn't available (no API key, crash, rate limit, etc.).
        """
        log.info(
            "agent.cpa_reviewer.start engagement=%s input_findings=%d",
            engagement.engagement_id, len(all_findings),
        )
        # Re-read the engagement in case the profile was submitted between engagement
        # creation and `ready` — the caller's snapshot might not reflect it.
        refreshed = self.store.get_active_engagement(engagement.conversation_id)
        if refreshed is not None and refreshed.engagement_id == engagement.engagement_id:
            engagement = refreshed
        company = self._infer_company(engagement)
        try:
            memo = agent_cpa_reviewer.build_memo(
                engagement, all_findings, company=company,
            )
        except Exception:
            log.exception(
                "agent.cpa_reviewer.crashed engagement=%s", engagement.engagement_id
            )
            await turn_context.send_activity(
                MessageFactory.text(
                    "Audit ran but the review memo crashed while aggregating findings. "
                    "Engagement is not closed; please re-run or contact admin."
                )
            )
            return

        # Preferred path: Opus 4.7 synthesis posts a single filtered card.
        profile_dict: dict | None = None
        if engagement.client_profile_json:
            try:
                profile_dict = json.loads(engagement.client_profile_json)
            except json.JSONDecodeError:
                log.warning(
                    "cpa_reviewer.profile_parse_failed engagement=%s",
                    engagement.engagement_id,
                )

        llm_output = agent_cpa_reviewer.synthesize_memo_with_llm(
            memo, all_findings,
            bs_highlights=self._bs_highlights(engagement),
            pnl_highlights=self._pnl_highlights(engagement),
            client_profile=profile_dict,
        )
        if llm_output is not None:
            await turn_context.send_activity(
                MessageFactory.attachment(
                    CardFactory.adaptive_card(self._render_cpa_memo_llm(memo, llm_output))
                )
            )
            log.info(
                "agent.cpa_reviewer.done engagement=%s source=llm "
                "errors=%d warnings=%d sign_off_ready=%s",
                engagement.engagement_id, memo.n_errors, memo.n_warnings,
                memo.sign_off_ready,
            )
            return

        # Fallback: deterministic rollup card when LLM is unavailable.
        await turn_context.send_activity(
            MessageFactory.attachment(
                CardFactory.adaptive_card(self._render_cpa_memo(memo))
            )
        )
        log.info(
            "agent.cpa_reviewer.done engagement=%s source=deterministic "
            "errors=%d warnings=%d sign_off_ready=%s",
            engagement.engagement_id, memo.n_errors, memo.n_warnings,
            memo.sign_off_ready,
        )

    _PROFILE_FIELDS: tuple[str, ...] = (
        "legal_name", "industry", "province", "fiscal_year_end",
        "gst_hst_registered", "qst_registered", "pst_registered",
        "quick_method_elected", "has_inventory", "has_payroll",
        "prior_year_filed", "notes",
    )

    def _render_profile_form(
        self, engagement: Engagement, inferred: dict[str, str]
    ) -> dict[str, Any]:
        """Render the profile card with inferred defaults substituted in each field."""
        n_prefilled = sum(1 for f in self._PROFILE_FIELDS if inferred.get(f))
        if n_prefilled:
            prefill_note = (
                f"Pre-filled {n_prefilled} field(s) from your uploaded files — "
                "please confirm or correct and fill any gaps, then Save. "
                "Skip to proceed without (slower for monthly cycles)."
            )
        else:
            prefill_note = (
                "Tell me about this client once — it makes the audit drop false "
                "positives and skip redundant questions. Skip if you want; the "
                "audit still runs, it'll just ask for the missing pieces."
            )
        values = {"engagementId": engagement.engagement_id, "prefillNote": prefill_note}
        for field in self._PROFILE_FIELDS:
            values[field] = inferred.get(field, "")
        return _substitute(
            json.loads(CLIENT_PROFILE_CARD_PATH.read_text(encoding="utf-8")),
            values,
        )

    def _infer_profile_from_docs(self, engagement: Engagement) -> dict[str, str]:
        """Parse BS + P&L + Journal to pre-fill client-profile fields.

        Saves the bookkeeper from retyping what the files already contain — name,
        fiscal year end, inventory presence, payroll, Quebec tax activity, Quick
        Method signature. Returns a dict with only the fields we could infer; the
        bookkeeper confirms or corrects them in the profile form.

        This is best-effort. Silent on parse failures. Returns `{}` if nothing
        could be inferred.
        """
        profile: dict[str, str] = {}
        eid = engagement.engagement_id

        # --- P&L: legal name ---
        pnl_doc = self.store.latest_document(eid, DOC_PNL)
        if pnl_doc is not None:
            try:
                pnl = parse_pnl(Path(pnl_doc.file_path))
                if pnl.company:
                    profile["legal_name"] = pnl.company
            except Exception:
                pass

        # --- Balance Sheet: legal name (fallback), fiscal year end, inventory flag ---
        bs_doc = self.store.latest_document(eid, DOC_BALANCE_SHEET)
        if bs_doc is not None:
            try:
                bs = parse_balance_sheet(Path(bs_doc.file_path))
                if "legal_name" not in profile and bs.company:
                    profile["legal_name"] = bs.company
                if bs.as_of:
                    profile["fiscal_year_end"] = bs.as_of.strftime("%m-%d")
                # Inventory presence
                inventory_present = any(
                    any(tok in l.name.lower() for tok in
                        ("inventory", "stock on hand", "merchandise", "work in progress"))
                    and l.amount is not None
                    for l in bs.lines
                )
                if inventory_present:
                    profile["has_inventory"] = "yes"
            except Exception:
                pass

        # --- Journal: province, Quick Method, payroll, GST/HST registration ---
        journal_doc = self.store.latest_document(eid, DOC_JOURNAL)
        if journal_doc is not None:
            try:
                jr = parse_journal_csv(Path(journal_doc.file_path))
                lines = jr.lines

                # Payroll accounts → has_payroll
                payroll_keys = ("wages", "salaries", "salary", "payroll", "cpp", " ei ", "cpp/ei")
                if any(any(k in l.account.lower() for k in payroll_keys) for l in lines):
                    profile["has_payroll"] = "yes"

                # GST/HST account activity with credits → registered for GST/HST
                tax_lines = [
                    l for l in lines
                    if any(k in l.account.lower() for k in ("gst", "hst", "qst", "tvq", "tps"))
                ]
                if any(l.credit > _ZERO for l in tax_lines):
                    profile["gst_hst_registered"] = "yes"

                # QST registration: QST account or 14.975% vendor pattern
                qst_account_present = any(
                    any(k in l.account.lower() for k in ("qst", "tvq"))
                    for l in tax_lines
                )
                if qst_account_present:
                    profile["qst_registered"] = "yes"

                # Province + Quick Method from vendor tax-rate pattern
                from src.agents.tax_auditor import (
                    _compute_vendor_stats, _detect_quick_method_pattern,
                )
                vendors = _compute_vendor_stats(jr)
                if _detect_quick_method_pattern(vendors):
                    profile["quick_method_elected"] = "yes"

                # Province: rate clustering
                material = [v for v in vendors if v.spend >= Decimal("100") and v.tax > _ZERO]
                if material:
                    tol = Decimal("0.3")
                    bucket_counts: dict[str, int] = {}
                    for v in material:
                        r = v.implied_rate
                        if abs(r - Decimal("14.975")) <= tol:
                            bucket = "QC"
                        elif abs(r - Decimal("13")) <= tol:
                            bucket = "ON"
                        elif abs(r - Decimal("15")) <= tol:
                            bucket = "ATL"
                        elif abs(r - Decimal("5")) <= tol:
                            bucket = "GST_ONLY"
                        else:
                            bucket = "OTHER"
                        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
                    if bucket_counts:
                        top_bucket = max(bucket_counts, key=bucket_counts.get)
                        if top_bucket == "QC":
                            profile["province"] = "QC"
                            profile.setdefault("qst_registered", "yes")
                        elif top_bucket == "ON":
                            profile["province"] = "ON"
            except Exception:
                pass

        return profile

    def _infer_company(self, engagement: Engagement) -> str | None:
        """Best-effort lookup: parse the latest BS/P&L/journal title rows, if present."""
        eid = engagement.engagement_id
        for doc_type, parse in (
            (DOC_BALANCE_SHEET, parse_balance_sheet),
            (DOC_PNL, parse_pnl),
            (DOC_JOURNAL, parse_journal_csv),
        ):
            doc = self.store.latest_document(eid, doc_type)
            if doc is None:
                continue
            try:
                return parse(Path(doc.file_path)).company
            except Exception:
                continue
        return None

    def _bs_highlights(self, engagement: Engagement) -> dict[str, str]:
        doc = self.store.latest_document(engagement.engagement_id, DOC_BALANCE_SHEET)
        if doc is None:
            return {}
        try:
            bs = parse_balance_sheet(Path(doc.file_path))
        except Exception:
            return {}
        out: dict[str, str] = {}
        for name in ("Total Assets", "Total Liabilities", "Total Equity",
                     "Retained Earnings", "Profit for the year",
                     "GST/HST Payable", "GST/HST Suspense"):
            amt = bs.amount_of(name)
            if amt is not None:
                out[name] = f"${amt:,.2f}"
        return out

    def _pnl_highlights(self, engagement: Engagement) -> dict[str, str]:
        doc = self.store.latest_document(engagement.engagement_id, DOC_PNL)
        if doc is None:
            return {}
        try:
            pl = parse_pnl(Path(doc.file_path))
        except Exception:
            return {}
        out: dict[str, str] = {}
        for name in ("Total Income", "Total Cost of Goods Sold",
                     "GROSS PROFIT", "Total Expenses", "PROFIT"):
            amt = pl.amount_of(name)
            if amt is not None:
                out[name] = f"${amt:,.2f}"
        return out

    # Role badges for the memo card. A visually distinct tag per responsible
    # party so the bookkeeper can scan for their items at a glance.
    _ROLE_BADGES = {
        "bookkeeper":  "📒 Bookkeeper",
        "cpa":         "🎓 CPA",
        "client":      "🧑‍💼 Client",
        "shareholder": "👤 Shareholder",
    }

    def _render_finding(self, f: "agent_cpa_reviewer.LlmFinding") -> str:
        """Format one finding as a markdown block. The plain-language action is
        surfaced distinctly so the responsible party knows exactly what to do."""
        role = (f.responsible or "").strip().lower()
        badge = self._ROLE_BADGES.get(role, f"👤 {role or '—'}")
        pri = f"P{f.priority}"
        lines = [f"**{pri} · {badge}** — {f.title}"]
        if f.detail:
            lines.append(f"  _{f.detail}_")
        if f.plain_language_action:
            lines.append(f"  ▶ **Action:** {f.plain_language_action}")
        return "\n".join(lines)

    def _render_cpa_memo_llm(
        self, memo: "agent_cpa_reviewer.Memo", llm: "agent_cpa_reviewer.LlmReviewOutput"
    ) -> dict[str, Any]:
        def bullets(items: list[str], empty: str) -> str:
            return "\n\n".join(f"• {i}" for i in items) if items else f"_{empty}_"

        def findings_section(
            findings: list["agent_cpa_reviewer.LlmFinding"], empty: str
        ) -> str:
            if not findings:
                return f"_{empty}_"
            ordered = sorted(findings, key=lambda x: x.priority)
            return "\n\n".join(self._render_finding(f) for f in ordered)

        if not llm.sign_off_ready or llm.blocking_issues:
            color = "attention"
        elif memo.n_warnings > 0 or llm.judgment_notes:
            color = "warning"
        else:
            color = "good"

        adj_lines = [
            f"• **DR** {a.debit_account}  **CR** {a.credit_account}  **${a.amount}**  — {a.description}"
            for a in llm.proposed_adjustments
        ]
        adjustments_md = "\n\n".join(adj_lines) if adj_lines else "_None — all corrections require client input (see questions below)._"

        return _substitute(
            json.loads(CPA_MEMO_LLM_CARD_PATH.read_text(encoding="utf-8")),
            {
                "company":          memo.company,
                "period":           memo.period,
                "engagementId":     memo.engagement_id,
                "journalStats":     self._journal_stats_line(memo.engagement_id),
                "bsStats":          self._bs_stats_line(memo.engagement_id),
                "pnlStats":         self._pnl_stats_line(memo.engagement_id),
                "executiveSummary": llm.executive_summary,
                "summaryColor":     color,
                "blockingMarkdown":    findings_section(llm.blocking_issues, "None — no blocking issues."),
                "judgmentMarkdown":    findings_section(llm.judgment_notes, "None — nothing discretionary flagged."),
                "adjustmentsMarkdown": adjustments_md,
                "questionsMarkdown":   bullets(llm.questions_for_client, "None — no open questions for the client."),
            },
        )

    def _journal_stats_line(self, engagement_id: str) -> str:
        """One-line journal summary for the memo-card header."""
        doc = self.store.latest_document(engagement_id, DOC_JOURNAL)
        if doc is None:
            return "—"
        try:
            r = parse_journal_csv(Path(doc.file_path))
        except Exception:
            return "(parse failed)"
        total_debit = sum((l.debit for l in r.lines), _ZERO)
        balanced = "✓" if not r.unbalanced_groups() else f"⚠ {len(r.unbalanced_groups())} unbalanced"
        return (
            f"{len(r.groups())} entries / {len(r.lines)} lines, "
            f"${total_debit:,.2f} activity, trial balance {balanced}"
        )

    def _bs_stats_line(self, engagement_id: str) -> str:
        doc = self.store.latest_document(engagement_id, DOC_BALANCE_SHEET)
        if doc is None:
            return "—"
        try:
            bs = parse_balance_sheet(Path(doc.file_path))
        except Exception:
            return "(parse failed)"
        ta = bs.amount_of("Total Assets") or _ZERO
        tle = bs.amount_of("Total Liabilities and Equity") or _ZERO
        identity = "✓" if ta == tle else f"⚠ ${ta:,.2f} vs ${tle:,.2f}"
        basis = f" ({bs.basis})" if bs.basis else ""
        return f"Total Assets ${ta:,.2f}, identity {identity}{basis}"

    def _pnl_stats_line(self, engagement_id: str) -> str:
        doc = self.store.latest_document(engagement_id, DOC_PNL)
        if doc is None:
            return "—"
        try:
            pl = parse_pnl(Path(doc.file_path))
        except Exception:
            return "(parse failed)"
        income = pl.amount_of("Total Income") or _ZERO
        profit = pl.amount_of("PROFIT")
        if profit is None:
            return f"Income ${income:,.2f}"
        margin = (profit / income * 100) if income > _ZERO else _ZERO
        return f"Income ${income:,.2f}, Profit ${profit:,.2f} ({margin:.1f}%)"

    def _render_cpa_memo(self, memo: "agent_cpa_reviewer.Memo") -> dict[str, Any]:
        def fmt(lines: list[str], empty: str) -> str:
            return "\n\n".join(lines) if lines else f"_{empty}_"

        if memo.n_errors > 0:
            color = "attention"
        elif memo.n_warnings > 0:
            color = "warning"
        else:
            color = "good"

        return _substitute(
            json.loads(CPA_MEMO_CARD_PATH.read_text(encoding="utf-8")),
            {
                "company":          memo.company,
                "period":           memo.period,
                "engagementId":     memo.engagement_id,
                "totalChecks":      str(memo.total_checks),
                "nErrors":          str(memo.n_errors),
                "nWarnings":        str(memo.n_warnings),
                "nInfo":            str(memo.n_info),
                "nOk":              str(memo.n_ok),
                "executiveSummary": memo.executive_summary,
                "summaryColor":     color,
                "actionsMarkdown":  fmt(memo.actions_required, "No blocking issues."),
                "warningsMarkdown": fmt(memo.recommend_review, "No warnings."),
                "contextMarkdown":  fmt(memo.context, "—"),
            },
        )

    def _render_findings_card(
        self, agent_label: str, findings: list[Finding]
    ) -> dict[str, Any]:
        """Render a findings-list card for any agent."""
        findings = sort_findings(findings)
        n_err = sum(1 for f in findings if f.severity == SEVERITY_ERROR)
        n_warn = sum(1 for f in findings if f.severity == SEVERITY_WARN)
        summary = (
            f"{len(findings)} checks"
            + (f" · {n_err} error" if n_err else "")
            + (f"s" if n_err > 1 else "")
            + (f" · {n_warn} warning" if n_warn else "")
            + (f"s" if n_warn > 1 else "")
        )
        lines: list[str] = []
        for f in findings:
            icon = SEVERITY_ICONS.get(f.severity, "•")
            lines.append(f"**{icon} {f.title}**")
            if f.detail:
                for dl in f.detail.splitlines():
                    dl = dl.strip()
                    if dl:
                        lines.append(f"  {dl}")
            if f.proposed_fix:
                lines.append(f"  _Fix:_ {f.proposed_fix}")
            lines.append("")  # blank line between findings
        markdown = "\n\n".join(lines).rstrip()

        return _substitute(
            json.loads(AGENT_FINDINGS_CARD_PATH.read_text(encoding="utf-8")),
            {
                "agentTitle": agent_label,
                "summaryLine": summary,
                "findingsMarkdown": markdown,
            },
        )

    async def _summarize_statement(
        self, turn_context: TurnContext, path: Path, doc_type: str
    ) -> None:
        """Parse the uploaded BS or P&L and post a summary adaptive card."""
        try:
            if doc_type == DOC_BALANCE_SHEET:
                stmt = parse_balance_sheet(path)
            else:
                stmt = parse_pnl(path)
        except Exception:
            log.exception("statement.parse_failed path=%s type=%s", path, doc_type)
            label = "Balance Sheet" if doc_type == DOC_BALANCE_SHEET else "P&L"
            await turn_context.send_activity(
                MessageFactory.text(
                    f"I saved the {label} but couldn't parse it. Export it from QBO as "
                    f"an Excel file (.xlsx) and re-upload."
                )
            )
            return

        card = self._render_statement_summary(stmt)
        await turn_context.send_activity(
            MessageFactory.attachment(CardFactory.adaptive_card(card))
        )

    async def _download_attachment(
        self, engagement: Engagement, attachment: Attachment
    ) -> Path:
        """Download a Teams file attachment to the engagement directory."""
        content: dict[str, Any] = attachment.content or {}
        download_url: str | None = content.get("downloadUrl")
        if not download_url:
            raise ValueError(f"Attachment `{attachment.name}` has no downloadUrl")

        target_dir = self.uploads_root / engagement.engagement_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / _safe_filename(attachment.name or "upload.bin")

        async with aiohttp.ClientSession() as session:
            async with session.get(download_url) as resp:
                resp.raise_for_status()
                with target_path.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
        return target_path

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _clean_text(turn_context: TurnContext) -> str:
        raw = TurnContext.remove_recipient_mention(turn_context.activity) or ""
        return _HTML_TAG_RE.sub("", raw).strip()

    @staticmethod
    def _conversation_type(turn_context: TurnContext) -> str:
        conv = turn_context.activity.conversation
        raw = (getattr(conv, "conversation_type", None) or "").lower()
        if raw == "personal":
            return CONV_PERSONAL
        if raw == "channel":
            return CONV_CHANNEL
        if raw == "groupchat":
            return CONV_GROUP
        return raw or CONV_PERSONAL

    @staticmethod
    def _user_aad_id(turn_context: TurnContext) -> str | None:
        sender = turn_context.activity.from_property
        return getattr(sender, "aad_object_id", None) or getattr(sender, "id", None)

    @staticmethod
    def _non_mention_attachments(attachments: list[Attachment] | None) -> list[Attachment]:
        if not attachments:
            return []
        return [a for a in attachments if a.content_type != "text/html"]

    @staticmethod
    def _render_intake_card(engagement: Engagement) -> dict[str, Any]:
        card = json.loads(INTAKE_CARD_PATH.read_text(encoding="utf-8"))
        return _substitute(card, {
            "periodDescription": engagement.period_description or "(unspecified period)",
            "engagementId": engagement.engagement_id,
        })

    @staticmethod
    def _render_statement_summary(stmt: FinancialStatement) -> dict[str, Any]:
        card = json.loads(STATEMENT_SUMMARY_CARD_PATH.read_text(encoding="utf-8"))

        if stmt.report_type == REPORT_BALANCE_SHEET:
            title = "Balance Sheet"
            key_names = [
                "Total Assets",
                "Total Liabilities",
                "Total Equity",
                "Retained Earnings",
            ]
            total_assets = stmt.amount_of("Total Assets") or _ZERO
            total_leq = stmt.amount_of("Total Liabilities and Equity") or _ZERO
            diff = total_assets - total_leq
            if diff == 0:
                integrity = "Accounting identity ✓  (Assets = Liabilities + Equity)"
                integrity_color = "good"
            else:
                integrity = (
                    f"⚠ Assets ({total_assets:,.2f}) ≠ Liabilities+Equity "
                    f"({total_leq:,.2f}), diff {diff:,.2f}"
                )
                integrity_color = "attention"
        else:
            title = "Profit & Loss"
            key_names = ["Total Income", "Total Cost of Goods Sold",
                         "GROSS PROFIT", "Total Expenses", "PROFIT"]
            total_income = stmt.amount_of("Total Income") or _ZERO
            total_exp = stmt.amount_of("Total Expenses") or _ZERO
            gross = stmt.amount_of("GROSS PROFIT")
            profit = stmt.amount_of("PROFIT")
            parts = []
            if gross is not None:
                implied_cogs = total_income - gross
                parts.append(f"Gross margin: {gross:,.2f} (implied COGS {implied_cogs:,.2f})")
            if profit is not None:
                margin = (profit / total_income * 100) if total_income else _ZERO
                parts.append(f"Net margin: {margin:.1f}%")
            integrity = "  ·  ".join(parts) if parts else ""
            integrity_color = "default"

        key_figures_lines = []
        for name in key_names:
            amt = stmt.amount_of(name)
            if amt is not None:
                key_figures_lines.append(f"• {name}: ${amt:,.2f}")
        key_figures = "\n".join(key_figures_lines) or "—"

        return _substitute(card, {
            "reportTitle":     title,
            "company":         stmt.company or "(unknown)",
            "period":          stmt.period_label or "—",
            "asOf":            stmt.as_of.isoformat() if stmt.as_of else "—",
            "basis":           stmt.basis or "—",
            "lines":           str(len(stmt.lines)),
            "keyFigures":      key_figures,
            "integrityCheck":  integrity,
            "integrityColor":  integrity_color,
        })

    @staticmethod
    def _render_journal_summary(report: JournalReport) -> dict[str, Any]:
        card = json.loads(JOURNAL_SUMMARY_CARD_PATH.read_text(encoding="utf-8"))
        lines = report.lines
        groups = report.groups()
        total_debit = sum((l.debit for l in lines), _ZERO)
        total_credit = sum((l.credit for l in lines), _ZERO)
        trial_ok = total_debit == total_credit
        unbalanced = report.unbalanced_groups()
        if trial_ok and not unbalanced:
            trial_text = f"${total_debit:,.2f} Dr / ${total_credit:,.2f} Cr ✓"
        elif unbalanced:
            trial_text = (
                f"⚠ {len(unbalanced)} unbalanced entries: "
                + ", ".join(unbalanced[:5])
                + ("…" if len(unbalanced) > 5 else "")
            )
        else:
            trial_text = f"⚠ ${total_debit:,.2f} Dr ≠ ${total_credit:,.2f} Cr"

        dates = sorted({l.txn_date for l in lines}) if lines else []
        date_range = f"{dates[0].isoformat()} → {dates[-1].isoformat()}" if dates else "—"

        from collections import Counter
        counter = Counter(l.account for l in lines if l.account)
        top = counter.most_common(5)
        top_text = "\n".join(f"• {acct} — {n} lines" for acct, n in top) or "—"

        return _substitute(card, {
            "company":      report.company or "(unknown)",
            "period":       report.period or "(unknown)",
            "dateRange":    date_range,
            "entries":      str(len(groups)),
            "lines":        str(len(lines)),
            "accounts":     str(len({l.account for l in lines if l.account})),
            "trialBalance": trial_text,
            "topAccounts":  top_text,
        })

    @staticmethod
    def _help_text() -> str:
        return (
            "Hi — I'm the Audit Bot. I work in two modes:\n\n"
            "🧹 **Cleanup mode** — I walk you through the 8-step bookkeeping cleanup SOP "
            "before anything gets audited. Use when the books aren't period-closed yet.\n\n"
            "`new cleanup <period>` (example: `new cleanup Q3 2026`)\n\n"
            "🔍 **Audit mode** — I run four audit agents over your exported files and "
            "produce a CPA review memo. Use after cleanup is done.\n\n"
            "`new audit <period>` (example: `new audit Q3 2026`) then drop the Journal, "
            "Balance Sheet, P&L (and optionally bank statements) and type `ready`."
        )


def _classify_doc_type(filename: str | None) -> str:
    name = (filename or "").lower()
    if any(k in name for k in ("journal", "gl", "general_ledger", "general ledger")):
        return DOC_JOURNAL
    if any(k in name for k in ("balance", "bs_", " bs.", "_bs.", "balancesheet")):
        return DOC_BALANCE_SHEET
    if any(k in name for k in ("p&l", "pnl", "p_l", "profit", "income_statement", "income statement")):
        return DOC_PNL
    if any(k in name for k in ("bank", "statement", "credit_card", "creditcard", " cc ", "_cc_")):
        return DOC_BANK_STATEMENT
    return "unknown"


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\- ]+", "_", name).strip()
    return cleaned or "upload.bin"


def _substitute(node: Any, values: dict[str, str]) -> Any:
    """Minimal ${var} substitution across the adaptive-card JSON tree."""
    if isinstance(node, str):
        out = node
        for key, val in values.items():
            out = out.replace("${" + key + "}", val)
        return out
    if isinstance(node, list):
        return [_substitute(item, values) for item in node]
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    return node
