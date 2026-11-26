"""Spawn the Antigravity CLI (`agy`) in print or persistent stream-json mode.

Supports two execution modes:
  1. Legacy one-shot print mode (`run_agy`):
       agy -p "<prompt>" [--model <id>] [--continue | --new-project]
           [--dangerously-skip-permissions] [--mode accept-edits | --mode plan --sandbox]
           [--settings <json>] [--print-timeout <duration>]
  2. Persistent NDJSON stream-json mode (`AgyStreamWorker` / `AgyWorkerPool`):
       agy --input-format stream-json --output-format stream-json [...]
     Keeps one warmed-up process per chat, eliminates cold-start overhead on
     follow-up turns, and emits live `StreamEvent` updates for Telegram.
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

LOG = logging.getLogger("antigravity_telegram_bridge")

# Safety cap on stdout capture — prevents unbounded memory growth from
# runaway agy output.
_STDOUT_CAP_BYTES = 524_288  # 512 KiB
_STDERR_CAP_BYTES = 16_384   # 16 KiB
_STREAM_READER_LIMIT = 4 * 1024 * 1024  # 4 MiB per NDJSON line

# Bridge-only secrets that must never leak into agy child processes.
_STRIPPED_ENV_KEYS = frozenset({
    "AGY_TELEGRAM_BOT_TOKEN",
    "CTI_WEBHOOK_SECRET",
    "CREDENTIALS_DIRECTORY",
})


# Reap any defunct child processes left by prior subprocess invocations
# (bwrap sandbox can leave orphaned grand-children).
def _reap_zombies() -> None:
    try:
        while True:
            pid, _status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except (ChildProcessError, OSError):
        pass


@dataclass(frozen=True)
class AgyResult:
    text: str
    exit_code: int
    stderr: str


@dataclass(frozen=True)
class StreamEvent:
    """Normalized event emitted by a streaming `agy` turn."""

    kind: str  # "init" | "tool" | "text" | "result"
    text: str = ""
    delta: str = ""
    tool_name: str = ""
    state: str = ""
    conversation_id: str = ""
    usage: dict[str, Any] | None = None


StreamCallback = Callable[[StreamEvent], Awaitable[None]]


def _build_env(*, auto_compact: str = "") -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV_KEYS}
    if auto_compact:
        env["AGY_CLI_AUTO_COMPACT_OVERRIDE"] = auto_compact
    return env


def _build_args(
    *,
    agy_path: str,
    prompt: str,
    has_session: bool,
    model: str,
    mode: str,
    print_timeout: str,
    chat_dir: str = "",
    settings: dict[str, Any] | None = None,
    stream_json: bool = False,
) -> list[str]:
    resolved_agy = shutil.which(agy_path) or agy_path
    if stream_json:
        args = [
            resolved_agy,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
        ]
    else:
        args = [resolved_agy, "-p", prompt]

    if has_session:
        args.append("--continue")
    else:
        args.append("--new-project")

    if model:
        args.extend(["--model", model])

    args.append("--dangerously-skip-permissions")

    if mode == "plan":
        args.extend(["--mode", "plan", "--sandbox"])
    elif mode == "code":
        args.extend(["--mode", "accept-edits"])

    if settings:
        args.extend(["--settings", json.dumps(settings, separators=(",", ":"))])

    # Persistent workers enforce per-turn timeouts in Python so idle workers stay alive.
    args.extend(["--print-timeout", "0s" if stream_json else print_timeout])
    return args


def _run_sync(
    args: list[str],
    cwd: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> AgyResult:
    """Synchronous worker for subprocess invocation. Called via asyncio.to_thread."""
    _reap_zombies()
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            env=env if env is not None else _build_env(),
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial_stderr = ""
        if exc.stderr is not None:
            partial_stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, (bytes, bytearray))
                else str(exc.stderr)
            )
        message = f"agy timed out after {timeout}s"
        stderr = f"{partial_stderr}\n{message}".strip() if partial_stderr else message
        gc.collect()
        return AgyResult(text="", exit_code=124, stderr=stderr)

    stdout = completed.stdout
    truncated = False
    if len(stdout) > _STDOUT_CAP_BYTES:
        stdout = stdout[:_STDOUT_CAP_BYTES]
        truncated = True

    text = stdout.decode("utf-8", errors="replace")
    if truncated:
        text += "\n\n[output truncated at 512KiB]"

    stderr_out = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
    gc.collect()
    return AgyResult(
        text=text,
        exit_code=completed.returncode,
        stderr=stderr_out,
    )


async def run_agy(
    prompt: str,
    *,
    chat_dir: str,
    has_session: bool,
    model: str,
    mode: str,
    agy_path: str,
    timeout: float | None = None,
    print_timeout: str = "15m",
    auto_compact: str = "",
    settings: dict[str, Any] | None = None,
) -> AgyResult:
    """Run agy in one-shot print mode. Returns when the turn ends."""
    args = _build_args(
        agy_path=agy_path,
        prompt=prompt,
        has_session=has_session,
        model=model,
        mode=mode,
        print_timeout=print_timeout,
        chat_dir=chat_dir,
        settings=settings,
        stream_json=False,
    )
    env = _build_env(auto_compact=auto_compact)
    return await asyncio.to_thread(_run_sync, args, chat_dir, timeout, env)


def _cap_text(text: str) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= _STDOUT_CAP_BYTES:
        return text
    return raw[:_STDOUT_CAP_BYTES].decode("utf-8", errors="ignore") + "\n\n[output truncated at 512KiB]"


class AgyStreamWorker:
    """A persistent `agy --input-format stream-json --output-format stream-json` process."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        key: tuple[str, ...],
    ) -> None:
        self.proc = proc
        self.key = key
        self.conversation_id: str = ""
        self._stderr_chunks: list[bytes] = []
        self._stderr_len = 0
        self._stderr_task: asyncio.Task[None] = asyncio.create_task(self._drain_stderr())

    @classmethod
    async def start(
        cls,
        *,
        chat_dir: str,
        has_session: bool,
        model: str,
        mode: str,
        agy_path: str,
        auto_compact: str = "",
        settings: dict[str, Any] | None = None,
        key: tuple[str, ...] = (),
    ) -> "AgyStreamWorker":
        _reap_zombies()
        args = _build_args(
            agy_path=agy_path,
            prompt="",
            has_session=has_session,
            model=model,
            mode=mode,
            print_timeout="0s",
            chat_dir=chat_dir,
            settings=settings,
            stream_json=True,
        )
        env = _build_env(auto_compact=auto_compact)
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=chat_dir,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_READER_LIMIT,
        )
        return cls(proc, key)

    async def _drain_stderr(self) -> None:
        if self.proc.stderr is None:
            return
        try:
            while True:
                chunk = await self.proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_chunks.append(chunk)
                self._stderr_len += len(chunk)
                while self._stderr_len > _STDERR_CAP_BYTES and len(self._stderr_chunks) > 1:
                    dropped = self._stderr_chunks.pop(0)
                    self._stderr_len -= len(dropped)
        except (asyncio.CancelledError, Exception):
            pass

    def _consume_stderr(self) -> str:
        data = b"".join(self._stderr_chunks)
        self._stderr_chunks.clear()
        self._stderr_len = 0
        return data.decode("utf-8", errors="replace").strip()

    @property
    def is_alive(self) -> bool:
        return (
            self.proc.returncode is None
            and self.proc.stdin is not None
            and not self.proc.stdin.is_closing()
            and self.proc.stdout is not None
        )

    async def _read_turn(self, on_event: StreamCallback | None) -> AgyResult:
        assert self.proc.stdout is not None
        text_parts: list[str] = []
        text_bytes = 0

        while True:
            raw_line = await self.proc.stdout.readline()
            if not raw_line:
                await self.proc.wait()
                rc = self.proc.returncode if self.proc.returncode is not None else 1
                stderr_text = self._consume_stderr()
                partial = "".join(text_parts)
                return AgyResult(
                    text=_cap_text(partial) if partial else "",
                    exit_code=rc if rc != 0 else 1,
                    stderr=stderr_text or "agy stream ended unexpectedly",
                )

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            ev_type = payload.get("event")
            if ev_type == "init":
                cid = str(payload.get("conversation_id") or "")
                if cid:
                    self.conversation_id = cid
                if on_event is not None:
                    await on_event(StreamEvent(kind="init", conversation_id=self.conversation_id))
                continue

            if ev_type == "step_update":
                su = payload.get("step_update") or {}
                if not isinstance(su, dict):
                    continue
                step_type = str(su.get("step_type") or "")
                state = str(su.get("state") or "")
                if step_type == "tool":
                    tool_name = str(su.get("tool_name") or "")
                    if not tool_name:
                        ti = su.get("tool_info")
                        if isinstance(ti, dict):
                            tool_name = str(ti.get("name") or "")
                    if tool_name and on_event is not None:
                        await on_event(StreamEvent(kind="tool", tool_name=tool_name, state=state))
                elif step_type == "agent_response":
                    delta = su.get("text_delta")
                    if isinstance(delta, str) and delta:
                        delta_len = len(delta.encode("utf-8", errors="replace"))
                        if text_bytes < _STDOUT_CAP_BYTES:
                            text_parts.append(delta)
                            text_bytes += delta_len
                        if on_event is not None:
                            await on_event(
                                StreamEvent(
                                    kind="text",
                                    text="".join(text_parts),
                                    delta=delta,
                                    state=state,
                                )
                            )
                continue

            if ev_type == "result":
                res = payload.get("result") or {}
                if not isinstance(res, dict):
                    res = {}
                cid = str(res.get("conversation_id") or self.conversation_id)
                if cid:
                    self.conversation_id = cid
                status = str(res.get("status") or "").upper()
                response_text = str(res.get("response") or "")
                if not response_text and text_parts:
                    response_text = "".join(text_parts)
                err_text = str(res.get("error") or "")
                stderr_text = self._consume_stderr()
                if not err_text and status != "SUCCESS":
                    err_text = stderr_text or f"agy status={status or 'UNKNOWN'}"
                usage = res.get("usage") if isinstance(res.get("usage"), dict) else None
                if on_event is not None:
                    await on_event(
                        StreamEvent(
                            kind="result",
                            text=_cap_text(response_text),
                            conversation_id=self.conversation_id,
                            usage=usage,
                        )
                    )
                gc.collect()
                return AgyResult(
                    text=_cap_text(response_text),
                    exit_code=0 if status == "SUCCESS" else 1,
                    stderr=err_text,
                )

    async def send_turn(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        on_event: StreamCallback | None = None,
    ) -> AgyResult:
        if not self.is_alive or self.proc.stdin is None:
            raise BrokenPipeError("agy stream worker is not running")
        self._consume_stderr()
        msg_line = json.dumps(
            {"event": "user", "message": {"content": prompt}},
            ensure_ascii=False,
        ) + "\n"
        self.proc.stdin.write(msg_line.encode("utf-8"))
        await self.proc.stdin.drain()

        try:
            if timeout is not None:
                return await asyncio.wait_for(self._read_turn(on_event), timeout=timeout)
            return await self._read_turn(on_event)
        except asyncio.TimeoutError:
            stderr_partial = self._consume_stderr()
            await self.close()
            message = f"agy timed out after {timeout}s"
            stderr = f"{stderr_partial}\n{message}".strip() if stderr_partial else message
            return AgyResult(text="", exit_code=124, stderr=stderr)

    async def close(self) -> None:
        try:
            if self.proc.stdin is not None and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
        except Exception:
            pass
        if self.proc.returncode is None:
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    self.proc.terminate()
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        self.proc.kill()
                        await self.proc.wait()
                    except ProcessLookupError:
                        pass
        self._stderr_task.cancel()
        try:
            await self._stderr_task
        except (asyncio.CancelledError, Exception):
            pass


