"""Способ 2 — Google News RSS. Закрывает всё, у чего нет своего фида."""
from __future__ import annotations

from urllib.parse import quote_plus

import feedparser

from .. import util
from ..config import Source, config
from .base import FetchResult, RawPost, http_client

LOCALE = {
    "ru": ("ru", "RU", "RU:ru"),
    "en": ("en", "US", "US:en"),
    "fr": ("fr", "FR", "FR:fr"),
}


def feed_url(query: str, lang: str = "ru") -> str:
    hl, gl, ceid = LOCALE.get(lang, LOCALE["ru"])
    return (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl={hl}&gl={gl}&ceid={ceid}"
    )


def split_publisher(title: str) -> tuple[str, str | None]:
    """В заголовке Google News издание идёт после последнего ' - '."""
    if " - " not in title:
        return title.strip(), None
    head, _, tail = title.rpartition(" - ")
    publisher = tail.strip()
    if head.strip() and 1 < len(publisher) <= 60:
        return head.strip(), publisher
    return title.strip(), None


def fetch(src: Source, *, query: str | None = None, **_: object) -> FetchResult:
    conf = config()["collect"]
    search = query or src.query
    if not search:
        return FetchResult(error="нет query")

    with http_client() as client:
        response = client.get(feed_url(search, src.lang or "ru"))
    if response.status_code >= 400:
        return FetchResult(error=f"HTTP {response.status_code}")

    parsed = feedparser.parse(response.content)
    posts: list[RawPost] = []
    for entry in parsed.entries[: conf["max_items_per_feed"]]:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        raw_title = util.clean_html(entry.get("title"))
        title, publisher = split_publisher(raw_title)
        published = util.parse_dt(
            entry.get("published_parsed") or entry.get("published")
        ) or util.now_utc()
        summary = util.clean_html(entry.get("summary"))
        # в summary Google кладёт HTML-список ссылок — от него остаётся мусор
        if summary.count("  ") > 4 or len(summary) > 600:
            summary = ""
        posts.append(
            RawPost(
                source_id=src.id,
                external_id=(entry.get("id") or link)[:400],
                url=link,
                title=title,
                text=summary or title,
                published_at=published,
                publisher=publisher or src.publisher,
                lang=src.lang,
                extra={"gnews": True},
            )
        )
    return FetchResult(posts)
