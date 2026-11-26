"""Unit tests for src.queue — TurnQueue concurrency and depth enforcement."""
from __future__ import annotations

from src.queue import TurnQueue
from src.telegram import InboundMessage


def _msg(chat_id: int, text: str) -> InboundMessage:
    return InboundMessage(update_id=1, chat_id=chat_id, user_id=chat_id, text=text)


async def test_owner_bypass_marks_queue_active() -> None:
    q = TurnQueue(owner_chat_id=1, max_depth=3)
    assert await q.submit(_msg(1, "owner")) is None
    assert q.active is True
    status = await q.submit(_msg(2, "guest"))
    assert status is not None and "Queued" in status


async def test_duplicate_chat_replaces_pending_without_bypassing() -> None:
    q = TurnQueue(owner_chat_id=1, max_depth=3)
    assert await q.submit(_msg(2, "first")) is None
    assert await q.submit(_msg(3, "queued-1")) is not None
    status = await q.submit(_msg(3, "queued-2"))
    assert status is not None and "Updated queued message" in status
    assert len(q.pending) == 1
    assert q.pending[0][1].text == "queued-2"


async def test_queue_respects_configured_max_depth() -> None:
    q = TurnQueue(owner_chat_id=1, max_depth=2)
    assert await q.submit(_msg(10, "active")) is None
    assert await q.submit(_msg(11, "p1")) is not None
    assert await q.submit(_msg(12, "p2")) is not None
    full = await q.submit(_msg(13, "p3"))
    assert full is not None and "Queue full" in full
