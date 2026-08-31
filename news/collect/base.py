"""Общий контракт сбора: все способы отдают RawPost."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .. import config as cfg


@dataclass
class RawPost:
    source_id: str
    external_id: str
    url: str
    text: str
    published_at: datetime
    title: str | None = None
    section: str | None = None
    publisher: str | None = None
    lang: str | None = None
    extra: dict = field(default_factory=dict)


class FetchResult:
    """Что вернул один опрос источника."""

    def __init__(self, posts: list[RawPost] | None = None, *, not_modified: bool = False,
                 etag: str | None = None, last_modified: str | None = None,
                 error: str | None = None) -> None:
        self.posts = posts or []
        self.not_modified = not_modified
        self.etag = etag
        self.last_modified = last_modified
        self.error = error


def http_client(timeout: float | None = None) -> httpx.Client:
    collect = cfg.config()["collect"]
    return httpx.Client(
        timeout=timeout or collect["http_timeout"],
        follow_redirects=True,
        headers={
            "User-Agent": collect["user_agent"],
            "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "ru,en;q=0.8,fr;q=0.7",
        },
    )
