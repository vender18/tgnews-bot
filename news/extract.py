"""Дотягивание полного текста статьи и разворот ссылок Google News.

RSS-описания обрезаны на 200–300 символов: для кластеризации хватает,
для суммаризации иногда нет. Тянем только то, что реально идёт в публикацию.
"""
from __future__ import annotations

import logging

import httpx

from . import util
from .config import config
from .collect.base import http_client
from .db import DB

log = logging.getLogger("extract")


def resolve_url(db: DB, url: str) -> str:
    """Ссылки Google News идут через редирект — в канале должна быть прямая."""
    if not url or "news.google.com" not in url:
        return url
    cached = db.one("SELECT resolved FROM url_cache WHERE src = ?", (url[:900],))
    if cached:
        return cached["resolved"]

    resolved = url
    try:
        with http_client(timeout=20) as client:
            response = client.get(url)
            final = str(response.url)
            if "news.google.com" not in final:
                resolved = final
            else:
                # новые ссылки Google отдают промежуточную страницу с data-n-au / ссылкой
                import re

                match = re.search(r'data-n-au="([^"]+)"', response.text)
                if not match:
                    match = re.search(r'<a[^>]+href="(https?://(?!news\.google)[^"]+)"',
                                      response.text)
                if match:
                    resolved = match.group(1)
    except httpx.HTTPError as exc:
        log.debug("не развернул ссылку %s: %s", url, exc)

    db.execute(
        "INSERT INTO url_cache (src, resolved, created_at) VALUES (?,?,?) "
        "ON CONFLICT (src) DO UPDATE SET resolved = excluded.resolved",
        (url[:900], resolved[:900], util.now_iso()),
    )
    return resolved


def article_text(url: str) -> str:
    """Полный текст статьи. trafilatura, если установлена, иначе грубый fallback."""
    if not url:
        return ""
    try:
        with http_client(timeout=25) as client:
            response = client.get(url)
        if response.status_code >= 400:
            return ""
        html = response.text
    except httpx.HTTPError as exc:
        log.debug("статья не скачалась %s: %s", url, exc)
        return ""

    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=False,
                                   favor_precision=True)
        if text:
            return util.clean_html(text)
    except Exception as exc:  # noqa: BLE001 — библиотека необязательная
        log.debug("trafilatura не справилась: %s", exc)

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        return util.clean_html("\n".join(p for p in paragraphs if len(p) > 60))
    except Exception:  # noqa: BLE001
        return ""


def enrich(db: DB, posts: list[dict], budget: int | None = None) -> dict[int, str]:
    """Возвращает {post_id: полный текст} для постов, которым коротко."""
    conf = config()["extract"]
    if not conf.get("enabled", True):
        return {}
    limit = budget if budget is not None else conf["max_per_run"]
    result: dict[int, str] = {}
    for post in posts:
        if limit <= 0:
            break
        if len(post.get("text") or "") >= conf["min_chars"]:
            continue
        url = resolve_url(db, post.get("url") or "")
        text = article_text(url)
        limit -= 1
        if len(text) > len(post.get("text") or ""):
            result[int(post["id"])] = text[:8000]
            db.execute("UPDATE posts SET text = ?, url = ? WHERE id = ?",
                       (text[:6000], url[:900], post["id"]))
    return result
