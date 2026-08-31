"""Один тик конвейера: собрать → склеить → оценить → опубликовать что положено."""
from __future__ import annotations

import logging
from datetime import datetime

from . import collect, commands, dedup, french, publish, reports, score, telegram, util
from .config import config
from .db import DB
from .llm import LLM

log = logging.getLogger("pipeline")


def _slot_time(now_msk: datetime, hhmm: str) -> datetime:
    hours, _, minutes = hhmm.partition(":")
    return now_msk.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0)


def _due_slots(db: DB, now: datetime) -> list[tuple[str, str]]:
    """Слоты, которые пора отработать: время наступило, но не позже окна догона."""
    conf = config()
    schedule = dict(conf["schedule"])
    if publish.exam_mode(db):
        schedule.update(conf["exam_mode"]["schedule"])

    catchup = conf["schedule"]["catchup_minutes"]
    today = now.strftime("%Y-%m-%d")
    due: list[tuple[str, str]] = []

    def consider(kind: str, hhmm: str, *, weekday: int | None = None) -> None:
        if weekday is not None and now.weekday() != weekday:
            return
        moment = _slot_time(now, hhmm)
        if now < moment:
            return
        if (now - moment).total_seconds() > catchup * 60:
            return
        key = f"{kind}:{today}:{hhmm}"
        if not db.slot_done(key):
            due.append((kind, key))

    for channel in ("A", "B", "C"):
        for hhmm in schedule.get(channel, []):
            consider(channel, hhmm)
    for hhmm in schedule.get("french", []):
        consider("french", hhmm)

    weekly = conf["schedule"]["weekly_review"]
    consider("weekly", weekly["time"], weekday=weekly["weekday"])
    report = conf["schedule"]["filter_report"]
    consider("filter_report", report["time"], weekday=report["weekday"])

    # выход из тихих часов: отдаём накопленное
    consider("queue", conf["quiet_hours"]["end"])
    consider("retention", "04:10")
    return due


def run_schedule(db: DB, llm: LLM) -> dict:
    now = util.now_msk()
    results: dict[str, object] = {}
    for kind, key in _due_slots(db, now):
        try:
            if kind in ("A", "B", "C"):
                results[key] = publish.publish_digest(db, llm, kind, slot=key)
            elif kind == "french":
                results[key] = french.publish(db, llm)
            elif kind == "weekly":
                results[key] = reports.weekly_review(db, llm)
            elif kind == "filter_report":
                results[key] = reports.filter_report(db)
            elif kind == "queue":
                results[key] = publish.release_queue(db, llm)
            elif kind == "retention":
                results[key] = retention(db)
        except Exception as exc:  # noqa: BLE001 — сбой одного слота не рушит тик
            log.exception("слот %s упал: %s", key, exc)
            results[key] = {"error": str(exc)}
            telegram.notify_owner(f"Слот {key} упал: {util.esc(str(exc)[:200])}", silent=True)
        db.mark_slot(key, util.now_iso())
    return results


def retention(db: DB) -> dict:
    conf = config()["retention"]
    posts_before = util.ago_iso(days=conf["posts_days"])
    clusters_before = util.ago_iso(days=conf["clusters_days"])
    db.execute("DELETE FROM posts WHERE fetched_at < ?", (posts_before,))
    db.execute("DELETE FROM clusters WHERE updated_at < ?", (clusters_before,))
    db.execute("DELETE FROM url_cache WHERE created_at < ?", (util.ago_iso(days=14),))
    db.execute("DELETE FROM schedule_runs WHERE ran_at < ?", (util.ago_iso(days=30),))
    db.execute("DELETE FROM llm_usage WHERE created_at < ?", (util.ago_iso(days=120),))
    return {"cleaned": True}


def tick(db: DB, llm: LLM, *, collect_only: bool = False) -> dict:
    util.set_deadline(float(config().get("run_budget_seconds", 660)))
    stats: dict[str, object] = {}
    stats["collect"] = collect.collect_all(db)
    if collect_only:
        return stats
    stats["dedup"] = dedup.run(db, llm)
    stats["score"] = score.run(db, llm)
    stats["urgent"] = publish.publish_urgent(db, llm)
    stats["schedule"] = run_schedule(db, llm)
    stats["commands"] = commands.poll(db, llm)
    stats["llm_spent"] = round(llm.spent_today(), 4)
    return stats
