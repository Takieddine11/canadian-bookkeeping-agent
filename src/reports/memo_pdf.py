"""Generate a professional CPA audit-memo PDF for Teams attachment.

The Teams card is space-constrained; the bookkeeper sees a short summary
with P1/P2 counts and a sign-off badge. Everything else — the full
Tier-4/3/2 sections, the per-item tables, the counter-arguments, the
AJE schedule, the document requests, the filing deadlines — goes in
the PDF attached to the same Teams message.

The PDF is styled to look like a compilation-review memo that a CPA
would hand to a bookkeeper or a client. It's designed to be readable
as an email attachment and printable on Letter paper.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger(__name__)


# ---- Styling ----------------------------------------------------------------


_PALETTE = {
    "header_bg":      colors.HexColor("#1F3A5F"),
    "header_fg":      colors.white,
    "tier4_bg":       colors.HexColor("#FCE4E4"),
    "tier4_border":   colors.HexColor("#C53030"),
    "tier3_bg":       colors.HexColor("#FFF4E1"),
    "tier3_border":   colors.HexColor("#B7791F"),
    "tier2_bg":       colors.HexColor("#FEF9C3"),
    "tier2_border":   colors.HexColor("#854D0E"),
    "badge_fail":     colors.HexColor("#C53030"),
    "badge_ok":       colors.HexColor("#15803D"),
    "muted":          colors.HexColor("#64748B"),
    "rule":           colors.HexColor("#D4D4D8"),
    "body":           colors.HexColor("#1E293B"),
}


def _build_styles() -> dict:
    base = getSampleStyleSheet()
    styles: dict[str, ParagraphStyle] = {}

    styles["title"] = ParagraphStyle(
        "title", parent=base["Heading1"],
        fontName="Helvetica-Bold", fontSize=20, leading=24,
        textColor=_PALETTE["body"], spaceAfter=2, alignment=TA_LEFT,
    )
    styles["subtitle"] = ParagraphStyle(
        "subtitle", parent=base["BodyText"],
        fontName="Helvetica", fontSize=11, leading=14,
        textColor=_PALETTE["muted"], spaceAfter=8,
    )
    styles["h1"] = ParagraphStyle(
        "h1", parent=base["Heading2"],
        fontName="Helvetica-Bold", fontSize=14, leading=18,
        textColor=_PALETTE["body"], spaceBefore=14, spaceAfter=6,
    )
    styles["h1_t4"] = ParagraphStyle(
        "h1_t4", parent=base["Heading2"],
        fontName="Helvetica-Bold", fontSize=14, leading=18,
        textColor=_PALETTE["tier4_border"], spaceBefore=14, spaceAfter=6,
    )
    styles["h1_t3"] = ParagraphStyle(
        "h1_t3", parent=base["Heading2"],
        fontName="Helvetica-Bold", fontSize=14, leading=18,
        textColor=_PALETTE["tier3_border"], spaceBefore=14, spaceAfter=6,
    )
    styles["h2"] = ParagraphStyle(
        "h2", parent=base["Heading3"],
        fontName="Helvetica-Bold", fontSize=12, leading=16,
        textColor=_PALETTE["body"], spaceBefore=10, spaceAfter=4,
    )
    styles["body"] = ParagraphStyle(
        "body", parent=base["BodyText"],
        fontName="Helvetica", fontSize=10, leading=14,
        textColor=_PALETTE["body"], spaceAfter=6,
    )
    styles["body_italic"] = ParagraphStyle(
        "body_italic", parent=styles["body"],
        fontName="Helvetica-Oblique",
    )
    styles["finding_title"] = ParagraphStyle(
        "finding_title", parent=base["Heading3"],
        fontName="Helvetica-Bold", fontSize=11, leading=14,
        textColor=_PALETTE["body"], spaceBefore=6, spaceAfter=3,
    )
    styles["finding_body"] = ParagraphStyle(
        "finding_body", parent=styles["body"],
        leftIndent=0, spaceAfter=4,
    )
    styles["action_label"] = ParagraphStyle(
        "action_label", parent=styles["body"],
        fontName="Helvetica-Bold", textColor=_PALETTE["badge_ok"],
        spaceAfter=2,
    )
    styles["counter_label"] = ParagraphStyle(
        "counter_label", parent=styles["body"],
        fontName="Helvetica-Bold", textColor=_PALETTE["muted"],
        spaceAfter=2,
    )
    styles["per_item_row"] = ParagraphStyle(
        "per_item_row", parent=styles["body"],
        fontName="Helvetica", fontSize=9, leading=12,
        leftIndent=12, spaceAfter=2,
    )
    styles["footer"] = ParagraphStyle(
        "footer", parent=base["BodyText"],
        fontName="Helvetica", fontSize=8, leading=10,
        textColor=_PALETTE["muted"], alignment=TA_CENTER,
    )
    styles["bullet"] = ParagraphStyle(
        "bullet", parent=styles["body"],
        leftIndent=14, bulletIndent=0, spaceAfter=3,
    )
    return styles


import re as _re

# reportlab's default Helvetica doesn't ship emoji glyphs — render them as
# "?" would be ugly, so we strip them entirely for PDF output. The Teams
# card still shows emoji; the PDF is a clean text-only document.
_EMOJI_RE = _re.compile(
    "["
    "\U0001F300-\U0001F9FF"     # symbols + pictographs (most emoji)
    "\U0001FA00-\U0001FAFF"     # extended pictographs
    "\U00002600-\U000027BF"     # misc symbols + dingbats (✓ ✗ ☕ etc.)
    "\U0001F000-\U0001F2FF"     # mahjong / domino / playing cards
    "]+",
    flags=_re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """Remove emoji characters — Helvetica doesn't have glyphs for them."""
    return _EMOJI_RE.sub("", text).replace("  ", " ").strip()


