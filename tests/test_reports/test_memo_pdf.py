"""Smoke tests for the CPA-memo PDF generator.

These are not visual-regression tests — we verify the PDF is produced
cleanly from both a rich LlmReviewOutput and a minimal one, and that
the byte content is a valid PDF (begins with the PDF magic number).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.cpa_reviewer import (
    LlmAdjustingEntry,
    LlmFinding,
    LlmReviewOutput,
)
from src.reports.memo_pdf import MemoContext, generate_memo_pdf


def _rich_memo() -> LlmReviewOutput:
    """Representative full-fidelity memo — all rich fields populated."""
    return LlmReviewOutput(
        executive_summary=(
            "Five Tier-4 blocking issues and three Tier-3 items. Sign-off blocked."
        ),
        memo_executive_summary_long=(
            "The file presents as clean on the surface — opening RE ties to the "
            "filed T2, the accounting identity holds, monthly revenue cadence is "
            "consistent. But a deeper read surfaces five material Tier-4 "
            "classification errors and three judgment items.\n\n"
            "The biggest single issue is $72,000 of cosmetic revenue coded as "
            "GST/QST-exempt. At 14.975%, retroactive exposure is approximately "
            "$10,782 of uncollected tax — voluntary disclosure is the right "
            "procedural path. Mixed-use ITC apportionment at 100% compounds the "
            "exposure by an additional $15–20K over the year.\n\n"
            "Sign-off blocked pending resolution of items 1-5 and client "
            "confirmation on the two Tier-2 asks."
        ),
        industry_context=(
            "SCIAN 621210 (Dentist professional corporation). Cosmetic service "
            "supplies are TAXABLE under ETA Sch V Part II s.1(1) — not exempt. "
            "ODQ professional dues follow the individual dentist and cannot be "
            "deducted at the corporate level without a T4 benefit. Mixed-use "
            "ITC apportionment (ETA s.169(1)) required when both exempt and "
            "taxable supplies are made."
        ),
        blocking_issues=[
            LlmFinding(
                priority=1,
                responsible="bookkeeper",
                title="Cosmetic revenue $72,000 coded as Exempt — should be Taxable",
                detail=(
                    "All revenue coded as 'Revenue - Dental (Exempt)' including "
                    "cosmetic services. ETA Sch V Part II s.1(1) excludes cosmetic "
                    "service supplies from the medical exemption."
                ),
                plain_language_action=(
                    "Reclassify every cosmetic revenue entry for the year with "
                    "the GST/QST QC tax code. File a Voluntary Disclosure with "
                    "RQ before they initiate audit."
                ),
                counter_argument=(
                    "If a portion of the $72K covers medically-indicated whitening "
                    "(e.g., tetracycline-stain treatment ordered by an MD), that "
                    "portion is exempt. Rough estimate: >80% is purely aesthetic."
                ),
                evidence_detail=(
                    "Per intake, revenue breakdown is RAMQ $185K + Private medical "
                    "$425K + Cosmetic $72K + Other $18K. Every monthly JE codes the "
                    "entire amount to 'Revenue - Dental (Exempt)' with tax code E. "
                    "The cosmetic portion is consistently misclassified.\n\n"
                    "Retroactive exposure: $72,000 × 14.975% = $10,782 of GST/QST "
                    "that should have been collected in 2025 but was not. Plus "
                    "interest, plus possible gross-negligence penalty. CRA and RQ "
                    "target cosmetic medical practices on this specific error and "
                    "the look-back is 4 years. The corporation bears the retroactive "
                    "liability even though patients were not originally charged."
                ),
                per_item_rows=[
                    "2025-01-15  $6,200.00  JE-2025-003 (whitening + cosmetic veneers)",
                    "2025-02-20  $6,800.00  JE-2025-013 (bundled into private)",
                    "2025-03-20  $7,100.00  JE-2025-019 (bundled)",
                ],
            ),
            LlmFinding(
                priority=2,
                responsible="cpa",
                title="Mixed-use ITC apportionment — ~$15-20K over-claimed",
                detail=(
                    "Taxable activity is ~10% of revenue but ITCs claimed at 100%."
                ),
                plain_language_action=(
                    "Rebuild ITCs for the year at the 10% apportionment + amend Q1/Q2/Q3."
                ),
                counter_argument=(
                    "Direct-attribution of inputs specifically used for cosmetic "
                    "procedures would yield a higher recoverable ITC; sampling of "
                    "each input category is the right methodology."
                ),
                evidence_detail=(
                    "Taxable-activity ratio: $72K cosmetic ÷ $700K total = 10.3%. "
                    "Current ITC claims are at 100% across dental supplies, lab "
                    "work, rent, equipment — each needs review.\n\n"
                    "Estimated over-claim: dental supplies ~$2,600, lab work "
                    "~$1,100, scanner $45K × 14.975% = $6,700 if capitalized and "
                    "used mostly for cosmetic, software $1,600. Combined "
                    "~$12–15K plus smaller items."
                ),
            ),
        ],
        judgment_notes=[
            LlmFinding(
                priority=10,
                responsible="client",
                title="Vegas conference — 5-night stay exceeds 3-day CE agenda",
                detail=(
                    "Dr. Rousseau's March Vegas trip: flight + 5 nights MGM Grand "
                    "+ registration + meals. Client noted seeing the Strip."
                ),
                plain_language_action=(
                    "Please provide the ODQ CE agenda with start/end dates."
                ),
                counter_argument=(
                    "Some dental conferences run 5 full days including weekends; "
                    "ask before concluding 2 personal nights."
                ),
            ),
        ],
        proposed_adjustments=[
            LlmAdjustingEntry(
                debit_account="Fixed Assets — Dental Equipment (Class 8)",
                credit_account="Small Equipment and Tools",
                amount="45000.00",
                description="Capitalize intraoral scanner; expensed in error.",
            ),
            LlmAdjustingEntry(
                debit_account="Dental Lab Costs (new COGS)",
                credit_account="Office Supplies",
                amount="24600.00",
                description="Reclassify 6 lab-work bills out of Office Supplies.",
            ),
        ],
        questions_for_client=[
            "Vegas conference: agenda with start/end dates?",
            "Golf clubs ($2,800): what's the business use scenario?",
        ],
        document_requests=[
            "ODQ Vegas CE agenda",
            "Landlord GST/QST registration numbers (Immeubles Rosemont Médical)",
            "2025 km log for Dr. Rousseau's Hyundai lease",
        ],
        filing_deadlines=[
            "Q4 2025 GST/QST — Jan 31, 2026",
            "T4 + RL-1 + taxable-benefit adjustments — Feb 28, 2026",
            "T5 + RL-3 for $40K dividend — Feb 28, 2026",
        ],
        closing_notes=(
            "This file's pattern — tidy on the surface, multiple classification "
            "errors underneath — is common for generalist bookkeepers handling "
            "professional-corp files. Consider a quarterly check-in for FY2026 "
            "to catch these issues at the source rather than at year-end."
        ),
        sign_off_ready=False,
    )


def _minimal_memo() -> LlmReviewOutput:
    """Minimal case — only the required short fields are populated."""
    return LlmReviewOutput(
        executive_summary="Clean file. No issues found.",
        blocking_issues=[],
        judgment_notes=[],
        proposed_adjustments=[],
        questions_for_client=[],
        sign_off_ready=True,
    )


def test_generate_memo_pdf_rich(tmp_path: Path) -> None:
    ctx = MemoContext(
        client_name="Cabinet Dentaire Rousseau-Bouchard inc.",
        period_description="Jan 1 – Dec 31, 2025",
        engagement_id="eng-test-rich",
    )
    out = tmp_path / "memo.pdf"
    returned = generate_memo_pdf(_rich_memo(), ctx, out)
    assert returned == out
    assert out.exists(), "PDF file was not written"
    data = out.read_bytes()
    assert len(data) > 2000, "PDF is suspiciously small"
    assert data.startswith(b"%PDF-"), "Output is not a valid PDF (missing magic)"


def test_generate_memo_pdf_minimal(tmp_path: Path) -> None:
    """Even with only the required short fields, the PDF must build."""
    ctx = MemoContext(
        client_name="Test Client Inc.",
        period_description="2025",
        engagement_id="eng-test-min",
    )
    out = tmp_path / "memo_minimal.pdf"
    generate_memo_pdf(_minimal_memo(), ctx, out)
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF-")


def test_generate_memo_pdf_sign_off_badge(tmp_path: Path) -> None:
    """Sign-off-ready vs not-ready should both render without error."""
    ctx = MemoContext(
        client_name="Test", period_description="2025",
        engagement_id="eng-badge",
    )
    ready = _minimal_memo()  # sign_off_ready=True
    not_ready = _rich_memo()  # sign_off_ready=False
    p1 = tmp_path / "ready.pdf"
    p2 = tmp_path / "notready.pdf"
    generate_memo_pdf(ready, ctx, p1)
    generate_memo_pdf(not_ready, ctx, p2)
    assert p1.exists() and p2.exists()
    # The not-ready memo should be materially larger (more content).
    assert p2.stat().st_size > p1.stat().st_size


def test_generate_memo_pdf_preserves_per_item_rows(tmp_path: Path) -> None:
    """Per-item rows in a finding should render to extracted PDF text so
    the bookkeeper can see the date+vendor+amount table we need them
    to preserve. reportlab compresses streams, so grepping raw bytes
    doesn't work — use pdfplumber to extract text like a real reader."""
    import pdfplumber

    ctx = MemoContext(
        client_name="Test", period_description="2025",
        engagement_id="eng-rows",
    )
    out = tmp_path / "rows.pdf"
    generate_memo_pdf(_rich_memo(), ctx, out)

    with pdfplumber.open(out) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Per-item row content must be recoverable from the PDF text stream.
    assert "JE-2025-003" in text, (
        f"per-item row content not in PDF text. Extracted:\n{text[:500]!r}"
    )
    assert "whitening" in text.lower() or "Whitening" in text, \
        "per-item row memo text not in PDF"
    # Sanity — the client name, a section header, and an AJE row should
    # all be reachable from the extracted text.
    assert "Executive Summary" in text
    assert "Blocking Issues" in text
    assert "Proposed Adjusting Journal Entries" in text
    assert "Cosmetic revenue" in text or "cosmetic revenue" in text
