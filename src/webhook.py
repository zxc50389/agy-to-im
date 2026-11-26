"""Webhook server — HTTP handler for Telegram webhook delivery."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.daemon import _TelegramLike

LOG = logging.getLogger("antigravity_telegram_bridge")

WEBHOOK_PATH = "/webhook"
MAX_WEBHOOK_BODY_BYTES = 1_048_576  # 1 MiB


def _verify_secret(body: bytes, header: str, token: str) -> bool:
    """Verify Telegram's X-Telegram-Bot-Api-Secret-Token header (constant-time)."""
    if not header or not token:
        return False
    return hmac.compare_digest(header, token)


async def _send_status(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    writer.close()


async def webhook_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    tg: "_TelegramLike", token: str,
) -> None:
    """Handle one incoming webhook request."""
    try:
        raw = b""
        content_length = 0
        body_start = 0
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
            if not chunk:
                break
            raw += chunk
            if b"\r\n\r\n" in raw:
                headers_end = raw.index(b"\r\n\r\n")
                body_start = headers_end + 4
                header_text = raw[:headers_end].decode("utf-8", errors="replace")
                for line in header_text.split("\r\n")[1:]:
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                        break
                if content_length < 0 or content_length > MAX_WEBHOOK_BODY_BYTES:
                    await _send_status(writer, "413 Payload Too Large")
                    return
                while len(raw) - body_start < content_length:
                    more = await asyncio.wait_for(reader.read(4096), timeout=5)
                    if not more:
                        break
                    raw += more
                    if len(raw) - body_start > MAX_WEBHOOK_BODY_BYTES:
                        await _send_status(writer, "413 Payload Too Large")
                        return
                break
            if len(raw) > 65_536:
                await _send_status(writer, "431 Request Header Fields Too Large")
                return

        if b"\r\n\r\n" not in raw:
            await _send_status(writer, "400 Bad Request")
            return

        headers_end = raw.index(b"\r\n\r\n")
        header_lines = raw[:headers_end].decode("utf-8", errors="replace").split("\r\n")
        req_parts = header_lines[0].split(" ")
        method = req_parts[0] if len(req_parts) >= 1 else ""
        path = req_parts[1].split("?", 1)[0] if len(req_parts) >= 2 else ""

        if path != WEBHOOK_PATH:
            await _send_status(writer, "404 Not Found")
            return
        if method != "POST":
            await _send_status(writer, "405 Method Not Allowed")
            return

        body = raw[headers_end + 4:]
        if content_length and len(body) > content_length:
            body = body[:content_length]
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            await _send_status(writer, "413 Payload Too Large")
            return

        secret_header = ""
        for line in header_lines[1:]:
            if line.lower().startswith("x-telegram-bot-api-secret-token:"):
                secret_header = line.split(":", 1)[1].strip()
                break

        if not _verify_secret(body, secret_header, token):
            await _send_status(writer, "403 Forbidden")
            return

        try:
            update = json.loads(body)
        except json.JSONDecodeError:
            await _send_status(writer, "400 Bad Request")
            return
        if not isinstance(update, dict):
            await _send_status(writer, "400 Bad Request")
            return

        _webhook_updates.append(update)
        await _send_status(writer, "200 OK")
    except Exception as e:
        LOG.debug("webhook handler error: %s", e)
        try:
            await _send_status(writer, "500 Internal Server Error")
        except Exception:
            pass


_webhook_updates: list[dict] = []


def drain_webhook_updates() -> list[dict]:
    """Return and clear pending webhook updates."""
    updates = list(_webhook_updates)
    _webhook_updates.clear()
    return updates


async def start_webhook_server(
    port: int, tg: "_TelegramLike", token: str,
) -> None:
    """Start a local HTTP server to receive Telegram webhook callbacks."""
    server = await asyncio.start_server(
        lambda r, w: webhook_handler(r, w, tg, token),
        host="0.0.0.0", port=port,
    )
    LOG.info("webhook server listening on port %d", port)
    async with server:
        await server.serve_forever()