def _safe(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _strip_emoji(str(text))
    return html.escape(cleaned).replace("\n", "<br/>")


def _role_badge(role: str) -> str:
    """Plain-text role label for PDF (no emoji — Helvetica can't render them)."""
    role = (role or "").strip().lower()
    return {
        "bookkeeper":  "Bookkeeper",
        "cpa":         "CPA",
        "client":      "Client",
        "shareholder": "Shareholder",
    }.get(role, role.capitalize() if role else "Unassigned")


# ---- Main entrypoint --------------------------------------------------------


@dataclass(frozen=True)
class MemoContext:
    """Caller-supplied metadata that the LLM doesn't know about."""
    client_name: str
    period_description: str
    engagement_id: str
    basis: str = "Accrual"
    reporting_currency: str = "CAD"


def generate_memo_pdf(
    llm_output,  # type: ignore[no-untyped-def]  # LlmReviewOutput from cpa_reviewer
    ctx: MemoContext,
    output_path: Path,
) -> Path:
    """Render the LLM memo as a PDF file at ``output_path`` and return the path."""
    styles = _build_styles()
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.9 * inch,
        title=f"Audit Review Memo — {ctx.client_name}",
        author="Canadian Bookkeeping Agent",
    )

    story: list = []

    # ---- Header -----------------------------------------------------------
    story.append(Paragraph(
        f"Audit Review Memo — {_safe(ctx.client_name)}", styles["title"]))
    story.append(Paragraph(
        f"Period: {_safe(ctx.period_description)} · Basis: {_safe(ctx.basis)}"
        f" · Reported in {_safe(ctx.reporting_currency)}",
        styles["subtitle"],
    ))

    # Sign-off badge row (plain text — PDF has no emoji glyphs)
    sign_off = bool(getattr(llm_output, "sign_off_ready", False))
    badge_text = "SIGN-OFF READY" if sign_off else "SIGN-OFF BLOCKED"
    badge_bg = _PALETTE["badge_ok"] if sign_off else _PALETTE["badge_fail"]
    badge_tbl = Table(
        [[Paragraph(
            f'<b><font color="white">{_safe(badge_text)}</font></b>',
            styles["body"])]],
        colWidths=[2.25 * inch],
    )
    badge_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), badge_bg),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(badge_tbl)
    story.append(Spacer(1, 12))

    # ---- Executive summary ------------------------------------------------
    exec_summary_long = (
        getattr(llm_output, "memo_executive_summary_long", "")
        or getattr(llm_output, "executive_summary", "")
        or ""
    )
    if exec_summary_long:
        story.append(Paragraph("Executive Summary", styles["h1"]))
        for para in exec_summary_long.split("\n\n"):
            if para.strip():
                story.append(Paragraph(_safe(para.strip()), styles["body"]))

    # ---- Industry context (optional) -------------------------------------
    industry = getattr(llm_output, "industry_context", "") or ""
    if industry.strip():
        story.append(Paragraph("Industry Context", styles["h1"]))
        for para in industry.split("\n\n"):
            if para.strip():
                story.append(Paragraph(_safe(para.strip()), styles["body"]))

    # ---- Blocking issues (Tier 4) ----------------------------------------
    blocking = list(getattr(llm_output, "blocking_issues", []) or [])
    if blocking:
        story.append(Paragraph(
            f"Blocking Issues — Tier 4 ({len(blocking)})", styles["h1_t4"]))
        for finding in sorted(blocking, key=lambda f: getattr(f, "priority", 99)):
            story.extend(_render_finding(finding, styles, _PALETTE["tier4_bg"],
                                          _PALETTE["tier4_border"]))

    # ---- Judgment notes (Tier 3 / Tier 2) --------------------------------
    judgment = list(getattr(llm_output, "judgment_notes", []) or [])
    if judgment:
        story.append(Paragraph(
            f"Judgment Notes &amp; Asks — Tier 3 / Tier 2 ({len(judgment)})",
            styles["h1_t3"]))
        for finding in sorted(judgment, key=lambda f: getattr(f, "priority", 99)):
            story.extend(_render_finding(finding, styles, _PALETTE["tier3_bg"],
                                          _PALETTE["tier3_border"]))

    # ---- Proposed AJEs ---------------------------------------------------
    ajes = list(getattr(llm_output, "proposed_adjustments", []) or [])
    if ajes:
        story.append(Paragraph("Proposed Adjusting Journal Entries",
                               styles["h1"]))
        header = ["#", "Debit", "Credit", "Amount", "Why"]
        rows = [header]
        for i, aje in enumerate(ajes, 1):
            amt_str = ""
            amt = getattr(aje, "amount", None)
            if amt is not None:
                try:
                    amt_str = f"${float(amt):,.2f}"
                except (TypeError, ValueError):
                    amt_str = str(amt)
            # LlmAdjustingEntry uses `description` (not `reason`) and stores
            # `amount` as a decimal string, not a Decimal — accept either.
            reason_text = (getattr(aje, "description", "")
                           or getattr(aje, "reason", "")
                           or "")
            rows.append([
                str(i),
                Paragraph(_safe(getattr(aje, "debit_account", "") or ""),
                          styles["body"]),
                Paragraph(_safe(getattr(aje, "credit_account", "") or ""),
                          styles["body"]),
                amt_str,
                Paragraph(_safe(reason_text), styles["body"]),
            ])
        aje_tbl = Table(
            rows,
            colWidths=[0.4 * inch, 1.6 * inch, 1.6 * inch, 0.9 * inch, 2.5 * inch],
            repeatRows=1,
        )
        aje_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _PALETTE["header_bg"]),
            ("TEXTCOLOR",  (0, 0), (-1, 0), _PALETTE["header_fg"]),
            ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",   (0, 0), (-1, 0), 9),
            ("ALIGN",      (3, 1), (3, -1), "RIGHT"),
            ("VALIGN",     (0, 0), (-1, -1), "TOP"),
            ("GRID",       (0, 0), (-1, -1), 0.5, _PALETTE["rule"]),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING",    (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
            ("TOPPADDING",    (0, 1), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#F8FAFC")]),
        ]))
        story.append(aje_tbl)

    # ---- Questions for client --------------------------------------------
    questions = list(getattr(llm_output, "questions_for_client", []) or [])
    if questions:
        story.append(Paragraph("Questions for the Client", styles["h1"]))
        for i, q in enumerate(questions, 1):
            story.append(Paragraph(
                f"{i}. {_safe(q)}", styles["body"]))

    # ---- Document requests -----------------------------------------------
    docs = list(getattr(llm_output, "document_requests", []) or [])
    if docs:
        story.append(Paragraph("Documents Requested", styles["h1"]))
        for i, d in enumerate(docs, 1):
            story.append(Paragraph(f"{i}. {_safe(d)}", styles["body"]))

    # ---- Filing deadlines ------------------------------------------------
    deadlines = list(getattr(llm_output, "filing_deadlines", []) or [])
    if deadlines:
        story.append(Paragraph("Filing Deadlines", styles["h1"]))
        for d in deadlines:
            story.append(Paragraph(f"• {_safe(d)}", styles["bullet"]))

    # ---- Closing notes ---------------------------------------------------
    closing = getattr(llm_output, "closing_notes", "") or ""
    if closing.strip():
        story.append(Paragraph("Closing Notes", styles["h1"]))
        for para in closing.split("\n\n"):
            if para.strip():
                story.append(Paragraph(_safe(para.strip()), styles["body"]))

    # ---- Footer page-template ------------------------------------------
    def _on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(_PALETTE["muted"])
        footer = (
            f"Audit Review Memo — {ctx.client_name} · Engagement {ctx.engagement_id}"
            f" · Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            f" · Page {doc.page}"
        )
        canvas.drawCentredString(
            LETTER[0] / 2, 0.4 * inch, footer)
        canvas.restoreState()

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    log.info(
        "memo_pdf.generated path=%s client=%s engagement=%s",
        output_path, ctx.client_name, ctx.engagement_id,
    )
    return output_path


