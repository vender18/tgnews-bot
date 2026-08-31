"""Раннер сбора: обходит источники, изолирует падения, складывает посты в БД."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from .. import filters, util
from ..config import Source, config, sources
from ..db import DB, dumps
from . import gnews, html, rss, tgweb
from .base import FetchResult, RawPost

log = logging.getLogger("collect")

FETCHERS = {
    "rss": rss.fetch,
    "gnews": gnews.fetch,
    "telegram_web": tgweb.fetch,
    "html": html.fetch,
}


def all_sources(db: DB) -> dict[str, Source]:
    """Каталог из sources.yaml плюс то, что добавлено командой /add."""
    result = dict(sources())
    for row in db.query("SELECT id, payload FROM user_sources"):
        try:
            src = Source.from_dict(json.loads(row["payload"]))
            result[src.id] = src
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("битый пользовательский источник %s: %s", row["id"], exc)
    return result


def state_of(db: DB, source_id: str) -> dict:
    row = db.one("SELECT * FROM sources_state WHERE id = ?", (source_id,))
    if row:
        return row
    db.execute("INSERT INTO sources_state (id) VALUES (?) ON CONFLICT (id) DO NOTHING",
               (source_id,))
    return db.one("SELECT * FROM sources_state WHERE id = ?", (source_id,)) or {"id": source_id}


_weight_cache: dict[str, float] | None = None


def weights(db: DB) -> dict[str, float]:
    """Веса всех источников одним запросом: на удалённой БД поштучные не окупаются."""
    global _weight_cache
    if _weight_cache is None:
        _weight_cache = {
            row["id"]: float(row["weight"])
            for row in db.query("SELECT id, weight FROM sources_state WHERE weight IS NOT NULL")
        }
    return _weight_cache


def reset_weight_cache() -> None:
    global _weight_cache
    _weight_cache = None


def effective_weight(db: DB, src: Source) -> float:
    if src is None:
        return 0.5
    tuned = weights(db).get(src.id)
    return float(tuned) if tuned is not None else float(src.weight)


def is_due(src: Source, state: dict, now) -> bool:
    intervals = config()["collect"]["interval_minutes"]
    minutes = intervals.get(src.type, 15)
    last = util.parse_dt(state.get("last_fetch"))
    if last is None:
        return True
    return now - last >= timedelta(minutes=minutes)


def store_post(db: DB, src: Source, raw: RawPost) -> str:
    """Кладёт пост. Возвращает 'new' | 'dup' | 'old' | 'dropped:<причина>'."""
    conf = config()["collect"]
    age_hours = (util.now_utc() - raw.published_at).total_seconds() / 3600
    max_age = 24 * 5 if src.low_volume else conf["max_age_hours"]
    if age_hours > max_age:
        return "old"
    if age_hours < -6:  # источник с кривой таймзоной
        raw.published_at = util.now_utc()

    title = raw.title or ""
    text = raw.text or ""
    keep, reason, geo = filters.classify(src, title, text)
    blob = f"{title}\n{text}"

    written = db.execute_count(
        """INSERT INTO posts (source_id, external_id, title, text, url, publisher, lang,
                              section, published_at, fetched_at, simhash, entities,
                              channel, geo, dropped, drop_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (source_id, external_id) DO NOTHING""",
        (
            src.id, raw.external_id, title[:500] or None, text[:6000], raw.url[:900],
            raw.publisher or src.publisher, raw.lang or src.lang, raw.section,
            util.iso(raw.published_at), util.now_iso(),
            str(util.simhash(blob)), dumps(sorted(util.entities(blob))),
            src.channel, dumps(geo), 0 if keep else 1, reason,
        ),
    )
    if not written:
        return "dup"
    return "new" if keep else f"dropped:{reason}"


def fetch_source(src: Source, state: dict) -> FetchResult:
    fetcher = FETCHERS.get(src.type)
    if fetcher is None:
        return FetchResult(error=f"неизвестный тип {src.type}")
    result = fetcher(src, etag=state.get("etag"), last_modified=state.get("last_modified"))
    # запасные пути, если основной фид переехал
    if (result.error or (not result.posts and not result.not_modified)) and src.type == "rss":
        if src.fallback_url:
            alt = rss.fetch(src, url_override=src.fallback_url)
            if alt.posts:
                return alt
        if src.gnews_fallback:
            alt = gnews.fetch(src, query=src.gnews_fallback)
            if alt.posts:
                return alt
    return result


def collect_all(db: DB, *, only: list[str] | None = None, force: bool = False) -> dict:
    now = util.now_utc()
    catalog = all_sources(db)
    stats = {"checked": 0, "new": 0, "dropped": 0, "errors": [], "per_source": {}}

    for source_id, src in catalog.items():
        if only and source_id not in only:
            continue
        if util.out_of_time(reserve=150):
            stats["errors"].append("сбор прерван по времени, продолжится следующим прогоном")
            break
        state = state_of(db, source_id)
        if not force:
            if not state.get("active", 1):
                continue
            muted = state.get("muted_until")
            if muted and muted > util.now_iso():
                continue
            if not is_due(src, state, now):
                continue

        stats["checked"] += 1
        try:
            result = fetch_source(src, state)
        except Exception as exc:  # noqa: BLE001 — падение источника не должно ронять конвейер
            result = FetchResult(error=f"{type(exc).__name__}: {exc}")

        if result.error:
            fails = int(state.get("fail_count") or 0) + 1
            deactivate = fails >= config()["collect"]["deactivate_after_failures"]
            db.execute(
                """UPDATE sources_state
                   SET last_fetch = ?, last_error = ?, fail_count = ?, active = ?
                   WHERE id = ?""",
                (util.now_iso(), result.error[:300], fails, 0 if deactivate else 1, source_id),
            )
            stats["errors"].append(f"{source_id}: {result.error}")
            log.warning("источник %s: %s", source_id, result.error)
            continue

        added = dropped = 0
        for raw in result.posts:
            try:
                outcome = store_post(db, src, raw)
            except Exception as exc:  # noqa: BLE001
                log.warning("пост %s/%s не сохранён: %s", source_id, raw.external_id, exc)
                continue
            if outcome == "new":
                added += 1
            elif outcome.startswith("dropped"):
                dropped += 1

        db.execute(
            """UPDATE sources_state
               SET last_fetch = ?, last_ok = ?, last_error = NULL, fail_count = 0,
                   etag = ?, last_modified = ?, items_total = items_total + ?
               WHERE id = ?""",
            (util.now_iso(), util.now_iso(), result.etag, result.last_modified, added, source_id),
        )
        stats["new"] += added
        stats["dropped"] += dropped
        if added or dropped:
            stats["per_source"][source_id] = {"new": added, "dropped": dropped}

    return stats


def check_sources(db: DB) -> list[dict]:
    """Проверка живости: HTTP 200 и хотя бы 3 записи за последние 48 часов."""
    catalog = all_sources(db)
    report: list[dict] = []
    fresh_after = util.now_utc() - timedelta(hours=48)

    for source_id, src in catalog.items():
        entry = {"id": source_id, "type": src.type, "channel": src.channel, "ok": False,
                 "items": 0, "fresh": 0, "error": None}
        state_of(db, source_id)
        try:
            result = fetch_source(src, {})
        except Exception as exc:  # noqa: BLE001
            result = FetchResult(error=f"{type(exc).__name__}: {exc}")

        broken = False  # источник недоступен, а не просто молчит
        if result.error:
            entry["error"] = result.error
            broken = True
        else:
            entry["items"] = len(result.posts)
            entry["fresh"] = sum(1 for p in result.posts if p.published_at >= fresh_after)
            if src.low_volume:
                # выходит редко: достаточно одной записи за неделю
                week = util.now_utc() - timedelta(days=7)
                weekly = sum(1 for p in result.posts if p.published_at >= week)
                entry["ok"] = weekly >= 1
                entry["fresh"] = weekly
                if not entry["ok"]:
                    entry["error"] = "ни одной записи за неделю"
            else:
                entry["ok"] = entry["fresh"] >= 3
                if not entry["ok"]:
                    entry["error"] = f"свежих записей за 48ч: {entry['fresh']}"
        report.append(entry)

        # отключаем только то, что реально не отвечает: пустая выдача бывает
        # временной — из-за гео, кеша издания или редких публикаций
        db.execute(
            "UPDATE sources_state SET last_error = ?, active = ?, fail_count = 0 WHERE id = ?",
            (entry["error"], 0 if broken else 1, source_id),
        )
    return report
