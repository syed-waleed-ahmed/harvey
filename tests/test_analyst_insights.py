"""Analyst insights say what's actually holding the pipeline up.

Queued prospects with drafts waiting in the Outbox are waiting on a human,
not "blocked", and the advice never points a Gmail/SMTP user at Instantly.
"""

import asyncio

from harvey.agents.analyst import Analyst
from harvey.state import StateManager


def insights(pipeline, outbox=None):
    report = {"pipeline": pipeline, "intents": {}, "campaigns": [], "stages": {}}
    if outbox is not None:
        report["outbox"] = outbox
    return Analyst.__new__(Analyst)._generate_insights(report)


def test_drafts_awaiting_approval_are_waiting_on_you():
    out = insights({"queued": 4}, {"pending_review": 8})
    assert any(i.startswith("WAITING ON YOU: 8 email(s)") for i in out)
    assert not any("BLOCKED" in i or "Instantly" in i for i in out)


def test_approved_and_scheduled_is_not_blocked():
    out = insights({"queued": 4}, {"approved": 3})
    assert not any("BLOCKED" in i or "WAITING ON YOU" in i for i in out)


def test_queued_with_nothing_in_the_outbox_is_blocked_without_naming_a_provider():
    for outbox in ({}, None):  # a report written before the outbox counts existed
        out = insights({"queued": 4}, outbox)
        blocked = [i for i in out if i.startswith("BLOCKED")]
        assert blocked and "Instantly" not in blocked[0]


def test_outbox_counts_by_status(tmp_path):
    async def go():
        state = StateManager(db_path=str(tmp_path / "harvey.db"))
        await state.init_db()
        for i, status in enumerate(["pending_review", "pending_review", "approved"]):
            await state.add_outbox_item(
                prospect_id=f"p{i}", to_email=f"p{i}@example.com", subject="s",
                body="b", send_at="2026-01-01T00:00:00", status=status)
        return await state.count_outbox_by_status()

    assert asyncio.run(go()) == {"pending_review": 2, "approved": 1}
