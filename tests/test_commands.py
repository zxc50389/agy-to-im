"""Unit tests for src.commands — bridge command handlers."""
from __future__ import annotations

from src.commands import handle_callback, handle_text_command
from src.config import AgyConfig, Config, TelegramConfig
from src.state import ChatState
from src.telegram import CallbackQuery


def _cfg(model: str = "", mode: str = "code") -> Config:
    return Config(
        telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]),
        agy=AgyConfig(default_workdir="/tmp", model=model, mode=mode),
    )


def _state() -> ChatState:
    return ChatState(chat_dir="/tmp/chat")


class _FakeMsg:
    def __init__(self, text: str, chat_id: int = 42, user_id: int = 42) -> None:
        self.text = text
        self.chat_id = chat_id
        self.user_id = user_id


async def test_start_command() -> None:
    reply = await handle_text_command(_FakeMsg("/start"), _state(), _cfg())
    assert reply is not None
    assert "Welcome" in reply.text


async def test_help_command() -> None:
    reply = await handle_text_command(_FakeMsg("/help"), _state(), _cfg())
    assert reply is not None
    assert "Commands" in reply.text


async def test_info_shows_default_model() -> None:
    reply = await handle_text_command(_FakeMsg("/info"), _state(), _cfg(model="gemini-2.5-pro"))
    assert reply is not None
    assert "gemini-2.5-pro" in reply.text


async def test_info_no_session() -> None:
    reply = await handle_text_command(_FakeMsg("/info"), _state(), _cfg())
    assert reply is not None
    assert "fresh" in reply.text.lower()


async def test_thinking_not_available() -> None:
    reply = await handle_text_command(_FakeMsg("/thinking on"), _state(), _cfg())
    assert reply is not None
    assert "not available" in reply.text.lower()


async def test_model_picker() -> None:
    reply = await handle_text_command(_FakeMsg("/model"), _state(), _cfg(model="gemini-2.5-pro"))
    assert reply is not None
    assert reply.keyboard is not None


async def test_model_set() -> None:
    cs = _state()
    reply = await handle_text_command(_FakeMsg("/model gemini-2.5-pro"), cs, _cfg())
    assert reply is not None
    assert cs.model == "gemini-2.5-pro"


async def test_reset_clears_session() -> None:
    cs = _state()
    cs.has_session = True
    reply = await handle_text_command(_FakeMsg("/reset"), cs, _cfg())
    assert reply is not None
    assert cs.has_session is False


async def test_unknown_returns_none() -> None:
    reply = await handle_text_command(_FakeMsg("hello world"), _state(), _cfg())
    assert reply is None


async def test_callback_nav_settings() -> None:
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="nav:settings")
    reply = handle_callback(cq, _state(), _cfg())
    assert reply.keyboard is not None


async def test_callback_model_choice() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="m:gemini-4-neon-high")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.model == "gemini-4-neon-high"
    assert "gemini-4-neon-high" in reply.toast


def test_bot_commands_menu() -> None:
    from datetime import datetime, timezone
    from src.commands import BOT_COMMANDS, DEFAULT_MODEL, MODEL_CHOICES, _format_usage_output

    assert DEFAULT_MODEL == "gemini-4-neon-high"
    assert DEFAULT_MODEL in MODEL_CHOICES
    assert [c["command"] for c in BOT_COMMANDS] == ["model", "usage", "reset"]
    now = datetime(2026, 11, 27, 6, 20, 0, tzinfo=timezone.utc)
    out = _format_usage_output(
        "Gemini Models\tWeekly Limit Remaining\t87%\t2026-12-01T01:22:03Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t35%\t2026-11-27T06:51:00Z\n"
        "Claude and GPT models\tWeekly Limit Remaining\t12%\t2026-11-27T10:35:00Z\n"
        "Claude and GPT models\tFive Hour Limit Remaining\t100%\t1970-01-01T00:00:00Z",
        now=now,
    )
    assert out.startswith("📊 <b>Model Quota</b>")
    assert "<b>Gemini</b>" in out
    assert "<b>Claude &amp; GPT</b>" in out
    assert "🟢 <code>Weekly █████████░  87%</code> · <code>3d 19h</code>" in out
    assert "🟡 <code>5-Hour ████░░░░░░  35%</code> · <code>31m</code>" in out
    assert "🔴 <code>Weekly █░░░░░░░░░  12%</code> · <code>4h 15m</code>" in out
    assert "🟢 <code>5-Hour ██████████ 100%</code>" in out
    assert "1970" not in out
    assert "<i>Updated " in out


def test_format_usage_output_edge_cases() -> None:
    from src.commands import _format_usage_output

    assert _format_usage_output("") == "(no usage data)"
    assert _format_usage_output("raw unstructured text") == "raw unstructured text"


async def test_usage_command_returns_html_without_keyboard(monkeypatch) -> None:
    from src import commands as cmd_mod

    async def _fake_fetch() -> str:
        return cmd_mod._format_usage_output(
            "Gemini Models\tWeekly Limit Remaining\t93%\t2026-12-03T07:13:10Z"
        )

    monkeypatch.setattr(cmd_mod, "_fetch_usage", _fake_fetch)
    reply = await handle_text_command(_FakeMsg("/usage"), _state(), _cfg())
    assert reply is not None
    assert reply.parse_mode == "HTML"
    assert reply.keyboard is None
    assert "<b>Gemini</b>" in reply.text


async def test_usage_command_error_uses_plain_text(monkeypatch) -> None:
    from src import commands as cmd_mod

    async def _fake_fetch() -> str:
        return "⚠️ /usage timed out."

    monkeypatch.setattr(cmd_mod, "_fetch_usage", _fake_fetch)
    reply = await handle_text_command(_FakeMsg("/usage"), _state(), _cfg())
    assert reply is not None
    assert reply.parse_mode is None
    assert reply.keyboard is None


async def test_callback_reset() -> None:
    cs = _state()
    cs.has_session = True
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="R")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.has_session is False
    assert "reset" in reply.toast.lower()


async def test_callback_mode_choice() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="M:plan")
    reply = handle_callback(cq, cs, _cfg())
    assert cs.mode == "plan"
    assert "plan" in reply.toast.lower()