def _render_finding(finding, styles: dict, bg_color, border_color) -> list:
    """Render one finding as a boxed section: title, detail, action,
    counter-argument, per-item rows."""
    blocks: list = []

    role = _role_badge(getattr(finding, "responsible", ""))
    priority = getattr(finding, "priority", "")
    title = getattr(finding, "title", "") or ""
    header = f"<b>P{_safe(priority)} · {_safe(role)}</b> — {_safe(title)}"
    blocks.append(Paragraph(header, styles["finding_title"]))

    # Short card-level detail first
    detail = getattr(finding, "detail", "") or ""
    if detail:
        blocks.append(Paragraph(_safe(detail), styles["body_italic"]))

    # Rich memo evidence_detail — 1-3 paragraphs
    evidence = getattr(finding, "evidence_detail", "") or ""
    if evidence:
        for para in evidence.split("\n\n"):
            if para.strip():
                blocks.append(Paragraph(_safe(para.strip()),
                                         styles["finding_body"]))

    # Per-item rows (if any)
    rows = list(getattr(finding, "per_item_rows", []) or [])
    if rows:
        for row in rows:
            blocks.append(Paragraph(f"• {_safe(row)}", styles["per_item_row"]))

    # Action (plain-text label — no emoji in PDF)
    action = getattr(finding, "plain_language_action", "") or ""
    if action:
        blocks.append(Paragraph(
            f'<b><font color="#15803D">Action:</font></b> {_safe(action)}',
            styles["finding_body"]))

    # Counter-argument (self-challenge)
    counter = getattr(finding, "counter_argument", "") or ""
    if counter:
        blocks.append(Paragraph(
            f'<b><font color="#64748B">Challenge this:</font></b> {_safe(counter)}',
            styles["finding_body"]))

    # Wrap in a thin left-accent rule
    inner = [[block] for block in blocks]
    tbl = Table(inner, colWidths=[6.75 * inch])
    tbl.setStyle(TableStyle([
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("BACKGROUND",    (0, 0), (-1, -1), bg_color),
        ("LINEBEFORE",    (0, 0), (0, -1), 3, border_color),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    return [KeepTogether(tbl), Spacer(1, 6)]
