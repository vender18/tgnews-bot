"""Недельный обзор (в канал A) и отчёт по фильтрации (в личку)."""
from __future__ import annotations

import logging

from . import config as cfg
from . import telegram, util
from .db import DB
from .llm import LLM

log = logging.getLogger("reports")


def weekly_review(db: DB, llm: LLM) -> dict:
    rows = db.query(
        """SELECT id, channel, headline, title, summary, score, source_count, published_at
           FROM clusters
           WHERE published_at >= ? AND channel IN ('A','B')
           ORDER BY score DESC LIMIT 12""",
        (util.ago_iso(days=7),),
    )
    if not rows:
        return {"published": 0, "reason": "за неделю нечего вспоминать"}

    items = []
    for row in rows:
        title = row.get("headline") or row.get("title") or ""
        items.append(f"- [{row['channel']}] {title} ({int(row.get('source_count') or 1)} источн.)")

    body = ""
    if llm.available:
        prompt = (
            "Ниже главные новости недели, которые читатель уже видел. "
            "Собери короткий обзор: 4–6 пунктов, каждый — одна фраза о том, "
            "что за неделю реально изменилось, а не пересказ заголовка. "
            "Сгруппируй по смыслу, без вводных и без оценок.\n\n"
            + "\n".join(items)
            + "\n\nОтветь простым текстом, каждый пункт с новой строки, начиная с «· »."
        )
        body = llm.ask(prompt, purpose="weekly", smart=True, max_tokens=900, effort="medium") or ""

    if not body:
        body = "\n".join(f"· {util.shorten(item[4:], 110)}" for item in items[:6])

    text = f"<b>Неделя · итоги</b>\n\n{util.esc(body)}"
    chat_id = cfg.channel_chat_id("A")
    if not chat_id:
        return {"published": 0, "reason": "не задан TG_CHANNEL_A"}
    message = telegram.try_send(chat_id, text)
    db.execute(
        "INSERT INTO publications (channel, kind, slot, message_id, cluster_ids, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("A", "weekly", "weekly", (message or {}).get("message_id"), "[]", util.now_iso()),
    )
    return {"published": 1}


def filter_report(db: DB) -> dict:
    """Что конвейер выбросил за неделю — чтобы видеть, не режет ли он нужное."""
    since = util.ago_iso(days=7)
    dropped = db.query(
        """SELECT drop_reason, COUNT(*) AS c FROM posts
           WHERE fetched_at >= ? AND dropped = 1
           GROUP BY drop_reason ORDER BY c DESC""",
        (since,),
    )
    total = int(db.scalar("SELECT COUNT(*) AS c FROM posts WHERE fetched_at >= ?", (since,), 0))
    published = int(db.scalar(
        "SELECT COUNT(*) AS c FROM clusters WHERE published_at >= ?", (since,), 0))
    below = db.query(
        """SELECT channel, COUNT(*) AS c FROM clusters
           WHERE created_at >= ? AND status IN ('scored','new') GROUP BY channel""",
        (since,),
    )
    noisy = db.query(
        """SELECT source_id, COUNT(*) AS c FROM posts
           WHERE fetched_at >= ? AND dropped = 1 GROUP BY source_id ORDER BY c DESC LIMIT 6""",
        (since,),
    )
    votes = db.query(
        """SELECT SUM(votes_up) AS up, SUM(votes_down) AS down FROM clusters
           WHERE published_at >= ?""", (since,))

    lines = [
        "<b>Отчёт по фильтрации за неделю</b>",
        f"собрано: {total}, опубликовано событий: {published}",
        "",
        "<b>Отсеяно на входе</b>",
    ]
    for row in dropped:
        lines.append(f"· {util.esc(str(row['drop_reason'] or 'без причины'))}: {row['c']}")
    if not dropped:
        lines.append("· ничего")

    lines += ["", "<b>Не дотянуло до порога</b>"]
    for row in below:
        lines.append(f"· канал {row['channel']}: {row['c']} событий")
    if not below:
        lines.append("· ничего")

    if noisy:
        lines += ["", "<b>Больше всего шума дают</b>"]
        for row in noisy:
            lines.append(f"· {util.esc(row['source_id'])}: {row['c']}")

    if votes and votes[0].get("up") is not None:
        lines += ["", f"Оценки: 👍 {int(votes[0].get('up') or 0)} · 👎 {int(votes[0].get('down') or 0)}"]

    spent = db.scalar("SELECT COALESCE(SUM(cost),0) AS c FROM llm_usage WHERE created_at >= ?",
                      (since,), 0.0)
    lines.append(f"Расход на модель за неделю: ${float(spent):.3f}")

    telegram.notify_owner("\n".join(lines), silent=True)
    return {"sent": 1}
