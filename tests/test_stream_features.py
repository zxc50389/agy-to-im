"""Tests for stream-json persistent worker, live streaming, dynamic models, auto-compact, and --settings."""
from __future__ import annotations

import json
import stat
import textwrap
from pathlib import Path
from typing import Any

import pytest

from src.agy_runner import AgyWorkerPool, StreamEvent, _build_args, _build_env
from src.commands import parse_agy_models_output
from src.config import ConfigError, load_config
from src.daemon import _TelegramStreamer


# ── #3 auto_compact & secret stripping ────────────────────────────


def test_build_env_sets_auto_compact_and_strips_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGY_TELEGRAM_BOT_TOKEN", "secret-tok")
    monkeypatch.setenv("CTI_WEBHOOK_SECRET", "wh-secret")
    env = _build_env(auto_compact="200K")
    assert env["AGY_CLI_AUTO_COMPACT_OVERRIDE"] == "200K"
    assert "AGY_TELEGRAM_BOT_TOKEN" not in env
    assert "CTI_WEBHOOK_SECRET" not in env


# ── #7 --settings & #2 stream-json args ───────────────────────────


def test_build_args_stream_json_and_settings() -> None:
    args = _build_args(
        agy_path="/usr/bin/agy",
        prompt="",
        has_session=True,
        model="gemini-pro-orchestrator-neon",
        mode="code",
        print_timeout="15m",
        settings={"orchestrator": {"planner": "gemini-4-neon-high", "executor": "gemini-4-neon-low"}},
        stream_json=True,
    )
    assert "--input-format" in args and "stream-json" in args
    assert "--output-format" in args
    assert "-p" not in args
    assert "--settings" in args
    s_idx = args.index("--settings")
    parsed = json.loads(args[s_idx + 1])
    assert parsed["orchestrator"]["planner"] == "gemini-4-neon-high"
    # Persistent workers pass 0s so idle processes don't self-timeout
    t_idx = args.index("--print-timeout")
    assert args[t_idx + 1] == "0s"


# ── #4 dynamic agy models parsing & filtering ─────────────────────


def test_parse_agy_models_output_filters_prefixes() -> None:
    raw = textwrap.dedent(
        """\
        gemini-4-neon-high             Gemini 4 Neon (High)
        gemini-4-neon-low              Gemini 4 Neon (Low)
        gemini-pro-orchestrator-neon   Orchestrator (Neon)
        gemini-3.8-flash-high          Gemini 3.8 Flash (High)
        claude-opus-5-5-high           Claude Opus 5.5 (High)
        gpt-oss-120b-medium            GPT-OSS 120B (Medium)
        """
    )
    filtered = parse_agy_models_output(raw, prefixes=["gemini-4-", "gemini-pro-orchestrator"])
    assert filtered == (
        "gemini-4-neon-high",
        "gemini-4-neon-low",
        "gemini-pro-orchestrator-neon",
    )


# ── config validation for new fields ──────────────────────────────


def test_load_config_parses_new_agy_fields(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "telegram": {"bot_token": "123:abc", "allowed_user_ids": [1]},
        "agy": {
            "model": "gemini-4-neon-high",
            "mode": "code",
            "auto_compact": "50%",
            "model_prefixes": ["gemini-4-"],
            "stream_mode": True,
            "settings": {"orchestrator": {"planner": "gemini-4-neon-high"}},
        },
    }))
    cfg = load_config(cfg_file)
    assert cfg.agy.auto_compact == "50%"
    assert cfg.agy.model_prefixes == ["gemini-4-"]
    assert cfg.agy.stream_mode is True
    assert cfg.agy.settings == {"orchestrator": {"planner": "gemini-4-neon-high"}}


def test_load_config_rejects_invalid_auto_compact(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "telegram": {"bot_token": "123:abc", "allowed_user_ids": [1]},
        "agy": {"auto_compact": "invalid!"},
    }))
    with pytest.raises(ConfigError, match="auto_compact"):
        load_config(cfg_file)


# ── #1 & #2 persistent stream-json worker multi-turn reuse ────────


