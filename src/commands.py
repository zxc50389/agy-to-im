"""Bridge command handlers — text slash commands and inline-keyboard callbacks."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.media import clean_inbox, list_inbox
from src.state import is_valid_model
from src.telegram import InlineKeyboard

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import CallbackQuery, InboundMessage

LOG = logging.getLogger("antigravity_telegram_bridge")
DEFAULT_MODEL = "gemini-4-neon-high"

MODEL_CHOICES: tuple[str, ...] = (
    "gemini-4-neon-xhigh",
    "gemini-4-neon-high",
    "gemini-4-neon-medium",
    "gemini-4-neon-low",
    "gemini-4-carbon-high",
    "gemini-4-carbon-medium",
    "gemini-4-carbon-low",
    "gemini-4-argon-high",
    "gemini-4-argon-medium",
    "gemini-4-argon-low",
    "gemini-4-hydrogen-xhigh",
    "gemini-4-hydrogen-high",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-low",
    "gemini-pro-orchestrator-neon",
    "gemini-pro-orchestrator",
    "gemini-pro-orchestrator-argon",
)
MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("code", "Code (auto)"),
    ("plan", "Plan (read-only sandbox)"),
)
BOT_COMMANDS: tuple[dict[str, str], ...] = (
    {"command": "model", "description": "Pick a model for this chat"},
    {"command": "usage", "description": "Show model quota usage"},
    {"command": "reset", "description": "Start a fresh agy session"},
)
_DEFAULT_TOKEN = "_DEFAULT"
_ACTIVE_MODEL_CHOICES: tuple[str, ...] = MODEL_CHOICES


def get_model_choices() -> tuple[str, ...]:
    """Return the active list of selectable models (dynamic if refreshed, else fallback)."""
    return _ACTIVE_MODEL_CHOICES


def parse_agy_models_output(raw: str, prefixes: list[str] | None = None) -> tuple[str, ...]:
    """Parse `agy models` stdout into deduplicated model slugs, optionally filtered by prefix."""
    found: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        slug = stripped.split()[0]
        if not is_valid_model(slug) or slug in seen:
            continue
        if prefixes and not any(slug.startswith(p) for p in prefixes):
            continue
        seen.add(slug)
        found.append(slug)
    return tuple(found)


def refresh_model_choices(
    agy_path: str = "agy",
    prefixes: list[str] | None = None,
) -> tuple[str, ...]:
    """Query `agy models` and update the active model list. Falls back on failure."""
    global _ACTIVE_MODEL_CHOICES
    resolved = shutil.which(agy_path) or agy_path
    try:
        res = subprocess.run(
            [resolved, "models"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if res.returncode == 0:
            parsed = parse_agy_models_output(res.stdout, prefixes=prefixes)
            if parsed:
                _ACTIVE_MODEL_CHOICES = parsed
                return _ACTIVE_MODEL_CHOICES
        else:
            LOG.warning("agy models exited %d: %s", res.returncode, res.stderr.strip())
    except Exception as err:
        LOG.warning("refresh_model_choices failed: %s", err)
    return _ACTIVE_MODEL_CHOICES

WELCOME_TEXT = (
    "👋 Welcome! I'm your Antigravity bridge bot.\n\n"
    "Send me any message and I'll forward it to the agy CLI. "
    "Each chat has its own session that persists across messages.\n\n"
    "Commands: /start · /help · /status · /settings · /model · /mode · /reset"
)

HELP_TEXT = (
    "📖 Antigravity Bridge Help\n\n"
    "Send any text to chat with agy.\n\n"
    "Commands:\n"
    "/status   — read-only system + chat summary\n"
    "/settings — control panel with buttons\n"
    "/model    — pick a model (per-chat override)\n"
    "/mode     — pick a mode (Code or Plan)\n"
    "/reset    — fresh agy session for this chat\n"
    "/info     — same as /status\n"
    "/image on|off — toggle photo processing\n"
    "/files    — list recent uploads\n"
    "/start, /help — these messages\n\n"
    "Per-chat overrides take precedence over config.json."
)


@dataclass(frozen=True)
class BridgeReply:
    """Daemon's structured reply: text, optional inline keyboard, optional toast."""

    text: str
    keyboard: InlineKeyboard | None = None
    toast: str = ""
    parse_mode: str | None = None