class AgyWorkerPool:
    """Manages one persistent `AgyStreamWorker` per chat directory."""

    def __init__(self) -> None:
        self._workers: dict[str, AgyStreamWorker] = {}

    @staticmethod
    def _make_key(
        *,
        chat_dir: str,
        model: str,
        mode: str,
        agy_path: str,
        auto_compact: str,
        settings: dict[str, Any] | None,
    ) -> tuple[str, ...]:
        settings_json = json.dumps(settings, sort_keys=True, separators=(",", ":")) if settings else ""
        return (chat_dir, model, mode, agy_path, auto_compact, settings_json)

    async def invalidate(self, chat_dir: str) -> None:
        worker = self._workers.pop(chat_dir, None)
        if worker is not None:
            await worker.close()

    async def close_all(self) -> None:
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            await worker.close()

    async def run_turn(
        self,
        prompt: str,
        *,
        chat_dir: str,
        has_session: bool,
        model: str,
        mode: str,
        agy_path: str,
        timeout: float | None = None,
        auto_compact: str = "",
        settings: dict[str, Any] | None = None,
        on_event: StreamCallback | None = None,
    ) -> AgyResult:
        key = self._make_key(
            chat_dir=chat_dir,
            model=model,
            mode=mode,
            agy_path=agy_path,
            auto_compact=auto_compact,
            settings=settings,
        )
        worker = self._workers.get(chat_dir)
        reused = False

        if worker is not None and (not has_session or worker.key != key or not worker.is_alive):
            await self.invalidate(chat_dir)
            worker = None

        if worker is None:
            worker = await AgyStreamWorker.start(
                chat_dir=chat_dir,
                has_session=has_session,
                model=model,
                mode=mode,
                agy_path=agy_path,
                auto_compact=auto_compact,
                settings=settings,
                key=key,
            )
            self._workers[chat_dir] = worker
        else:
            reused = True

        try:
            result = await worker.send_turn(prompt, timeout=timeout, on_event=on_event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            await self.invalidate(chat_dir)
            if not reused:
                raise
            # Stale persistent worker died while idle; respawn once and continue session.
            worker = await AgyStreamWorker.start(
                chat_dir=chat_dir,
                has_session=True,
                model=model,
                mode=mode,
                agy_path=agy_path,
                auto_compact=auto_compact,
                settings=settings,
                key=key,
            )
            self._workers[chat_dir] = worker
            result = await worker.send_turn(prompt, timeout=timeout, on_event=on_event)

        if not worker.is_alive:
            self._workers.pop(chat_dir, None)
        return result


WORKER_POOL = AgyWorkerPool()