@pytest.fixture
def fake_stream_agy(tmp_path: Path) -> Path:
    """Fake agy CLI speaking the exact NDJSON stream-json protocol across multiple turns."""
    script = tmp_path / "fake-agy-stream"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys

            pid = os.getpid()
            print(json.dumps({"event": "init", "conversation_id": f"conv-{pid}"}), flush=True)
            turn = 0
            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                msg = json.loads(raw)
                assert msg["event"] == "user"
                content = msg["message"]["content"]
                turn += 1
                print(json.dumps({
                    "event": "step_update",
                    "step_update": {"step_type": "tool", "tool_name": "view_file", "state": "ACTIVE"},
                }), flush=True)
                reply = f"pid={pid} turn={turn} echo={content}"
                print(json.dumps({
                    "event": "step_update",
                    "step_update": {"step_type": "agent_response", "text_delta": reply, "state": "DONE"},
                }), flush=True)
                print(json.dumps({
                    "event": "result",
                    "result": {"conversation_id": f"conv-{pid}", "status": "SUCCESS", "response": reply},
                }), flush=True)
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


async def test_worker_pool_reuses_process_and_recycles_on_reset(
    fake_stream_agy: Path, tmp_path: Path
) -> None:
    pool = AgyWorkerPool()
    events: list[StreamEvent] = []

    async def on_ev(ev: StreamEvent) -> None:
        events.append(ev)

    try:
        r1 = await pool.run_turn(
            "first",
            chat_dir=str(tmp_path),
            has_session=False,
            model="gemini-4-neon-high",
            mode="code",
            agy_path=str(fake_stream_agy),
            on_event=on_ev,
        )
        r2 = await pool.run_turn(
            "second",
            chat_dir=str(tmp_path),
            has_session=True,
            model="gemini-4-neon-high",
            mode="code",
            agy_path=str(fake_stream_agy),
            on_event=on_ev,
        )
        assert r1.exit_code == 0 and "turn=1 echo=first" in r1.text
        assert r2.exit_code == 0 and "turn=2 echo=second" in r2.text
        # Same PID proves persistent warm worker was reused without cold start!
        pid1 = r1.text.split()[0]
        pid2 = r2.text.split()[0]
        assert pid1 == pid2
        assert any(e.kind == "tool" and e.tool_name == "view_file" for e in events)
        assert any(e.kind == "text" for e in events)

        # Reset (has_session=False) spawns a fresh worker process
        r3 = await pool.run_turn(
            "after-reset",
            chat_dir=str(tmp_path),
            has_session=False,
            model="gemini-4-neon-high",
            mode="code",
            agy_path=str(fake_stream_agy),
        )
        assert r3.exit_code == 0 and "turn=1 echo=after-reset" in r3.text
        assert r3.text.split()[0] != pid1
    finally:
        await pool.close_all()


# ── #1 Telegram live streamer edits placeholder in place ──────────


class _FakeStreamTG:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []

    async def send_message(self, chat_id: int, text: str, *, keyboard: Any = None) -> int:
        self.sent.append((chat_id, text))
        return 777

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, *, keyboard: Any = None) -> None:
        self.edits.append((chat_id, message_id, text))


async def test_telegram_streamer_updates_and_finalizes() -> None:
    tg = _FakeStreamTG()
    streamer = _TelegramStreamer(tg, 42)  # type: ignore[arg-type]
    await streamer.start()
    assert tg.sent == [(42, "💭 Thinking…")]

    # Force immediate edit by resetting throttle timestamp
    streamer._last_edit = 0.0
    await streamer.on_event(StreamEvent(kind="tool", tool_name="view_file"))
    assert any("Reading file" in txt for _, _, txt in tg.edits)

    streamer._last_edit = 0.0
    await streamer.on_event(StreamEvent(kind="text", text="Hello"))
    assert any("Hello ▍" in txt for _, _, txt in tg.edits)

    await streamer.finalize("Final answer")
    assert tg.edits[-1] == (42, 777, "Final answer")
    assert len(tg.sent) == 1  # edited in place, no duplicate message
