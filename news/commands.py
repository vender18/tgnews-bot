"""Команды бота и обратная связь. Опрос через getUpdates — вебхук не нужен."""
from __future__ import annotations

import json
import logging

from . import config as cfg
from . import collect, publish, telegram, util
from .config import Source, config
from .db import DB, loads
from .llm import LLM

log = logging.getLogger("commands")

HELP = """<b>Команды</b>

/digest [A|B|C] — собрать и выпустить дайджест прямо сейчас
/queue — что лежит в очереди и ждёт публикации
/focus тема — поднять тему в приоритете (/focus off — очистить)
/quiet on|off|23:00-07:30 — тихие часы
/exam_mode on|off — режим подготовки к экзаменам
/sources — состояние источников
/add ссылка|@канал|запрос [A|B|C] — добавить источник
/mute id [часы] — временно заглушить источник
/check_sources — проверить живость всех источников
/stats — что происходило за сутки и неделю"""


def _is_owner(chat_id: str | int) -> bool:
    owner = cfg.owner_chat_id()
    return owner is not None and str(chat_id) == str(owner)


# --- отдельные команды -----------------------------------------------------

def cmd_sources(db: DB) -> str:
    rows = {r["id"]: r for r in db.query("SELECT * FROM sources_state")}
    catalog = collect.all_sources(db)
    lines = ["<b>Источники</b>"]
    for channel in ("A", "B", "C"):
        part = [s for s in catalog.values() if s.channel == channel]
        if not part:
            continue
        lines.append(f"\n<b>Канал {channel}</b>")
        for src in sorted(part, key=lambda s: s.id):
            state = rows.get(src.id, {})
            weight = state.get("weight")
            weight = float(weight) if weight is not None else src.weight
            if not state:
                mark = "·"
            elif not state.get("active", 1):
                mark = "✕"
            elif state.get("muted_until") and state["muted_until"] > util.now_iso():
                mark = "🔇"
            elif state.get("last_error"):
                mark = "!"
            else:
                mark = "✓"
            total = state.get("items_total") or 0
            lines.append(f"{mark} {util.esc(src.id)} · вес {weight:.2f} · постов {total}")
            if state.get("last_error"):
                lines.append(f"   <i>{util.esc(str(state['last_error'])[:120])}</i>")
    return "\n".join(lines)


def cmd_stats(db: DB) -> str:
    day = util.ago_iso(hours=24)
    week = util.ago_iso(days=7)
    collected = db.scalar("SELECT COUNT(*) AS c FROM posts WHERE fetched_at >= ?", (day,), 0)
    dropped = db.scalar(
        "SELECT COUNT(*) AS c FROM posts WHERE fetched_at >= ? AND dropped = 1", (day,), 0)
    clusters = db.scalar("SELECT COUNT(*) AS c FROM clusters WHERE created_at >= ?", (day,), 0)
    spent = db.scalar("SELECT COALESCE(SUM(cost),0) AS c FROM llm_usage WHERE created_at >= ?",
                      (day,), 0.0)
    lines = [
        "<b>Сутки</b>",
        f"собрано постов: {collected}, отсеяно сразу: {dropped}",
        f"событий (кластеров): {clusters}",
        f"расход на модель: ${float(spent):.3f}",
        "",
        "<b>Опубликовано за сутки</b>",
    ]
    for channel in ("A", "B", "C"):
        total = publish.published_last_day(db, channel)
        urgent = publish.published_last_day(db, channel, kind="urgent")
        lines.append(f"{channel}: {total} материалов, из них срочных {urgent}")

    top = db.query(
        """SELECT id, headline, title, score, votes_up, votes_down FROM clusters
           WHERE published_at >= ? ORDER BY score DESC LIMIT 5""", (week,))
    if top:
        lines += ["", "<b>Топ недели</b>"]
        for row in top:
            title = row.get("headline") or row.get("title") or ""
            lines.append(f"· {util.esc(util.shorten(title, 80))} — {float(row['score'] or 0):.0f}")
    return "\n".join(lines)


def cmd_queue(db: DB) -> str:
    rows = db.query(
        "SELECT id, channel, score, title, headline FROM clusters "
        "WHERE status IN ('queued','scored') AND score >= 60 ORDER BY score DESC LIMIT 15")
    if not rows:
        return "Очередь пуста."
    lines = ["<b>В очереди</b>"]
    for row in rows:
        title = row.get("headline") or row.get("title") or ""
        lines.append(f"[{row['channel']}] {float(row['score'] or 0):.0f} · "
                     f"{util.esc(util.shorten(title, 90))}")
    return "\n".join(lines)


