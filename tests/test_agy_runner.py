"""Tests for src.agy_runner — subprocess spawn and argv construction."""
from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest

from src.agy_runner import AgyResult, run_agy


@pytest.fixture
def fake_agy(tmp_path: Path) -> Path:
    """Create an executable shell stub that echoes a plain-text reply."""
    script = tmp_path / "agy"
    script.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            cat > /dev/null
            echo 'fake reply'
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


async def test_run_agy_returns_stdout_text(fake_agy: Path, tmp_path: Path) -> None:
    result = await run_agy(
        prompt="hello",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(fake_agy),
    )
    assert isinstance(result, AgyResult)
    assert result.exit_code == 0
    assert result.text.strip() == "fake reply"


async def test_run_agy_surfaces_nonzero_exit(tmp_path: Path) -> None:
    script = tmp_path / "agy-fail"
    script.write_text("#!/bin/sh\necho boom 1>&2\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    result = await run_agy(
        prompt="x",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(script),
    )
    assert result.exit_code == 1
    assert "boom" in result.stderr
    assert result.text == ""


async def test_run_agy_builds_argv(tmp_path: Path) -> None:
    captured = tmp_path / "argv.txt"
    script = tmp_path / "agy-record"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            cat > /dev/null
            printf '%s\\n' "$@" > "{captured}"
            echo ok
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    workdir = tmp_path / "work"
    workdir.mkdir()
    await run_agy(
        prompt="hi",
        chat_dir=str(workdir),
        has_session=True,
        model="gemini-2.5-pro",
        mode="plan",
        agy_path=str(script),
    )
    args = captured.read_text().splitlines()
    assert "-p" in args and "hi" in args
    assert "--continue" in args
    assert "--model" in args and "gemini-2.5-pro" in args
    assert "--dangerously-skip-permissions" in args
    assert "--sandbox" in args
    assert "--print-timeout" in args


async def test_run_agy_uses_new_project_for_fresh_session(tmp_path: Path) -> None:
    captured = tmp_path / "argv.txt"
    script = tmp_path / "agy-record"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            cat > /dev/null
            printf '%s\\n' "$@" > "{captured}"
            echo ok
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    workdir = tmp_path / "work"
    workdir.mkdir()
    await run_agy(
        prompt="hi",
        chat_dir=str(workdir),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(script),
    )
    args = captured.read_text().splitlines()
    assert "--new-project" in args
    assert "--continue" not in args


async def test_run_agy_returns_timeout_result(tmp_path: Path) -> None:
    script = tmp_path / "agy-hang"
    script.write_text("#!/bin/sh\ncat > /dev/null\nsleep 5\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result = await run_agy(
        prompt="x",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(script),
        timeout=0.3,
    )
    assert result.exit_code == 124
    assert "timed out" in result.stderr
    assert result.text == ""


async def test_run_agy_no_timeout_by_default(fake_agy: Path, tmp_path: Path) -> None:
    result = await run_agy(
        prompt="x",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(fake_agy),
    )
    assert result.exit_code == 0
    assert result.text.strip() == "fake reply"


def test_build_args_plan_sandbox() -> None:
    from src.agy_runner import _build_args

    args = _build_args(
        agy_path="/usr/bin/agy",
        prompt="run-security-scan",
        has_session=False,
        model="gemini-2.5-pro",
        mode="plan",
        print_timeout="15m",
        chat_dir="/home/i/chat_workspace",
    )

    assert args[0] == "/usr/bin/agy"
    assert "--mode" in args and "plan" in args
    assert "--sandbox" in args
    assert "-p" in args and "run-security-scan" in args


def test_sync_system_prompt_rule_lifecycle(tmp_path: Path) -> None:
    from src.agy_runner import AgyWorkerPool, sync_system_prompt_rule

    chat_dir = tmp_path / "chat"
    rule_file = chat_dir / "GEMINI.md"

    # Empty prompt with no existing file does nothing
    assert sync_system_prompt_rule(str(chat_dir), "") is None
    assert not rule_file.exists()

    # Creates managed block
    out = sync_system_prompt_rule(str(chat_dir), "Always be concise.")
    assert out == rule_file
    text1 = rule_file.read_text(encoding="utf-8")
    assert "<!-- agy-to-im:system_prompt:start -->" in text1
    assert "Always be concise." in text1
    assert "<!-- agy-to-im:system_prompt:end -->" in text1

    # Preserves user-authored notes outside managed block when updated
    rule_file.write_text(text1 + "\n# My Notes\nKeep this.\n", encoding="utf-8")
    sync_system_prompt_rule(str(chat_dir), "Updated system rule.")
    text2 = rule_file.read_text(encoding="utf-8")
    assert "Updated system rule." in text2
    assert "Always be concise." not in text2
    assert "# My Notes\nKeep this." in text2

    # Clearing prompt removes only managed block, keeping user notes
    sync_system_prompt_rule(str(chat_dir), "")
    text3 = rule_file.read_text(encoding="utf-8")
    assert "<!-- agy-to-im:system_prompt:start -->" not in text3
    assert text3.strip() == "# My Notes\nKeep this."

    # If file only had managed block, clearing deletes the file
    chat_dir2 = tmp_path / "chat2"
    sync_system_prompt_rule(str(chat_dir2), "Temp rule")
    assert (chat_dir2 / "GEMINI.md").exists()
    sync_system_prompt_rule(str(chat_dir2), "")
    assert not (chat_dir2 / "GEMINI.md").exists()

    # Worker pool key changes when system_prompt changes
    k1 = AgyWorkerPool._make_key(
        chat_dir="/tmp/c", model="m", mode="code", agy_path="agy",
        auto_compact="", settings=None, system_prompt="A",
    )
    k2 = AgyWorkerPool._make_key(
        chat_dir="/tmp/c", model="m", mode="code", agy_path="agy",
        auto_compact="", settings=None, system_prompt="B",
    )
    assert k1 != k2
