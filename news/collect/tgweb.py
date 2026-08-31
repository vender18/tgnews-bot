"""Способ 3 — публичная веб-версия телеграм-канала (t.me/s/<channel>).

Работает без авторизации и без второго аккаунта — только для публичных каналов.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from .. import util
from ..config import Source
from .base import FetchResult, RawPost, http_client


def fetch(src: Source, **_: object) -> FetchResult:
    handle = (src.handle or "").lstrip("@")
    if not handle:
        return FetchResult(error="нет handle")

    with http_client() as client:
        response = client.get(f"https://t.me/s/{handle}")
    if response.status_code >= 400:
        return FetchResult(error=f"HTTP {response.status_code}")

    soup = BeautifulSoup(response.text, "html.parser")
    blocks = soup.select("div.tgme_widget_message")
    if not blocks:
        return FetchResult(error="канал закрыт или разметка изменилась")

    posts: list[RawPost] = []
    for block in blocks:
        data_post = block.get("data-post")
        if not data_post:
            continue
        body = block.select_one("div.tgme_widget_message_text")
        raw_text = body.get_text("\n", strip=True) if body else ""
        text = util.strip_promo(util.clean_html(raw_text))
        if len(text) < 40:
            continue
        time_tag = block.select_one("time[datetime]")
        published = util.parse_dt(time_tag.get("datetime") if time_tag else None) or util.now_utc()
        title = util.shorten(text.split("\n")[0], 160)
        posts.append(
            RawPost(
                source_id=src.id,
                external_id=str(data_post),
                url=f"https://t.me/{data_post}",
                title=title,
                text=text,
                published_at=published,
                publisher=src.publisher,
                lang=src.lang,
            )
        )
    return FetchResult(posts)