def cmd_focus(db: DB, args: str) -> str:
    current = list(db.kv_get("focus", []) or [])
    arg = args.strip()
    if not arg:
        return "Фокус: " + (", ".join(current) if current else "пусто")
    if arg.lower() in ("off", "clear", "сброс"):
        db.kv_set("focus", [])
        return "Фокус очищен."
    if arg.startswith("-"):
        term = arg[1:].strip().lower()
        current = [t for t in current if t != term]
        db.kv_set("focus", current)
        return "Убрано. Фокус: " + (", ".join(current) or "пусто")
    current.append(arg.lower())
    db.kv_set("focus", sorted(set(current))[:12])
    return "Фокус: " + ", ".join(sorted(set(current))[:12])


def cmd_quiet(db: DB, args: str) -> str:
    arg = args.strip().lower()
    default = config()["quiet_hours"]
    if not arg:
        conf = db.kv_get("quiet_hours") or default
        if conf.get("disabled"):
            return "Тихие часы выключены."
        return f"Тихие часы: {conf['start']}–{conf['end']}"
    if arg in ("off", "выкл"):
        db.kv_set("quiet_hours", {**default, "disabled": True})
        return "Тихие часы выключены."
    if arg in ("on", "вкл"):
        db.kv_set("quiet_hours", {**default, "disabled": False})
        return f"Тихие часы: {default['start']}–{default['end']}"
    if "-" in arg:
        start, _, end = arg.partition("-")
        try:
            util.hhmm_to_minutes(start.strip())
            util.hhmm_to_minutes(end.strip())
        except ValueError:
            return "Формат: /quiet 23:00-07:30"
        db.kv_set("quiet_hours", {"start": start.strip(), "end": end.strip(), "disabled": False})
        return f"Тихие часы: {start.strip()}–{end.strip()}"
    return "Формат: /quiet on | off | 23:00-07:30"


def cmd_exam_mode(db: DB, args: str) -> str:
    arg = args.strip().lower()
    if arg in ("on", "вкл", "1"):
        db.kv_set("exam_mode", True)
        return "Режим экзаменов включён: меньше новостей, выше порог, развивающее и французский остаются."
    if arg in ("off", "выкл", "0"):
        db.kv_set("exam_mode", False)
        return "Режим экзаменов выключен."
    return "Режим экзаменов " + ("включён." if db.kv_get("exam_mode", False) else "выключен.")


def cmd_mute(db: DB, args: str) -> str:
    parts = args.split()
    if not parts:
        return "Формат: /mute источник [часы]"
    source_id = parts[0]
    hours = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 24
    if source_id not in collect.all_sources(db):
        return f"Источник {source_id} не найден."
    collect.state_of(db, source_id)
    until = util.now_iso() if hours <= 0 else util.after_iso(hours=hours)
    db.execute("UPDATE sources_state SET muted_until = ? WHERE id = ?", (until, source_id))
    return f"{source_id} заглушён на {hours} ч."


def cmd_add(db: DB, args: str) -> str:
    parts = args.split()
    if not parts:
        return "Формат: /add ссылка|@канал|поисковый запрос [A|B|C]"
    channel = "A"
    if parts[-1].upper() in ("A", "B", "C"):
        channel = parts[-1].upper()
        parts = parts[:-1]
    target = " ".join(parts).strip()
    if not target:
        return "Формат: /add ссылка|@канал|поисковый запрос [A|B|C]"

    from .collect import gnews as gnews_mod
    from .collect import rss as rss_mod
    from .collect import tgweb as tgweb_mod

    if target.startswith("@") or "t.me/" in target:
        handle = target.split("t.me/")[-1].lstrip("@").strip("/")
        source_id = f"tg_{handle}"
        src = Source(id=source_id, type="telegram_web", channel=channel, handle=handle,
                     weight=0.6, publisher=f"@{handle}")
        result = tgweb_mod.fetch(src)
    elif target.startswith("http"):
        source_id = "rss_" + util.normalize(target).split()[-1][:24]
        src = Source(id=source_id, type="rss", channel=channel, url=target, weight=0.7)
        result = rss_mod.fetch(src)
    else:
        source_id = "gnews_" + "_".join(util.normalize(target).split())[:28]
        src = Source(id=source_id, type="gnews", channel=channel, query=target, weight=0.6)
        result = gnews_mod.fetch(src)

    if result.error or not result.posts:
        return f"Не получилось: {result.error or 'источник ничего не отдал'}"

    payload = {k: v for k, v in src.__dict__.items() if v not in (None, [], {})}
    db.execute(
        "INSERT INTO user_sources (id, payload, created_at) VALUES (?,?,?) "
        "ON CONFLICT (id) DO UPDATE SET payload = excluded.payload",
        (src.id, json.dumps(payload, ensure_ascii=False), util.now_iso()),
    )
    collect.state_of(db, src.id)
    return f"Добавлен {src.id} ({src.type}) в канал {channel}: {len(result.posts)} записей."


