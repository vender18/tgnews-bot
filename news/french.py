"""Французская рубрика: три предложения оригинала в день.

Смысл — не «читать новости по-французски», а набирать ~1000 живых предложений
за год до экзамена. Поэтому текст остаётся французским, переводится только словарь.
"""
from __future__ import annotations

import logging

from . import config as cfg
from . import extract, telegram, util
from .config import config, sources
from .db import DB
from .llm import LLM

log = logging.getLogger("french")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def french_source_ids() -> list[str]:
    return [s.id for s in sources().values() if s.lang == "fr" or "fr" in (s.tags or [])]


def pick_post(db: DB) -> dict | None:
    ids = french_source_ids()
    if not ids:
        return None
    placeholders = ",".join("?" for _ in ids)
    rows = db.query(
        f"""SELECT * FROM posts
            WHERE source_id IN ({placeholders}) AND dropped = 0 AND published_at >= ?
            ORDER BY published_at DESC LIMIT 40""",
        (*ids, util.ago_iso(hours=30)),
    )
    if not rows:
        return None
    used = set(db.kv_get("french_used", []) or [])
    fresh = [r for r in rows if str(r["id"]) not in used]
    pool = fresh or rows
    # длинный текст полезнее короткой заметки
    return max(pool, key=lambda r: len(r.get("text") or ""))


def build_post(db: DB, llm: LLM) -> tuple[str, str] | None:
    conf = config()["french"]
    post = pick_post(db)
    if not post:
        return None

    url = extract.resolve_url(db, post.get("url") or "")
    body = post.get("text") or ""
    if len(body) < 600:
        full = extract.article_text(url)
        if len(full) > len(body):
            body = full
            db.execute("UPDATE posts SET text = ? WHERE id = ?", (body[:6000], post["id"]))

    title = (post.get("title") or "").strip()
    sentences = util.sentences(body, conf["sentences"])
    words_block = ""

    if llm.available:
        prompt = (
            f"Voici un article de presse en français.\n\nTitre: {title}\n\n"
            f"Texte: {util.shorten(body, 2500)}\n\n"
            f"1) Choisis {conf['sentences']} phrases CONSÉCUTIVES du texte, telles quelles, "
            f"sans les modifier, de niveau {conf['level']}, qui racontent l'essentiel.\n"
            f"2) Choisis {conf['words']} mots ou expressions utiles de ces phrases "
            f"(niveau {conf['level']}, des mots qui reviennent souvent dans la presse) "
            f"et donne leur traduction en russe.\n\n"
            'Réponds uniquement en JSON: {"sentences": ["...", "..."], '
            '"words": [{"fr": "...", "ru": "..."}]}'
        )
        data = llm.ask_json(prompt, purpose="french", smart=True, max_tokens=1200, effort="low")
        if isinstance(data, dict):
            picked = [str(s).strip() for s in (data.get("sentences") or []) if str(s).strip()]
            # модель обязана цитировать, а не пересказывать
            verified = [s for s in picked if s[:40].lower() in body.lower()]
            if verified:
                sentences = verified[: conf["sentences"]]
            pairs = []
            for item in (data.get("words") or [])[: conf["words"]]:
                try:
                    pairs.append(f"<b>{util.esc(item['fr'])}</b> — {util.esc(item['ru'])}")
                except (KeyError, TypeError):
                    continue
            if pairs:
                words_block = "📎 " + " · ".join(pairs)

    if not sentences:
        return None

    now = util.now_msk()
    date = f"{now.day} {MONTHS[now.month - 1]}"
    lines = [f"🇫🇷 <b>Une nouvelle</b> · {date}", ""]
    if title:
        lines.append(f"<b>{util.esc(title)}</b>")
    lines.append(util.esc(" ".join(sentences)))
    if words_block:
        lines += ["", words_block]
    if url:
        lines += ["", f"<a href=\"{util.esc(url)}\">Lire l'original</a>"]

    used = list(db.kv_get("french_used", []) or [])
    used.append(str(post["id"]))
    db.kv_set("french_used", used[-60:])
    return "\n".join(lines), str(post["id"])


def publish(db: DB, llm: LLM) -> dict:
    built = build_post(db, llm)
    if not built:
        return {"published": 0, "reason": "нет свежего французского материала"}
    text, _post_id = built
    chat_id = cfg.channel_chat_id("C")
    if not chat_id:
        return {"published": 0, "reason": "не задан TG_CHANNEL_C"}
    message = telegram.send_message(chat_id, text)
    message_id = message.get("message_id") if message else None
    db.execute(
        """INSERT INTO publications (channel, kind, slot, message_id, cluster_ids, created_at)
           VALUES (?,?,?,?,?,?)""",
        ("C", "french", "french", message_id, "[]", util.now_iso()),
    )
    return {"published": 1, "message_id": message_id}
