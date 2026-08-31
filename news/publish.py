"""Публикация: суммаризация, форматирование, две полосы, лимиты, тихие часы."""
from __future__ import annotations

import logging

from . import config as cfg
from . import extract, telegram, util
from .collect import effective_weight, reset_weight_cache
from .config import config, source as get_source
from .db import DB, dumps
from .dedup import cluster_posts, independent_sources
from .llm import LLM

log = logging.getLogger("publish")

CHANNEL_TITLE = {
    "A": "Главное",
    "B": "Город",
    "C": "Разбор",
}


# --- состояние -------------------------------------------------------------

def quiet_now(db: DB) -> bool:
    override = db.kv_get("quiet_hours")
    conf = override or config()["quiet_hours"]
    if conf.get("disabled"):
        return False
    return util.in_quiet_hours(util.now_msk(), conf["start"], conf["end"])


def exam_mode(db: DB) -> bool:
    return bool(db.kv_get("exam_mode", False))


def threshold(db: DB, channel: str) -> float:
    conf = config()
    value = float(conf["score"]["publish_threshold"].get(channel, 50))
    if exam_mode(db):
        value += float(conf["exam_mode"]["threshold_bonus"])
    return value


def published_last_day(db: DB, channel: str, kind: str | None = None) -> int:
    since = util.ago_iso(hours=24)
    if kind:
        return int(db.scalar(
            "SELECT COUNT(*) AS c FROM publications WHERE channel = ? AND kind = ? AND created_at >= ?",
            (channel, kind, since), 0))
    return int(db.scalar(
        "SELECT COUNT(*) AS c FROM clusters WHERE channel = ? AND published_at >= ?",
        (channel, since), 0))


# --- суммаризация ----------------------------------------------------------

def _best_post(posts: list[dict], db: DB) -> dict:
    if not posts:
        return {}
    return max(posts, key=lambda p: (
        effective_weight(db, get_source(p["source_id"])) if get_source(p["source_id"]) else 0.5,
        len(p.get("text") or ""),
    ))


def _fallback_summary(cluster: dict, posts: list[dict], db: DB) -> tuple[str, str]:
    """Без модели: заголовок лучшего источника плюс первые фразы по существу."""
    best = _best_post(posts, db)
    headline = (best.get("title") or cluster.get("title") or "").strip()
    summary = ""
    ordered = [best] + [p for p in posts if p["id"] != best["id"]]
    for post in ordered:
        text = util.strip_boilerplate(util.strip_promo(post.get("text") or ""))
        if headline and text.startswith(headline):
            text = text[len(headline):].strip(" .—-")
        candidate = " ".join(util.sentences(text, 2))
        if len(candidate) >= 60:
            summary = candidate
            break
    return util.shorten(headline, 140), util.shorten(summary, 320)


def summarize_batch(db: DB, llm: LLM, items: list[tuple[dict, list[dict]]]
                    ) -> dict[int, tuple[str, str]]:
    """Один вызов модели на весь дайджест — дешевле, чем по кластеру за раз."""
    result: dict[int, tuple[str, str]] = {}
    if not items:
        return result

    if not llm.available:
        for cluster, posts in items:
            result[int(cluster["id"])] = _fallback_summary(cluster, posts, db)
        return result

    blocks = []
    for index, (cluster, posts) in enumerate(items, start=1):
        _, names = independent_sources(posts)
        best = _best_post(posts, db)
        body = util.shorten(util.strip_promo(best.get("text") or ""),
                            int(llm.tune("summary_body_chars", 1200)))
        others = [util.shorten(p.get("title") or "", 120) for p in posts[:4]
                  if p["id"] != best["id"]]
        blocks.append(
            f"{index}. Заголовок: {best.get('title') or cluster.get('title')}\n"
            f"Издания: {', '.join(names[:6])}\n"
            f"Текст: {body}\n"
            + (f"Другие формулировки: {'; '.join(others)}\n" if others else "")
        )

    prompt = (
        "Ты собираешь новостной дайджест для одного человека. Для каждого материала дай:\n"
        "1) заголовок — до 90 знаков, без кликбейта, без восклицаний;\n"
        "2) суть — 1–2 предложения, только факты: что произошло, кого касается, "
        "какие цифры названы. Без вводных слов, без «как сообщает», без оценок.\n"
        "Иностранные материалы излагай по-русски.\n"
        "Если издания расходятся в подаче — укажи это одной фразой в конце сути.\n\n"
        + "\n".join(blocks)
        + '\n\nОтветь только JSON-массивом: [{"i": 1, "headline": "...", "summary": "..."}]'
    )
    data = llm.ask_json(prompt, purpose="summary", smart=True, max_tokens=2000, effort="medium")

    if isinstance(data, list):
        for item in data:
            try:
                index = int(item["i"]) - 1
                if not 0 <= index < len(items):
                    continue
                cluster, _posts = items[index]
                headline = util.shorten(str(item.get("headline") or "").strip(), 160)
                summary = util.shorten(str(item.get("summary") or "").strip(), 400)
                if headline:
                    result[int(cluster["id"])] = (headline, summary)
            except (KeyError, TypeError, ValueError):
                continue

    for cluster, posts in items:
        result.setdefault(int(cluster["id"]), _fallback_summary(cluster, posts, db))
    return result


