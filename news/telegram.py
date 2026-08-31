"""Тонкий клиент Telegram Bot API. Никаких сторонних библиотек — только HTTP."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config as cfg

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


def call(method: str, payload: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
    token = cfg.require_env("TG_BOT_TOKEN")
    url = API.format(token=token, method=method)
    try:
        response = httpx.post(url, json=payload or {}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise TelegramError(f"сеть: {exc}") from exc
    data = response.json()
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description')}")
    return data.get("result")


def send_message(chat_id: str | int, text: str, *, keyboard: list | None = None,
                 silent: bool = False, preview: bool = False) -> dict | None:
    if cfg.dry_run():
        log.info("[DRY_RUN] -> %s\n%s\n", chat_id, text)
        return None
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_notification": silent,
        "link_preview_options": {"is_disabled": not preview},
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return call("sendMessage", payload)


def edit_markup(chat_id: str | int, message_id: int, keyboard: list | None) -> None:
    if cfg.dry_run():
        return
    try:
        call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": keyboard or []},
        })
    except TelegramError as exc:
        log.warning("не удалось обновить клавиатуру: %s", exc)


def answer_callback(callback_id: str, text: str = "") -> None:
    if cfg.dry_run():
        return
    try:
        call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
    except TelegramError as exc:
        log.warning("answerCallbackQuery: %s", exc)


def get_updates(offset: int | None = None, timeout: int = 0) -> list[dict]:
    payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset:
        payload["offset"] = offset
    return call("getUpdates", payload, timeout=timeout + 25) or []


def notify_owner(text: str, *, silent: bool = False) -> None:
    owner = cfg.owner_chat_id()
    if not owner:
        log.info("TG_OWNER_ID не задан, сообщение не отправлено:\n%s", text)
        return
    try:
        send_message(owner, text, silent=silent)
    except TelegramError as exc:
        log.warning("сообщение владельцу не ушло: %s", exc)