def _effective_model(cs: "ChatState", cfg: "Config") -> tuple[str, str]:
    if cs.model:
        return cs.model, "chat"
    if cfg.agy.model:
        return cfg.agy.model, "config"
    return DEFAULT_MODEL, "default"


def _effective_mode(cs: "ChatState", cfg: "Config") -> tuple[str, str]:
    if cs.mode:
        return cs.mode, "chat"
    return cfg.agy.mode, "config"


def render_status(cs: "ChatState", cfg: "Config") -> str:
    model, model_src = _effective_model(cs, cfg)
    mode, mode_src = _effective_mode(cs, cfg)
    session = "active (resumes next turn)" if cs.has_session else "fresh (next turn starts new)"
    home = os.path.expanduser("~")
    workdir = cs.chat_dir.replace(home, "~", 1)
    return (
        "🟢 Antigravity Bridge — Status\n"
        f"Model:      {model}  [{model_src}]\n"
        f"Mode:       {mode}  [{mode_src}]\n"
        "\n"
        "This chat:\n"
        f"  Session:  {session}\n"
        f"  Turns:    {cs.turn_count}\n"
        f"  Workdir:  {workdir}"
    )


def _settings_keyboard() -> InlineKeyboard:
    return [
        [
            {"text": "🤖 Change model", "callback_data": "nav:model"},
            {"text": "🛡 Change mode", "callback_data": "nav:mode"},
        ],
        [{"text": "🧹 Reset session", "callback_data": "R"}],
        [{"text": "🔄 Refresh", "callback_data": "nav:settings"}],
    ]


def _model_keyboard(current_per_chat: str) -> InlineKeyboard:
    rows: InlineKeyboard = []
    for m in get_model_choices():
        marker = "● " if m == current_per_chat else "○ "
        rows.append([{"text": marker + m, "callback_data": f"m:{m}"}])
    default_marker = "● " if not current_per_chat else "○ "
    rows.append([
        {"text": default_marker + "Use config default", "callback_data": f"m:{_DEFAULT_TOKEN}"}
    ])
    return rows


def _mode_keyboard(current_per_chat: str) -> InlineKeyboard:
    cells: list[dict[str, object]] = []
    for value, label in MODE_CHOICES:
        marker = "● " if value == current_per_chat else "○ "
        cells.append({"text": marker + label, "callback_data": f"M:{value}"})
    default_marker = "● " if not current_per_chat else "○ "
    return [
        cells,
        [{"text": default_marker + "Use config default", "callback_data": f"M:{_DEFAULT_TOKEN}"}],
        [{"text": "← Back to settings", "callback_data": "nav:settings"}],
    ]


def _render_settings(cs: "ChatState", cfg: "Config") -> BridgeReply:
    return BridgeReply(text=render_status(cs, cfg), keyboard=_settings_keyboard())


def _render_model_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur, src = _effective_model(cs, cfg)
    return BridgeReply(
        text=f"🤖 Choose a model for this chat\n\nCurrent: {cur}  [{src}]",
        keyboard=_model_keyboard(cs.model),
    )


def _render_mode_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur, src = _effective_mode(cs, cfg)
    return BridgeReply(
        text=f"🛡 Choose a mode for this chat\n\nCurrent: {cur}  [{src}]",
        keyboard=_mode_keyboard(cs.mode),
    )


_USAGE_GROUP_ALIASES: dict[str, str] = {
    "gemini models": "Gemini",
    "claude and gpt models": "Claude & GPT",
}
_USAGE_LABEL_ALIASES: dict[str, str] = {
    "weekly limit remaining": "Weekly",
    "five hour limit remaining": "5-Hour",
    "daily limit remaining": "Daily",
    "hourly limit remaining": "Hourly",
}
_USAGE_HEADER_HTML = "📊 <b>Model Quota</b>"


def _normalize_usage_group(group: str) -> str:
    return _USAGE_GROUP_ALIASES.get(group.strip().lower(), group.strip())


