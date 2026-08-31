"""Раннер сбора: обходит источники, изолирует падения, складывает посты в БД."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
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


INSERT_POST_SQL = """INSERT INTO posts (source_id, external_id, title, text, url, publisher,
                          lang, section, published_at, fetched_at, simhash, entities,
                          channel, geo, dropped, drop_reason)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
       ON CONFLICT (source_id, external_id) DO NOTHING"""


def prepare_post(src: Source, raw: RawPost) -> tuple[str, tuple | None]:
    """Готовит строку к вставке. Возвращает ('new'|'old'|'dropped:...', данные)."""
    conf = config()["collect"]
    age_hours = (util.now_utc() - raw.published_at).total_seconds() / 3600
    max_age = 24 * 5 if src.low_volume else conf["max_age_hours"]
    if age_hours > max_age:
        return "old", None
    if age_hours < -6:  # источник с кривой таймзоной
        raw.published_at = util.now_utc()

    title = raw.title or ""
    text = raw.text or ""
    keep, reason, geo = filters.classify(src, title, text)
    blob = f"{title}\n{text}"
    row = (
        src.id, raw.external_id, title[:500] or None, text[:6000], raw.url[:900],
        raw.publisher or src.publisher, raw.lang or src.lang, raw.section,
        util.iso(raw.published_at), util.now_iso(),
        str(util.simhash(blob)), dumps(sorted(util.entities(blob))),
        src.channel, dumps(geo), 0 if keep else 1, reason,
    )
    return ("new" if keep else f"dropped:{reason}"), row


def store_post(db: DB, src: Source, raw: RawPost) -> str:
    """Одиночная вставка — для команд и тестов; в конвейере посты пишутся пачкой."""
    outcome, row = prepare_post(src, raw)
    if row is None:
        return outcome
    if not db.execute_count(INSERT_POST_SQL, row):
        return "dup"
    return outcome


def known_ids(db: DB, source_id: str, external_ids: list[str]) -> set[str]:
    """Какие записи источника уже лежат в базе — одним запросом вместо запроса на пост."""
    if not external_ids:
        return set()
    found: set[str] = set()
    for start in range(0, len(external_ids), 200):
        chunk = external_ids[start:start + 200]
        placeholders = ",".join("?" for _ in chunk)
        rows = db.query(
            f"SELECT external_id FROM posts WHERE source_id = ? "
            f"AND external_id IN ({placeholders})",
            (source_id, *chunk),
        )
        found.update(r["external_id"] for r in rows)
    return found


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


def collect_all(db: DB, *, only: list[str] | None = None, force: bool = False,
                workers: int = 8) -> dict:
    """Опрашивает источники параллельно, а пишет в базу одним потоком и пачками."""
    now = util.now_utc()
    catalog = all_sources(db)
    stats = {"checked": 0, "new": 0, "dropped": 0, "errors": [], "per_source": {}}

    states = {row["id"]: row for row in db.query("SELECT * FROM sources_state")}
    due: list[tuple[Source, dict]] = []
    fresh_states: list[tuple[str]] = []
    for source_id, src in catalog.items():
        if only and source_id not in only:
            continue
        state = states.get(source_id)
        if state is None:
            fresh_states.append((source_id,))
            state = {"id": source_id}
        if not force:
            if not state.get("active", 1):
                continue
            muted = state.get("muted_until")
            if muted and muted > util.now_iso():
                continue
            if not is_due(src, state, now):
                continue
        due.append((src, state))

    db.execute_many("INSERT INTO sources_state (id) VALUES (?) ON CONFLICT (id) DO NOTHING",
                    fresh_states)
    if not due:
        return stats

    def poll(pair: tuple[Source, dict]) -> tuple[Source, dict, FetchResult]:
        src, state = pair
        try:
            return src, state, fetch_source(src, state)
        except Exception as exc:  # noqa: BLE001 — падение источника не рушит конвейер
            return src, state, FetchResult(error=f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(poll, due))

    rows_to_insert: list[tuple] = []
    ok_updates: list[tuple] = []
    fail_updates: list[tuple] = []
    deactivate_after = config()["collect"]["deactivate_after_failures"]

    for src, state, result in results:
        stats["checked"] += 1
        if result.error:
            fails = int(state.get("fail_count") or 0) + 1
            fail_updates.append((util.now_iso(), result.error[:300], fails,
                                 0 if fails >= deactivate_after else 1, src.id))
            stats["errors"].append(f"{src.id}: {result.error}")
            log.warning("источник %s: %s", src.id, result.error)
            continue

        seen = known_ids(db, src.id, [p.external_id for p in result.posts])
        added = dropped = 0
        for raw in result.posts:
            if raw.external_id in seen:
                continue
            try:
                outcome, row = prepare_post(src, raw)
            except Exception as exc:  # noqa: BLE001
                log.warning("пост %s/%s не разобран: %s", src.id, raw.external_id, exc)
                continue
            if row is None:
                continue
            rows_to_insert.append(row)
            if outcome == "new":
                added += 1
            else:
                dropped += 1

        ok_updates.append((util.now_iso(), util.now_iso(), result.etag, result.last_modified,
                           added, src.id))
        stats["new"] += added
        stats["dropped"] += dropped
        if added or dropped:
            stats["per_source"][src.id] = {"new": added, "dropped": dropped}

    db.execute_many(INSERT_POST_SQL, rows_to_insert)
    db.execute_many(
        """UPDATE sources_state
           SET last_fetch = ?, last_error = ?, fail_count = ?, active = ?
           WHERE id = ?""", fail_updates)
    db.execute_many(
        """UPDATE sources_state
           SET last_fetch = ?, last_ok = ?, last_error = NULL, fail_count = 0,
               etag = ?, last_modified = ?, items_total = items_total + ?
           WHERE id = ?""", ok_updates)
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
