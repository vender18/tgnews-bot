"""Способ 4 — HTML-парсинг. Последнее средство, каждый источник изолирован.

Селекторы описываются прямо в sources.yaml:

    - id: example
      type: html
      url: "https://example.com/news"
      selectors:
        item: "article.news-item"
        title: "h2"
        link: "a"
        date: "time"
        text: "p.lead"

Сломался селектор — источник помечается неактивным, конвейер идёт дальше.
"""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import util
from ..config import Source
from .base import FetchResult, RawPost, http_client


def fetch(src: Source, **_: object) -> FetchResult:
    if not src.url:
        return FetchResult(error="нет url")
    selectors = src.selectors or {}
    item_selector = selectors.get("item")
    if not item_selector:
        return FetchResult(error="не описан selectors.item")

    with http_client() as client:
        response = client.get(src.url)
    if response.status_code >= 400:
        return FetchResult(error=f"HTTP {response.status_code}")

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select(item_selector)
    if not items:
        return FetchResult(error=f"селектор {item_selector} ничего не нашёл")

    posts: list[RawPost] = []
    for item in items[:60]:
        link_el = item.select_one(selectors.get("link", "a"))
        href = link_el.get("href") if link_el else None
        if not href:
            continue
        url = urljoin(src.url, href)
        title_el = item.select_one(selectors["title"]) if selectors.get("title") else link_el
        title = util.clean_html(title_el.get_text(" ", strip=True)) if title_el else ""
        text_el = item.select_one(selectors["text"]) if selectors.get("text") else None
        text = util.clean_html(text_el.get_text(" ", strip=True)) if text_el else ""
        date_el = item.select_one(selectors["date"]) if selectors.get("date") else None
        published = None
        if date_el:
            published = util.parse_dt(date_el.get("datetime") or date_el.get_text(strip=True))
        posts.append(
            RawPost(
                source_id=src.id,
                external_id=url[:400],
                url=url,
                title=title or None,
                text=text or title,
                published_at=published or util.now_utc(),
                publisher=src.publisher,
                lang=src.lang,
            )
        )
    return FetchResult(posts)
