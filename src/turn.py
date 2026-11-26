"""Turn execution — agy print-mode invocation."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import TYPE_CHECKING

from src.agy_runner import WORKER_POOL, StreamCallback, run_agy
from src.health import record_latency

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import InboundMessage

LOG = logging.getLogger("antigravity_telegram_bridge")
AGY_TIMEOUT_S = 900.0
_BG_TASKS: set[asyncio.Task[None]] = set()


async def _compile_memory_layer(chat_dir: str, graphify_bin: str) -> None:
    """Run graphify extract and export in the background to build the OKF memory vault."""
    try:
        # Run graphify extract . (AST + semantic codebase index)
        proc1 = await asyncio.create_subprocess_exec(
            graphify_bin, "extract", ".",
            cwd=chat_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc1.wait()

        # Run graphify export okf --dir concepts (Google OKF v0.1 bundle)
        proc2 = await asyncio.create_subprocess_exec(
            graphify_bin, "export", "okf", "--dir", "concepts",
            cwd=chat_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc2.wait()
        LOG.info("OKF-Graphify memory layer successfully updated for %s", chat_dir)
    except Exception as exc:
        LOG.warning("Failed to update OKF-Graphify memory layer: %s", exc)

async def execute_agy(
    tg: "_TelegramLike", chat_id: int, msg: "InboundMessage",
    cs: "ChatState", cfg: "Config", agy_path: str,
    *, on_event: StreamCallback | None = None,
) -> tuple[str, int]:
    prompt = (msg.text or "").strip()
    if not prompt:
        return "⚠️ Empty prompt. Please provide text or a supported attachment.", 0
    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat(tg, chat_id, hb_stop))
    turn_start = time.perf_counter()
    try:
        if cfg.agy.stream_mode:
            result = await WORKER_POOL.run_turn(
                prompt=prompt,
                chat_dir=cs.chat_dir,
                has_session=cs.has_session,
                model=cs.model or cfg.agy.model,
                mode=cs.mode or cfg.agy.mode,
                agy_path=agy_path,
                timeout=AGY_TIMEOUT_S,
                auto_compact=cfg.agy.auto_compact,
                settings=cfg.agy.settings,
                on_event=on_event,
            )
        else:
            result = await run_agy(
                prompt=prompt,
                chat_dir=cs.chat_dir,
                has_session=cs.has_session,
                model=cs.model or cfg.agy.model,
                mode=cs.mode or cfg.agy.mode,
                agy_path=agy_path,
                timeout=AGY_TIMEOUT_S,
                auto_compact=cfg.agy.auto_compact,
                settings=cfg.agy.settings,
            )
        if result.exit_code == 0:
            graphify_bin = shutil.which("graphify")
            if graphify_bin:
                task = asyncio.create_task(_compile_memory_layer(cs.chat_dir, graphify_bin))
                _BG_TASKS.add(task)
                task.add_done_callback(_BG_TASKS.discard)
    finally:
        hb_stop.set()
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass
    elapsed = int((time.perf_counter() - turn_start) * 1000)
    record_latency(elapsed)
    if result.exit_code != 0:
        LOG.error(
            "turn failed chat=%d cwd=%s exit=%d ms=%d stderr=%s",
            chat_id, cs.chat_dir, result.exit_code, elapsed, result.stderr.strip(),
        )
    else:
        LOG.info(
            "turn chat=%d cwd=%s exit=%d ms=%d reply_len=%d",
            chat_id, cs.chat_dir, result.exit_code, elapsed, len(result.text or ""),
        )
    return result.text or "", result.exit_code


async def _heartbeat(
    tg: "_TelegramLike", chat_id: int, stop_event: asyncio.Event
) -> None:
    """Typing indicator refresh every 4 s."""
    try:
        while not stop_event.is_set():
            try:
                await tg.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        return
