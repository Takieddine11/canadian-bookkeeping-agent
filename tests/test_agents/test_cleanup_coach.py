"""Unit tests for the Cleanup Coach state machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.cleanup_coach import STEPS, handle_command, opening_message, render_step
from src.store.engagement_db import (
    CONV_PERSONAL,
    MODE_AUDIT,
    MODE_CLEANUP,
    PHASE_DELIVERED,
    EngagementStore,
)


@pytest.fixture
def store(tmp_path: Path) -> EngagementStore:
    return EngagementStore(root=tmp_path / "engagements")


@pytest.fixture
def cleanup_engagement(store: EngagementStore):
    return store.create_engagement(
        conversation_id="conv-cleanup",
        conversation_type=CONV_PERSONAL,
        period_description="Q3 2026",
        mode=MODE_CLEANUP,
    )


def test_engagement_starts_in_cleanup_mode_at_step_zero(cleanup_engagement) -> None:
    assert cleanup_engagement.mode == MODE_CLEANUP
    assert cleanup_engagement.cleanup_step_index == 0
    assert cleanup_engagement.phase.lower().startswith("cleanup")


def test_opening_message_mentions_period_and_controls() -> None:
    msg = opening_message("Q3 2026")
    assert "Q3 2026" in msg
    assert "next" in msg.lower()
    assert "back" in msg.lower()
    assert "status" in msg.lower()


def test_render_step_includes_title_and_body() -> None:
    step = STEPS[0]
    rendered = render_step(step)
    assert step.title in rendered
    assert "Legal entity" in rendered or "Business Number" in rendered


def test_next_advances_one_step_and_persists(
    store: EngagementStore, cleanup_engagement
) -> None:
    resp = handle_command(store, cleanup_engagement, "next")
    assert resp.step_index == 1
    assert STEPS[1].title in resp.text
    # Persisted:
    refreshed = store.get_active_engagement(cleanup_engagement.conversation_id)
    assert refreshed.cleanup_step_index == 1


def test_next_synonyms_all_advance(store: EngagementStore, cleanup_engagement) -> None:
    for command in ("done", "continue", "ok"):
        # Re-read engagement each time to get the latest step index.
        eng = store.get_active_engagement(cleanup_engagement.conversation_id)
        before = eng.cleanup_step_index
        handle_command(store, eng, command)
        after = store.get_active_engagement(cleanup_engagement.conversation_id)
        assert after.cleanup_step_index == before + 1


def test_back_goes_previous(store: EngagementStore, cleanup_engagement) -> None:
    handle_command(store, cleanup_engagement, "next")   # -> 1
    eng = store.get_active_engagement(cleanup_engagement.conversation_id)
    handle_command(store, eng, "next")                  # -> 2
    eng = store.get_active_engagement(cleanup_engagement.conversation_id)
    resp = handle_command(store, eng, "back")           # -> 1
    assert resp.step_index == 1


def test_back_on_step_zero_is_noop(store: EngagementStore, cleanup_engagement) -> None:
    resp = handle_command(store, cleanup_engagement, "back")
    assert resp.step_index == 0
    refreshed = store.get_active_engagement(cleanup_engagement.conversation_id)
    assert refreshed.cleanup_step_index == 0


def test_repeat_renders_current_step_without_advancing(
    store: EngagementStore, cleanup_engagement
) -> None:
    handle_command(store, cleanup_engagement, "next")  # -> 1
    eng = store.get_active_engagement(cleanup_engagement.conversation_id)
    resp = handle_command(store, eng, "repeat")
    assert resp.step_index == 1
    assert STEPS[1].title in resp.text


def test_status_reports_progress(store: EngagementStore, cleanup_engagement) -> None:
    handle_command(store, cleanup_engagement, "next")
    handle_command(
        store,
        store.get_active_engagement(cleanup_engagement.conversation_id),
        "next",
    )
    eng = store.get_active_engagement(cleanup_engagement.conversation_id)
    resp = handle_command(store, eng, "status")
    assert f"{eng.cleanup_step_index + 1} of {len(STEPS)}" in resp.text


def test_skip_on_nonskippable_step_refuses(
    store: EngagementStore, cleanup_engagement
) -> None:
    # Step 1 (index 0 — Client context) is marked non_skippable.
    assert STEPS[0].non_skippable is True
    resp = handle_command(store, cleanup_engagement, "skip")
    assert resp.step_index == 0
    assert "non-skippable" in resp.text.lower()


def test_skip_on_skippable_step_advances(store: EngagementStore, cleanup_engagement) -> None:
    # Advance through non-skippable steps to the first skippable one.
    idx = 0
    while STEPS[idx].non_skippable:
        handle_command(
            store,
            store.get_active_engagement(cleanup_engagement.conversation_id),
            "next",
        )
        idx += 1
    eng = store.get_active_engagement(cleanup_engagement.conversation_id)
    resp = handle_command(store, eng, "skip")
    assert resp.step_index == idx + 1
    assert "skipped" in resp.text.lower()


def test_unrecognized_command_stays_on_step(store: EngagementStore, cleanup_engagement) -> None:
    resp = handle_command(store, cleanup_engagement, "tell me a joke")
    assert resp.step_index == 0
    assert resp.unrecognized is True


def test_final_step_done_transitions_to_audit_mode(
    store: EngagementStore, cleanup_engagement
) -> None:
    # Fast-forward through all steps.
    for _ in range(len(STEPS) - 1):
        eng = store.get_active_engagement(cleanup_engagement.conversation_id)
        handle_command(store, eng, "next")

    # We're now on the last step. "done" should complete cleanup.
    eng = store.get_active_engagement(cleanup_engagement.conversation_id)
    assert eng is not None
    assert eng.cleanup_step_index == len(STEPS) - 1
    resp = handle_command(store, eng, "done")
    assert resp.cleanup_complete is True
    assert "new audit" in resp.text.lower()

    # The cleanup engagement is closed (phase=delivered) so the conversation is free
    # for `new audit <period>` to start fresh.
    active = store.get_active_engagement(cleanup_engagement.conversation_id)
    assert active is None


def test_cleanup_engagement_and_audit_engagement_are_separate(
    store: EngagementStore, cleanup_engagement
) -> None:
    """After cleanup completes, a new engagement with mode=audit can start in the
    same conversation without conflict."""
    # Complete cleanup.
    for _ in range(len(STEPS)):
        eng = store.get_active_engagement(cleanup_engagement.conversation_id)
        if eng is None:
            break
        handle_command(store, eng, "next")
    assert store.get_active_engagement(cleanup_engagement.conversation_id) is None

    # Start audit engagement.
    audit_eng = store.create_engagement(
        conversation_id=cleanup_engagement.conversation_id,
        conversation_type=CONV_PERSONAL,
        period_description="Q3 2026",
        mode=MODE_AUDIT,
    )
    assert audit_eng.mode == MODE_AUDIT
    assert audit_eng.engagement_id != cleanup_engagement.engagement_id