# --- форматирование --------------------------------------------------------

def _link(db: DB, posts: list[dict]) -> str:
    best = _best_post(posts, db)
    return extract.resolve_url(db, best.get("url") or "")


def _sources_line(posts: list[dict]) -> str:
    _, names = independent_sources(posts)
    return " · ".join(util.esc(n) for n in names[:5])


def _divergence_note(posts: list[dict], divergence: int) -> str:
    if not divergence:
        return ""
    russian = [p.get("publisher") or p["source_id"] for p in posts
               if not (get_source(p["source_id"]) and get_source(p["source_id"]).foreign)]
    foreign = [p.get("publisher") or p["source_id"] for p in posts
               if get_source(p["source_id"]) and get_source(p["source_id"]).foreign]
    if divergence == 2:
        return f"\n⚖️ Освещают только иностранные издания: {util.esc(', '.join(sorted(set(foreign))[:4]))}"
    return (f"\n⚖️ Две оптики: {util.esc(', '.join(sorted(set(russian))[:3]))} "
            f"и {util.esc(', '.join(sorted(set(foreign))[:3]))}")


def format_item(db: DB, index: int, cluster: dict, posts: list[dict],
                headline: str, summary: str) -> str:
    url = _link(db, posts)
    mark = "⚖️ " if cluster.get("divergence") else ""
    headline = util.shorten(headline, 120)
    lines = [f"{mark}<b>{index}. {util.esc(headline)}</b>"]
    # не повторять заголовок в сути — так бывает у телеграм-источников
    if summary and util.jaccard(set(util.tokens(headline)), set(util.tokens(summary))) > 0.75:
        summary = ""
    if summary:
        lines.append(util.esc(summary))
    tail = _sources_line(posts)
    if url:
        tail += f" · <a href=\"{util.esc(url)}\">открыть</a>"
    lines.append(f"<i>{tail}</i>")
    return "\n".join(lines)


def digest_keyboard(cluster_ids: list[int]) -> list[list[dict]]:
    """Ряд номеров: нажатие раскрывает оценку конкретного пункта."""
    row: list[dict] = []
    keyboard: list[list[dict]] = []
    for index, cluster_id in enumerate(cluster_ids, start=1):
        row.append({"text": str(index), "callback_data": f"p:{cluster_id}:{index}"})
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return keyboard


def vote_keyboard(cluster_id: int, index: int, back: str | None = None) -> list[list[dict]]:
    row = [
        {"text": f"👍 {index}", "callback_data": f"u:{cluster_id}:{index}"},
        {"text": f"👎 {index}", "callback_data": f"d:{cluster_id}:{index}"},
    ]
    if back:
        row.append({"text": "←", "callback_data": f"b:{back}"})
    return [row]


# --- полосы ----------------------------------------------------------------

def _mark_published(db: DB, cluster_ids: list[int], kind: str) -> None:
    now = util.now_iso()
    for cluster_id in cluster_ids:
        db.execute(
            "UPDATE clusters SET status = 'published', published_at = ?, published_kind = ? "
            "WHERE id = ?",
            (now, kind, cluster_id),
        )


