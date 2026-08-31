"""Способ 1 — RSS. Основной канал сбора, с поддержкой ETag / Last-Modified."""
from __future__ import annotations

import feedparser

from .. import util
from ..config import Source, config
from .base import FetchResult, RawPost, http_client


def _entry_id(entry, url: str) -> str:
    for key in ("id", "guid", "link"):
        value = entry.get(key)
        if value:
            return str(value)[:400]
    return url[:400]


def _entry_text(entry) -> str:
    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            return util.clean_html(value)
    content = entry.get("content")
    if content:
        try:
            return util.clean_html(content[0].get("value"))
        except (AttributeError, IndexError, KeyError):
            pass
    return ""


def _entry_section(entry) -> str | None:
    tags = entry.get("tags")
    if tags:
        try:
            return str(tags[0].get("term"))[:120]
        except (AttributeError, IndexError, KeyError):
            return None
    return None


def fetch(src: Source, *, etag: str | None = None, last_modified: str | None = None,
          url_override: str | None = None) -> FetchResult:
    conf = config()["collect"]
    url = url_override or src.url
    if not url:
        return FetchResult(error="нет url")

    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    with http_client() as client:
        response = client.get(url, headers=headers)

    if response.status_code == 304:
        return FetchResult(not_modified=True, etag=etag, last_modified=last_modified)
    if response.status_code >= 400:
        return FetchResult(error=f"HTTP {response.status_code}")

    parsed = feedparser.parse(response.content)
    entries = parsed.entries[: conf["max_items_per_feed"]]
    if not entries and getattr(parsed, "bozo", 0) and not parsed.feed:
        return FetchResult(error="не распарсилось как фид")

    posts: list[RawPost] = []
    for entry in entries:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        published = util.parse_dt(
            entry.get("published_parsed") or entry.get("updated_parsed")
            or entry.get("published") or entry.get("updated")
        ) or util.now_utc()
        title = util.clean_html(entry.get("title"))
        text = _entry_text(entry)
        posts.append(
            RawPost(
                source_id=src.id,
                external_id=_entry_id(entry, link),
                url=link,
                title=title or None,
                text=text or title or "",
                published_at=published,
                section=_entry_section(entry),
                publisher=src.publisher,
                lang=src.lang,
            )
        )

    return FetchResult(
        posts,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )
