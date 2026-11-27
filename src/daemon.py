"""Long-running daemon: poll Telegram, route through agy, reply."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from src.agy_runner import WORKER_POOL, AgyResult, StreamEvent
from src.commands import (
    BOT_COMMANDS,
    handle_callback,
    handle_text_command,
    refresh_model_choices,
)
from src.config import Config, load_config
from src.health import configure_memory_limit, memory_healthy, record_error, record_turn, start_server
from src.media import build_media_prompt, clean_inbox
from src.okf_memory import attach_memory
from src.queue import TurnQueue
from src.state import ChatState, State, load_state, save_state
from src.telegram import CallbackQuery, InboundMessage, TelegramClient, chunk_message, is_authorized
from src.turn import execute_agy
from src.webhook import drain_webhook_updates, start_webhook_server

LOG = logging.getLogger("antigravity_telegram_bridge")
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
DEFAULT_STATE_PATH = Path.home() / ".antigravity" / "bridge" / "state.json"
DEFAULT_CHATS_ROOT = Path.home() / ".antigravity" / "bridge" / "chats"
DEFAULT_AGY_BIN = "agy"
LONG_POLL_TIMEOUT = 30
AGY_TIMEOUT_S = 900.0
_QUEUE = TurnQueue()


class _TelegramLike(Protocol):
    async def __aenter__(self) -> "_TelegramLike": ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def get_me(self) -> dict[str, Any]: ...
    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]: ...
    async def send_message(
        self, chat_id: int, text: str, *, keyboard: Any | None = None, parse_mode: str | None = None
    ) -> int | None: ...
    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, keyboard: Any | None = None, parse_mode: str | None = None
    ) -> None: ...
    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None: ...
    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None: ...


RunAgyFunc = Callable[..., Awaitable[AgyResult]]


@dataclass(frozen=True)
class DaemonInfo:
    bot_username: str
    started_at: float
    agy_version: str


def _resolve_agy_path() -> str:
    return os.environ.get("AGY_BIN") or DEFAULT_AGY_BIN


def _resolve_chats_root(cfg: Config) -> Path:
    if cfg.agy.chats_root:
        return Path(cfg.agy.chats_root).expanduser()
    return DEFAULT_CHATS_ROOT


def _detect_agy_version(agy_path: str) -> str:
    try:
        out = subprocess.run(
            [agy_path, "--version"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception as err:
        LOG.warning("agy --version failed: %s", err)
    return "unknown"


def _ensure_chat_state(state: State, chat_id: int, chats_root: Path) -> ChatState:
    cs = state.chats.get(chat_id)
    if cs is not None:
        Path(cs.chat_dir).mkdir(parents=True, exist_ok=True)
        return cs
    chat_dir = chats_root / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    cs = ChatState(chat_dir=str(chat_dir))
    state.chats[chat_id] = cs
    return cs


def _format_timeout_reply() -> str:
    minutes = AGY_TIMEOUT_S / 60
    return (
        f"⏱️ agy turn cut off at {minutes:.0f} min.\n\n"
        "• Send a shorter follow-up\n"
        "• Split into smaller steps\n"
        "• /reset to start fresh"
    )


_TOOL_LABELS: dict[str, str] = {
    "view_file": "Reading file",
    "read_file": "Reading file",
    "run_command": "Running command",
    "grep_search": "Searching files",
    "find_by_name": "Finding files",
    "list_dir": "Listing directory",
    "search_web": "Searching web",
    "read_url_content": "Reading webpage",
    "write_to_file": "Writing file",
    "write_file": "Writing file",
    "replace_file_content": "Editing file",
    "edit_file": "Editing file",
    "invoke_subagent": "Running subagent",
}
_STREAM_EDIT_INTERVAL_S = 1.2
_STREAM_PREVIEW_LIMIT = 3900


class _TelegramStreamer:
    """Streams live tool/text progress into one Telegram message via throttled edits."""

    def __init__(self, tg: _TelegramLike, chat_id: int) -> None:
        self._tg = tg
        self._chat_id = chat_id
        self._msg_id: int | None = None
        self._last_edit: float = 0.0
        self._last_rendered: str = ""
        self._has_text: bool = False

    async def start(self) -> None:
        try:
            self._msg_id = await self._tg.send_message(self._chat_id, "💭 Thinking…")
            self._last_rendered = "💭 Thinking…"
            self._last_edit = time.monotonic()
        except Exception:
            self._msg_id = None

    async def _maybe_edit(self, text: str, *, force: bool = False) -> None:
        if self._msg_id is None or text == self._last_rendered:
            return
        now = time.monotonic()
        if not force and (now - self._last_edit) < _STREAM_EDIT_INTERVAL_S:
            return
        try:
            await self._tg.edit_message_text(self._chat_id, self._msg_id, text)
            self._last_rendered = text
            self._last_edit = now
        except Exception:
            pass

    async def on_event(self, ev: StreamEvent) -> None:
        if ev.kind == "tool" and not self._has_text:
            label = _TOOL_LABELS.get(ev.tool_name, f"Running {ev.tool_name}")
            await self._maybe_edit(f"🔧 {label}…")
        elif ev.kind == "text" and ev.text:
            self._has_text = True
            preview = ev.text
            if len(preview) > _STREAM_PREVIEW_LIMIT:
                preview = preview[:_STREAM_PREVIEW_LIMIT] + "\n…"
            await self._maybe_edit(preview + " ▍")

    async def finalize(self, reply: str) -> None:
        chunks = chunk_message(reply)
        if not chunks:
            return
        if self._msg_id is not None:
            try:
                await self._tg.edit_message_text(self._chat_id, self._msg_id, chunks[0])
                for extra in chunks[1:]:
                    await self._tg.send_message(self._chat_id, extra)
                return
            except Exception as err:
                LOG.warning("stream finalize edit failed (sending fresh): %s", err)
        await self._tg.send_message(self._chat_id, reply)


async def _do_turn(
    msg: InboundMessage, tg: _TelegramLike, cs: ChatState,
    cfg: Config, agy_path: str,
    *, state: State | None = None, info: DaemonInfo | None = None,
) -> None:
    prompt = await build_media_prompt(msg, tg, cs, cfg)
    if prompt is None:
        return
    if is_dataclass(msg) and hasattr(msg, "text"):
        msg = replace(msg, text=prompt)
    try:
        await tg.send_chat_action(msg.chat_id, "typing")
    except Exception:
        pass

    streamer: _TelegramStreamer | None = None
    if cfg.agy.stream_mode:
        streamer = _TelegramStreamer(tg, msg.chat_id)
        await streamer.start()
        text, code = await execute_agy(
            tg, msg.chat_id, msg, cs, cfg, agy_path, on_event=streamer.on_event,
        )
    else:
        text, code = await execute_agy(tg, msg.chat_id, msg, cs, cfg, agy_path)

    if code == 124:
        record_error()
        reply = _format_timeout_reply()
    elif code != 0:
        record_error()
        err_detail = f"\n\n{text}" if text else ""
        reply = f"⚠️ agy error (exit {code}){err_detail}"
    else:
        record_turn()
        if not cs.has_session:
            cs.has_session = True
        cs.turn_count += 1
        reply = text or "(empty reply)"
    # Post-turn maintenance: update OKF memory layer, clean inbox
    try:
        attach_memory(
            cs.chat_dir,
            state,
            bridge_version="0.2.0",
            bot_username=info.bot_username if info else "",
            memory_limit_bytes=cfg.safety.memory.limit_bytes,
        )
        clean_inbox(cs.chat_dir, max_age_hours=cfg.safety.inbox.max_age_hours)
    except Exception:
        pass
    try:
        if streamer is not None:
            await streamer.finalize(reply)
        else:
            await tg.send_message(msg.chat_id, reply)
    except Exception as err:
        LOG.error("sendMessage failed chat=%s: %s", msg.chat_id, err)


async def _process_text(
    msg: InboundMessage, tg: _TelegramLike, state: State,
    state_path: Path, chats_root: Path, cfg: Config,
    agy_path: str, info: DaemonInfo,
) -> None:
    if not is_authorized(msg, cfg.telegram):
        LOG.info("drop unauth user=%s", msg.user_id)
        return

    cs = _ensure_chat_state(state, msg.chat_id, chats_root)
    save_state(state_path, state)

    prev_state = (cs.has_session, cs.model, cs.mode)
    first_tok = msg.text.strip().split(maxsplit=1)[0].lower().split("@", 1)[0] if msg.text.strip() else ""
    if first_tok == "/usage":
        try:
            await tg.send_chat_action(msg.chat_id, "typing")
        except Exception:
            pass
    reply = await handle_text_command(msg, cs, cfg)
    if reply is not None:
        if (cs.has_session, cs.model, cs.mode) != prev_state:
            await WORKER_POOL.invalidate(cs.chat_dir)
        save_state(state_path, state)
        try:
            if reply.parse_mode:
                await tg.send_message(
                    msg.chat_id, reply.text, keyboard=reply.keyboard, parse_mode=reply.parse_mode
                )
            else:
                await tg.send_message(msg.chat_id, reply.text, keyboard=reply.keyboard)
        except Exception as err:
            LOG.error("sendMessage failed chat=%s: %s", msg.chat_id, err)
        return

    status = await _QUEUE.submit(msg)
    if status is not None:
        await tg.send_message(msg.chat_id, status)
        return

    await _do_turn(msg, tg, cs, cfg, agy_path, state=state, info=info)
    _QUEUE.complete()

    while True:
        next_item = _QUEUE.next()
        if next_item is None:
            break
        nxt_msg, nxt_fut = next_item
        nxt_cs = _ensure_chat_state(state, nxt_msg.chat_id, chats_root)
        await _do_turn(nxt_msg, tg, nxt_cs, cfg, agy_path, state=state, info=info)
        _QUEUE.complete()
        nxt_fut.set_result(None)

    save_state(state_path, state)


async def _process_callback(
    cq: CallbackQuery, tg: _TelegramLike, state: State,
    state_path: Path, chats_root: Path, cfg: Config, info: DaemonInfo,
) -> None:
    if not is_authorized(
        InboundMessage(update_id=cq.update_id, chat_id=cq.chat_id,
                       user_id=cq.user_id, text=""),
        cfg.telegram,
    ):
        LOG.info("drop unauth callback user=%s", cq.user_id)
        await tg.answer_callback_query(cq.callback_query_id, text="Not authorized")
        return

    cs = _ensure_chat_state(state, cq.chat_id, chats_root)
    prev_state = (cs.has_session, cs.model, cs.mode)
    reply = handle_callback(cq, cs, cfg)
    if (cs.has_session, cs.model, cs.mode) != prev_state:
        await WORKER_POOL.invalidate(cs.chat_dir)
    save_state(state_path, state)

    try:
        await tg.answer_callback_query(cq.callback_query_id, text=reply.toast)
    except Exception:
        pass
    try:
        if reply.parse_mode:
            await tg.edit_message_text(
                cq.chat_id, cq.message_id, reply.text,
                keyboard=reply.keyboard, parse_mode=reply.parse_mode,
            )
        else:
            await tg.edit_message_text(
                cq.chat_id, cq.message_id, reply.text, keyboard=reply.keyboard
            )
    except Exception as err:
        LOG.warning("editMessageText failed (sending fresh): %s", err)
        try:
            if reply.parse_mode:
                await tg.send_message(
                    cq.chat_id, reply.text,
                    keyboard=reply.keyboard, parse_mode=reply.parse_mode,
                )
            else:
                await tg.send_message(cq.chat_id, reply.text, keyboard=reply.keyboard)
        except Exception as err2:
            LOG.error("fallback sendMessage failed: %s", err2)


async def _fetch_updates(tg: _TelegramLike, offset: int) -> list[dict[str, Any]]:
    try:
        return await asyncio.wait_for(
            tg.get_updates(offset=offset, timeout=LONG_POLL_TIMEOUT),
            timeout=LONG_POLL_TIMEOUT + 5,
        )
    except asyncio.TimeoutError:
        return []
    except Exception as err:
        LOG.warning("getUpdates failed: %s", err)
        await asyncio.sleep(2)
        return []


async def run(
    *, cfg: Config, state_path: Path, chats_root: Path,
    tg: _TelegramLike, agy_path: str, info: DaemonInfo,
    stop_event: asyncio.Event,
    webhook_url: str = "", webhook_secret: str = "",
) -> None:
    _QUEUE.owner_chat_id = cfg.telegram.allowed_user_ids[0] if cfg.telegram.allowed_user_ids else 0
    _QUEUE.max_depth = cfg.safety.queue.max_depth
    _QUEUE.max_per_user = cfg.safety.queue.max_per_user
    _QUEUE.cooldown_seconds = cfg.safety.queue.cooldown_seconds
    configure_memory_limit(cfg.safety.memory.limit_bytes)
    mem_check_interval = max(1, cfg.safety.memory.check_interval_loops)
    chats_root.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path, chats_root)
    tick = 0
    async with tg:
        if webhook_url and hasattr(tg, "set_webhook"):
            wh_res = await tg.set_webhook(webhook_url, webhook_secret)  # type: ignore[attr-defined]
            LOG.info("webhook setup: %s", wh_res.get("description") if wh_res.get("ok") else wh_res)
        try:
            while not stop_event.is_set():
                tick += 1
                # Health gate: self-terminate if over limit so systemd
                # Restart=on-failure brings up a clean replacement.
                if tick % mem_check_interval == 0 and not memory_healthy(cfg.safety.memory.limit_bytes):
                    LOG.critical("memory limit exceeded — triggering self-restart")
                    stop_event.set()
                    break

                if webhook_url:
                    updates = drain_webhook_updates()
                else:
                    updates = await _fetch_updates(tg, state.last_update_id + 1)
                    updates.extend(drain_webhook_updates())
                for upd in updates:
                    state.last_update_id = max(state.last_update_id, int(upd.get("update_id", 0)))

                    msg = parse_update_for_daemon(upd)
                    if msg is not None:
                        if not is_authorized(msg, cfg.telegram):
                            LOG.info("drop unauth user=%s", msg.user_id)
                            continue
                        allowed, wait = _QUEUE.check_ratelimit(msg.user_id)
                        if not allowed:
                            await tg.send_message(msg.chat_id, f"⏳ Rate limit. Wait {wait}s.")
                            continue
                        await _process_text(
                            msg, tg, state, state_path, chats_root,
                            cfg, agy_path, info,
                        )
                        continue

                    cq = parse_callback_for_daemon(upd)
                    if cq is not None:
                        await _process_callback(
                            cq, tg, state, state_path, chats_root, cfg, info,
                        )
                        continue

                if not updates:
                    await asyncio.sleep(0.1 if webhook_url else 0)
                save_state(state_path, state)
        finally:
            await WORKER_POOL.close_all()
            if webhook_url and hasattr(tg, "delete_webhook"):
                try:
                    await tg.delete_webhook()  # type: ignore[attr-defined]
                except Exception:
                    pass


# Aliases to avoid name collisions in the loop above
from src.telegram import parse_callback_query as parse_callback_for_daemon
from src.telegram import parse_update as parse_update_for_daemon


async def _detect_bot_username(cfg: Config) -> str:
    try:
        async with TelegramClient(cfg.telegram.bot_token) as tg:
            me = await tg.get_me()
            uname = me.get("username")
            return f"@{uname}" if isinstance(uname, str) and uname else "?"
    except Exception as err:
        LOG.warning("getMe failed: %s", err)
    return "?"


async def _register_bot_commands(cfg: Config) -> None:
    cmds = list(BOT_COMMANDS)
    try:
        async with TelegramClient(cfg.telegram.bot_token) as tg:
            await tg.set_my_commands(cmds)
            await tg.set_my_commands(cmds, scope={"type": "all_private_chats"})
    except Exception as err:
        LOG.warning("setMyCommands failed: %s", err)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    cfg_path = Path(os.environ.get("AGY_BRIDGE_CONFIG") or DEFAULT_CONFIG_PATH)
    state_path = Path(os.environ.get("AGY_BRIDGE_STATE") or DEFAULT_STATE_PATH)
    cfg = load_config(cfg_path)
    chats_root = _resolve_chats_root(cfg)
    agy_path = _resolve_agy_path()
    stop = asyncio.Event()

    async def _go() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        health_srv = await start_server()

        wh_task: asyncio.Task | None = None
        wh_secret = ""
        wh_url = os.environ.get("CTI_WEBHOOK_URL", "")
        wh_port = int(os.environ.get("CTI_WEBHOOK_PORT") or "8080")

        tg = TelegramClient(cfg.telegram.bot_token)
        try:
            bot_username = await _detect_bot_username(cfg)
            await _register_bot_commands(cfg)
            models = refresh_model_choices(agy_path, cfg.agy.model_prefixes)
            info = DaemonInfo(
                bot_username=bot_username,
                started_at=time.time(),
                agy_version=_detect_agy_version(agy_path),
            )
            LOG.info(
                "bridge starting bot=%s agy=%s chats_root=%s mode=%s model=%s stream=%s auto_compact=%s models=%d",
                info.bot_username, info.agy_version, chats_root,
                cfg.agy.mode, cfg.agy.model or "(default)",
                cfg.agy.stream_mode, cfg.agy.auto_compact or "(default)",
                len(models),
            )

            if wh_url:
                wh_secret = (
                    os.environ.get("CTI_WEBHOOK_SECRET")
                    or hashlib.sha256(cfg.telegram.bot_token.encode()).hexdigest()[:32]
                )
                wh_task = asyncio.create_task(start_webhook_server(wh_port, tg, wh_secret))

            await run(
                cfg=cfg, state_path=state_path, chats_root=chats_root,
                tg=tg, agy_path=agy_path, info=info, stop_event=stop,
                webhook_url=wh_url, webhook_secret=wh_secret,
            )
        finally:
            if wh_task is not None:
                wh_task.cancel()
                try:
                    await wh_task
                except Exception:
                    pass
            health_srv.close()
            await health_srv.wait_closed()

    asyncio.run(_go())


if __name__ == "__main__":
    main()