def _record_publication(db: DB, channel: str, kind: str, slot: str | None,
                        message_id: int | None, cluster_ids: list[int]) -> int | None:
    return db.insert_returning_id(
        """INSERT INTO publications (channel, kind, slot, message_id, cluster_ids, created_at)
           VALUES (?,?,?,?,?,?) RETURNING id""",
        (channel, kind, slot, message_id, dumps(cluster_ids), util.now_iso()),
    )


def pick_for_digest(db: DB, channel: str, limit: int) -> list[dict]:
    since = util.ago_iso(hours=30)
    return db.query(
        """SELECT * FROM clusters
           WHERE channel = ? AND status IN ('scored', 'queued')
             AND score >= ? AND updated_at >= ?
           ORDER BY score DESC, source_count DESC LIMIT ?""",
        (channel, threshold(db, channel), since, limit),
    )


def publish_digest(db: DB, llm: LLM, channel: str, slot: str | None = None,
                   *, force: bool = False) -> dict:
    conf = config()
    limits = conf["limits"]
    room = limits["max_per_day"].get(channel, 20) - published_last_day(db, channel)
    if room <= 0 and not force:
        return {"published": 0, "reason": "исчерпан дневной лимит канала"}

    want = min(limits["digest_items"].get(channel, 5), max(room, 1))
    clusters = pick_for_digest(db, channel, want)
    if not clusters:
        return {"published": 0, "reason": "нечего публиковать"}

    items = [(cluster, cluster_posts(db, cluster["id"])) for cluster in clusters]
    items = [(cluster, posts) for cluster, posts in items if posts]
    if not items:
        return {"published": 0, "reason": "нечего публиковать"}

    extract.enrich(db, [p for _cluster, posts in items for p in posts[:1]])
    items = [(cluster, cluster_posts(db, cluster["id"])) for cluster, _ in items]

    summaries = summarize_batch(db, llm, items)
    header_time = util.now_msk().strftime("%H:%M")
    header = f"<b>{CHANNEL_TITLE.get(channel, channel)} · {header_time}</b>"

    blocks = [header, ""]
    used: list[int] = []
    for index, (cluster, posts) in enumerate(items, start=1):
        headline, summary = summaries.get(int(cluster["id"]), ("", ""))
        if not headline:
            continue
        block = format_item(db, len(used) + 1, cluster, posts, headline, summary)
        if sum(len(b) + 2 for b in blocks) + len(block) > 3900:
            break
        blocks.append(block)
        blocks.append("")
        used.append(int(cluster["id"]))
        db.execute("UPDATE clusters SET headline = ?, summary = ? WHERE id = ?",
                   (headline, summary, cluster["id"]))

    if not used:
        return {"published": 0, "reason": "нечего публиковать"}

    chat_id = cfg.channel_chat_id(channel)
    if not chat_id:
        return {"published": 0, "reason": f"не задан TG_CHANNEL_{channel}"}

    text = "\n".join(blocks).strip()
    silent = quiet_now(db)
    message = telegram.send_message(chat_id, text, silent=silent)
    message_id = message.get("message_id") if message else None

    _mark_published(db, used, "digest")
    pub_id = _record_publication(db, channel, "digest", slot, message_id, used)
    if message_id and pub_id:
        telegram.edit_markup(chat_id, message_id, digest_keyboard(used))
    return {"published": len(used), "message_id": message_id, "clusters": used}