def _normalize_usage_label(label: str) -> str:
    cleaned = label.strip()
    alias = _USAGE_LABEL_ALIASES.get(cleaned.lower())
    if alias:
        return alias
    return re.sub(r"\s+limit\s+remaining$", "", cleaned, flags=re.IGNORECASE)


def _usage_bar(pct_raw: str, *, width: int = 10) -> tuple[str, str, str]:
    """Return (status_dot, bar, padded_pct) for a percentage string like '93%'."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*%?\s*$", pct_raw)
    if not m:
        return "⚪", "░" * width, pct_raw.strip()
    pct = max(0.0, min(100.0, float(m.group(1))))
    pct_int = round(pct)
    if pct_int >= 50:
        dot = "🟢"
    elif pct_int >= 20:
        dot = "🟡"
    else:
        dot = "🔴"
    if pct_int >= 100:
        filled = width
    elif pct_int <= 0:
        filled = 0
    else:
        filled = max(1, min(width - 1, round(pct_int / 100 * width)))
    bar = "█" * filled + "░" * (width - filled)
    return dot, bar, f"{pct_int:>3}%"


def _format_reset_countdown(reset_raw: str, *, now: datetime) -> str:
    if not reset_raw:
        return ""
    try:
        dt = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
    except ValueError:
        return reset_raw
    if dt.year <= 1970:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta_s = int((dt - now).total_seconds())
    if delta_s <= 0:
        return "now"
    if delta_s < 3600:
        return f"{max(1, delta_s // 60)}m"
    if delta_s < 86400:
        h, m = divmod(delta_s // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(delta_s // 3600, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _format_usage_output(raw: str, *, now: datetime | None = None) -> str:
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return "(no usage data)"
    ref_now = now if now is not None else datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)

    groups: dict[str, list[tuple[str, str, str]]] = {}
    extra_lines: list[str] = []
    parsed_any = False

    for line in lines:
        cols = line.split("\t")
        if len(cols) == 4:
            parsed_any = True
            group_raw, label_raw, pct_raw, reset_raw = (c.strip() for c in cols)
            group = _normalize_usage_group(group_raw)
            label = _normalize_usage_label(label_raw)
            groups.setdefault(group, []).append((label, pct_raw, reset_raw))
        else:
            extra_lines.append(line)

    if not parsed_any:
        return raw

    label_width = min(
        max((len(lbl) for rows in groups.values() for lbl, _, _ in rows), default=6),
        10,
    )
    sections: list[str] = [_USAGE_HEADER_HTML]
    for group, rows in groups.items():
        block = [f"<b>{html.escape(group)}</b>"]
        for label, pct_raw, reset_raw in rows:
            dot, bar, pct_disp = _usage_bar(pct_raw)
            countdown = _format_reset_countdown(reset_raw, now=ref_now)
            padded_label = f"{label:<{label_width}}"
            core = html.escape(f"{padded_label} {bar} {pct_disp}")
            row_str = f"{dot} <code>{core}</code>"
            if countdown:
                row_str += f" · <code>{html.escape(countdown)}</code>"
            block.append(row_str)
        sections.append("\n".join(block))

    if extra_lines:
        sections.append("\n".join(html.escape(line) for line in extra_lines))

    updated = ref_now.astimezone().strftime("%H:%M:%S")
    sections.append(f"<i>Updated {updated}</i>")
    return "\n\n".join(sections)


async def _fetch_usage() -> str:
    agy_bin = os.environ.get("AGY_BIN") or "agy"
    resolved = shutil.which(agy_bin) or agy_bin
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved, "-p", "/usage", "--print-timeout", "30s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35.0)
    except asyncio.TimeoutError:
        return "⚠️ /usage timed out."
    except Exception as err:
        return f"⚠️ Failed to fetch usage: {err}"
    out = stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        err_txt = stderr.decode("utf-8", errors="replace").strip()
        return f"⚠️ /usage failed (exit {proc.returncode}){': ' + err_txt if err_txt else ''}"
    return _format_usage_output(out)


async def render_usage_reply() -> BridgeReply:
    """Fetch quota usage and return a formatted BridgeReply."""
    text = await _fetch_usage()
    parse_mode = "HTML" if text.startswith(_USAGE_HEADER_HTML) else None
    return BridgeReply(
        text=text,
        parse_mode=parse_mode,
    )


async def handle_text_command(
    msg: "InboundMessage",
    cs: "ChatState",
    cfg: "Config",
) -> BridgeReply | None:
    """Return a reply for a slash command, else None (forward to agy)."""
    stripped = msg.text.strip()
    if not stripped:
        return None
    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower().split("@", 1)[0]
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/usage":
        return await render_usage_reply()

    if cmd == "/start":
        return BridgeReply(WELCOME_TEXT)
    if cmd == "/help":
        return BridgeReply(HELP_TEXT)
    if cmd in ("/status", "/info"):
        return BridgeReply(render_status(cs, cfg))
    if cmd == "/settings":
        return _render_settings(cs, cfg)
    if cmd == "/model":
        if args:
            if is_valid_model(args):
                cs.model = args
                return BridgeReply(f"🤖 Model set to {args}")
            return BridgeReply(f"⚠️ Unsupported model: {args}")
        return _render_model_picker(cs, cfg)
    if cmd == "/mode":
        if args:
            if args in {"code", "plan"}:
                cs.mode = args
                return BridgeReply(f"🛡 Mode set to {args}")
            return BridgeReply(f"⚠️ Unsupported mode: {args}")
        return _render_mode_picker(cs, cfg)
    if cmd == "/reset":
        cs.has_session = False
        return BridgeReply("🧹 Session reset. The next message starts fresh.")
    if cmd == "/thinking":
        mode = args.strip().lower()
        if mode in ("on", "true", "1"):
            return BridgeReply("💭 Thinking visibility is not available in agy print mode.")
        if mode in ("off", "false", "0"):
            return BridgeReply("💭 agy print mode already suppresses thinking streams.")
        return BridgeReply("💭 agy print mode does not expose thinking streams.")
    if cmd == "/compact":
        return BridgeReply("🗜️ Context compaction is not supported by agy print mode.")
    if cmd == "/image":
        mode = args.strip().lower()
        if mode in ("on", "true", "1"):
            cs.photo_enabled = True  # type: ignore[attr-defined]
            return BridgeReply("📸 Photo processing: ON")
        if mode in ("off", "false", "0"):
            cs.photo_enabled = False  # type: ignore[attr-defined]
            return BridgeReply("📸 Photo processing: OFF")
        return BridgeReply("📸 Photo processing toggle not available in this build.")
    if cmd == "/files":
        if args == "clean":
            removed = clean_inbox(cs.chat_dir, max_age_hours=0)
            return BridgeReply(f"🧹 Inbox cleaned: {removed} files removed")
        files = list_inbox(cs.chat_dir, limit=20)
        if not files:
            return BridgeReply("📭 Inbox is empty")
        return BridgeReply("📎 Inbox:\n" + "\n".join(f"• {f}" for f in files))
    if cmd == "/queue":
        return BridgeReply("📋 Queue status is available via daemon internals.")
    return None


def handle_callback(
    cq: "CallbackQuery",
    cs: "ChatState",
    cfg: "Config",
) -> BridgeReply:
    """Handle inline-keyboard button taps. Always returns a reply to render."""
    data = cq.data

    if data == "nav:status":
        return BridgeReply(render_status(cs, cfg))
    if data == "nav:settings":
        return _render_settings(cs, cfg)
    if data == "nav:model":
        return _render_model_picker(cs, cfg)
    if data == "nav:mode":
        return _render_mode_picker(cs, cfg)
    if data == "R":
        cs.has_session = False
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Session reset")

    if data.startswith("m:"):
        choice = data[2:]
        if choice == _DEFAULT_TOKEN:
            cs.model = ""
            toast = "Using config default"
        elif choice in get_model_choices():
            cs.model = choice
            toast = f"Model: {choice}"
        else:
            toast = "Unknown choice"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    if data.startswith("M:"):
        choice = data[2:]
        valid_modes = {v for v, _ in MODE_CHOICES}
        if choice == _DEFAULT_TOKEN:
            cs.mode = ""
            toast = "Using config default"
        elif choice in valid_modes:
            cs.mode = choice
            toast = f"Mode: {choice}"
        else:
            toast = "Unknown choice"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    return _render_settings(cs, cfg)