def cmd_check_sources(db: DB) -> str:
    report = collect.check_sources(db)
    ok = [r for r in report if r["ok"]]
    bad = [r for r in report if not r["ok"]]
    lines = [f"<b>Проверка источников</b>: живых {len(ok)} из {len(report)}"]
    if bad:
        lines.append("\n<b>Не отвечают или пусты</b>")
        for row in bad:
            lines.append(f"✕ {util.esc(row['id'])} — {util.esc(str(row['error'])[:90])}")
    lines.append("\n<b>Работают</b>")
    lines.append(", ".join(util.esc(r["id"]) for r in ok) or "—")
    return "\n".join(lines)


# --- диспетчер -------------------------------------------------------------

def handle_command(db: DB, llm: LLM, text: str) -> str:
    command, _, args = text.partition(" ")
    command = command.split("@")[0].lower()

    if command in ("/start", "/help"):
        return HELP
    if command == "/digest":
        channel = (args.strip().upper() or "A")[:1]
        if channel not in ("A", "B", "C"):
            return "Формат: /digest A|B|C"
        result = publish.publish_digest(db, llm, channel, slot="manual", force=True)
        if result.get("published"):
            return f"Дайджест {channel}: {result['published']} материалов."
        return f"Дайджест {channel} не вышел: {result.get('reason')}"
    if command == "/queue":
        return cmd_queue(db)
    if command == "/focus":
        return cmd_focus(db, args)
    if command == "/quiet":
        return cmd_quiet(db, args)
    if command == "/exam_mode":
        return cmd_exam_mode(db, args)
    if command == "/sources":
        return cmd_sources(db)
    if command == "/add":
        return cmd_add(db, args)
    if command == "/mute":
        return cmd_mute(db, args)
    if command == "/stats":
        return cmd_stats(db)
    if command == "/check_sources":
        return cmd_check_sources(db)
    return "Не знаю такой команды. /help"


def handle_callback(db: DB, callback: dict) -> None:
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    parts = data.split(":")
    action = parts[0]

    if action == "p" and len(parts) >= 3:
        pub = db.one("SELECT id FROM publications WHERE message_id = ?", (message_id,))
        telegram.edit_markup(chat_id, message_id,
                             publish.vote_keyboard(int(parts[1]), int(parts[2]),
                                                   back=str(pub["id"]) if pub else None))
        telegram.answer_callback(callback["id"], f"Пункт {parts[2]}")
        return

    if action in ("u", "d") and len(parts) >= 2:
        note = publish.register_vote(db, int(parts[1]), 1 if action == "u" else -1)
        pub = db.one("SELECT cluster_ids FROM publications WHERE message_id = ?", (message_id,))
        cluster_ids = loads(pub.get("cluster_ids"), []) if pub else []
        if len(cluster_ids) > 1:
            telegram.edit_markup(chat_id, message_id, publish.digest_keyboard(cluster_ids))
        telegram.answer_callback(callback["id"], note)
        return

    if action == "b" and len(parts) >= 2:
        pub = db.one("SELECT cluster_ids FROM publications WHERE id = ?", (int(parts[1]),))
        cluster_ids = loads(pub.get("cluster_ids"), []) if pub else []
        telegram.edit_markup(chat_id, message_id, publish.digest_keyboard(cluster_ids))
        telegram.answer_callback(callback["id"])
        return

    telegram.answer_callback(callback["id"])


def poll(db: DB, llm: LLM) -> dict:
    """Забирает накопившиеся апдейты. Вызывается каждым тиком конвейера."""
    offset = db.kv_get("tg_offset", 0)
    try:
        updates = telegram.get_updates(offset=offset or None)
    except telegram.TelegramError as exc:
        log.warning("getUpdates: %s", exc)
        return {"updates": 0}

    handled = 0
    last_id = offset
    for update in updates:
        last_id = max(last_id, int(update["update_id"]) + 1)
        try:
            if "callback_query" in update:
                handle_callback(db, update["callback_query"])
                handled += 1
            elif "message" in update:
                message = update["message"]
                chat_id = (message.get("chat") or {}).get("id")
                text = (message.get("text") or "").strip()
                if not text or not text.startswith("/"):
                    continue
                if not _is_owner(chat_id):
                    telegram.send_message(chat_id, "Это личный бот.")
                    continue
                reply = handle_command(db, llm, text)
                telegram.send_message(chat_id, reply)
                handled += 1
        except Exception as exc:  # noqa: BLE001 — один кривой апдейт не рушит тик
            log.warning("апдейт %s не обработан: %s", update.get("update_id"), exc)

    if last_id != offset:
        db.kv_set("tg_offset", last_id)
    return {"updates": handled}