def publish_urgent(db: DB, llm: LLM) -> dict:
    """Экстренная полоса: высокий скор, минимум два независимых источника, лимит в сутки."""
    conf = config()["score"]
    limits = config()["limits"]["urgent_per_day"]
    stats = {"published": 0, "queued": 0}

    for channel in ("A", "B"):
        allowed = limits.get(channel, 0)
        if allowed <= 0:
            continue
        already = published_last_day(db, channel, kind="urgent")
        candidates = db.query(
            """SELECT * FROM clusters
               WHERE channel = ? AND status IN ('scored', 'queued')
                 AND score >= ? AND source_count >= ? AND updated_at >= ?
               ORDER BY score DESC LIMIT 5""",
            (channel, conf["urgent_threshold"], conf["urgent_min_sources"],
             util.ago_iso(hours=12)),
        )
        if not candidates:
            continue

        if quiet_now(db):
            # с 23:00 до 07:30 ни одного уведомления — копим и отдаём утром
            for cluster in candidates:
                if cluster["status"] != "queued":
                    db.execute("UPDATE clusters SET status = 'queued' WHERE id = ?",
                               (cluster["id"],))
                    stats["queued"] += 1
            continue

        for cluster in candidates:
            if already >= allowed:
                break
            posts = cluster_posts(db, cluster["id"])
            if not posts:
                continue
            extract.enrich(db, posts[:2], budget=2)
            posts = cluster_posts(db, cluster["id"])
            summary_map = summarize_batch(db, llm, [(cluster, posts)])
            headline, summary = summary_map.get(int(cluster["id"]), ("", ""))
            if not headline:
                continue

            chat_id = cfg.channel_chat_id(channel)
            if not chat_id:
                break
            url = _link(db, posts)
            text = (
                f"⚡️ <b>{util.esc(headline)}</b>\n\n"
                f"{util.esc(summary)}\n"
                f"{_divergence_note(posts, int(cluster.get('divergence') or 0))}\n"
                f"<i>{_sources_line(posts)}"
                + (f" · <a href=\"{util.esc(url)}\">открыть</a>" if url else "")
                + "</i>"
            )
            message = telegram.send_message(chat_id, text)
            message_id = message.get("message_id") if message else None
            db.execute("UPDATE clusters SET headline = ?, summary = ?, urgent = 1 WHERE id = ?",
                       (headline, summary, cluster["id"]))
            _mark_published(db, [int(cluster["id"])], "urgent")
            _record_publication(db, channel, "urgent", None, message_id, [int(cluster["id"])])
            if message_id:
                telegram.edit_markup(chat_id, message_id,
                                     vote_keyboard(int(cluster["id"]), 1))
            already += 1
            stats["published"] += 1
    return stats


def release_queue(db: DB, llm: LLM) -> dict:
    """После тихих часов отдаём накопленное экстренное — одним постом, без шума."""
    if quiet_now(db):
        return {"published": 0}
    queued = db.query(
        "SELECT * FROM clusters WHERE status = 'queued' AND updated_at >= ? "
        "ORDER BY score DESC LIMIT 10",
        (util.ago_iso(hours=14),),
    )
    if not queued:
        return {"published": 0}
    total = 0
    for channel in ("A", "B", "C"):
        part = [c for c in queued if c["channel"] == channel]
        if not part:
            continue
        result = publish_digest(db, llm, channel, slot="queue", force=True)
        total += result.get("published", 0)
    # то, что не влезло, остаётся в очереди до ближайшего слота
    return {"published": total}


def register_vote(db: DB, cluster_id: int, vote: int) -> str:
    """👍/👎 подкручивает веса источников кластера в диапазоне 0.2–1.2."""
    posts = cluster_posts(db, cluster_id)
    if not posts:
        return "материал не найден"
    step = 0.05 * (1 if vote > 0 else -1)
    touched = []
    for post in posts:
        src = get_source(post["source_id"])
        if not src:
            continue
        current = effective_weight(db, src)
        updated = max(0.2, min(1.2, round(current + step, 3)))
        db.execute(
            "INSERT INTO sources_state (id, weight) VALUES (?, ?) "
            "ON CONFLICT (id) DO UPDATE SET weight = excluded.weight",
            (src.id, updated),
        )
        touched.append(src.id)
    db.execute("INSERT INTO votes (cluster_id, source_id, vote, created_at) VALUES (?,?,?,?)",
               (cluster_id, ",".join(touched)[:200], vote, util.now_iso()))
    reset_weight_cache()
    column = "votes_up" if vote > 0 else "votes_down"
    db.execute(f"UPDATE clusters SET {column} = {column} + 1 WHERE id = ?", (cluster_id,))
    return "учтено: важное" if vote > 0 else "учтено: лишнее"
