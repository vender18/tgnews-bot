"""Дедупликация и кластеризация: simhash → сущности → LLM для пограничных пар.

Одно событие приходит от Интерфакса, ТАСС, РБК и Ленты почти одновременно —
в канал оно должно уйти одним постом.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from . import util
from .config import config, source as get_source
from .db import DB, dumps, loads
from .llm import LLM

log = logging.getLogger("dedup")


def _norm_publisher(name: str | None) -> str:
    if not name:
        return ""
    text = re.sub(r"[^\w ]+", " ", name.lower().replace("ё", "е"))
    text = re.sub(r"\b(ru|рф|news|новости|online|онлайн|ru\.?)\b", " ", text)
    return " ".join(text.split())


def independent_sources(posts: list[dict]) -> tuple[int, list[str]]:
    """Считает независимые издания.

    Google News не увеличивает счётчик, если издание уже пришло напрямую —
    иначе счётчик врёт и в экстренную полосу лезет что попало.
    """
    direct: dict[str, str] = {}
    via_gnews: dict[str, str] = {}
    for post in posts:
        src = get_source(post["source_id"])
        display = post.get("publisher") or (src.display if src else post["source_id"])
        key = _norm_publisher(display) or post["source_id"]
        if src and src.type == "gnews":
            via_gnews.setdefault(key, display)
        else:
            direct.setdefault(key, display)
    for key in list(via_gnews):
        if key in direct:
            via_gnews.pop(key)
    names = list(direct.values()) + list(via_gnews.values())
    return len(names), names


def _divergence(posts: list[dict]) -> int:
    """0 — обычный кластер, 1 — освещают обе стороны, 2 — только иностранные."""
    foreign = russian = 0
    for post in posts:
        src = get_source(post["source_id"])
        if src and src.foreign:
            foreign += 1
        else:
            russian += 1
    if foreign and russian:
        return 1
    if foreign >= 2 and not russian:
        return 2
    return 0


def cluster_posts(db: DB, cluster_id: int) -> list[dict]:
    return db.query(
        "SELECT * FROM posts WHERE cluster_id = ? ORDER BY published_at ASC", (cluster_id,))


def recompute(db: DB, cluster_id: int) -> dict | None:
    posts = cluster_posts(db, cluster_id)
    if not posts:
        return None
    count, _ = independent_sources(posts)
    ents: set[str] = set()
    for post in posts:
        ents |= set(loads(post.get("entities"), []) or [])
    best = max(posts, key=lambda p: (
        (get_source(p["source_id"]).weight if get_source(p["source_id"]) else 0.5),
        len(p.get("title") or ""),
    ))
    tokens = max(len(util.tokens(f"{p.get('title') or ''} {p.get('text') or ''}")) for p in posts)
    db.execute(
        """UPDATE clusters
           SET source_count = ?, entities = ?, title = ?, updated_at = ?,
               first_seen_at = ?, divergence = ?, tokens = ?
           WHERE id = ?""",
        (count, dumps(sorted(ents)[:120]), (best.get("title") or "")[:500], util.now_iso(),
         min(p["published_at"] for p in posts), _divergence(posts), tokens, cluster_id),
    )
    return db.one("SELECT * FROM clusters WHERE id = ?", (cluster_id,))


class ClusterIndex:
    """Индекс живых кластеров по ключам сущностей.

    Перебирать «последние N кластеров» нельзя: за шесть часов их набегают сотни,
    и нужное событие выпадает из окна. Ищем только тех, с кем есть общие сущности.
    """

    def __init__(self, db: DB, window_hours: int) -> None:
        since = util.iso(util.now_utc() - timedelta(hours=window_hours))
        self.clusters: dict[int, dict] = {}
        self.by_key: dict[str, set[int]] = {}
        self.recent: dict[str, list[int]] = {}
        rows = db.query(
            """SELECT id, simhash, entities, title, channel, tokens FROM clusters
               WHERE updated_at >= ? AND status <> 'dropped'
               ORDER BY updated_at DESC LIMIT 4000""",
            (since,),
        )
        for row in rows:
            self.add(row)

    def add(self, cluster: dict) -> None:
        cluster_id = int(cluster["id"])
        ents = set(loads(cluster.get("entities"), []) or [])
        cluster = dict(cluster)
        cluster["_ents"] = ents
        self.clusters[cluster_id] = cluster
        for key in ents:
            self.by_key.setdefault(key, set()).add(cluster_id)
        channel = cluster.get("channel") or "A"
        self.recent.setdefault(channel, []).insert(0, cluster_id)

    def merge(self, cluster_id: int, ents: set[str]) -> None:
        cluster = self.clusters.get(cluster_id)
        if not cluster:
            return
        cluster["_ents"] |= ents
        for key in ents:
            self.by_key.setdefault(key, set()).add(cluster_id)

    def candidates(self, post_ents: set[str], channel: str, limit: int = 60) -> list[dict]:
        scored: dict[int, int] = {}
        for key in post_ents:
            for cluster_id in self.by_key.get(key, ()):  # общие сущности
                scored[cluster_id] = scored.get(cluster_id, 0) + 1
        ranked = sorted(scored.items(), key=lambda kv: -kv[1])[:limit]
        found = [self.clusters[cid] for cid, _ in ranked
                 if self.clusters.get(cid, {}).get("channel") == channel]
        if not found:
            # у поста нет распознанных сущностей — сверяемся с последними по каналу
            found = [self.clusters[cid] for cid in self.recent.get(channel, [])[:40]]
        return found


def _attach(db: DB, post: dict, cluster_id: int) -> None:
    db.execute("UPDATE posts SET cluster_id = ? WHERE id = ?", (cluster_id, post["id"]))
    db.execute("UPDATE clusters SET status = CASE WHEN status = 'published' THEN status "
               "ELSE 'new' END, updated_at = ? WHERE id = ?", (util.now_iso(), cluster_id))


def _create(db: DB, post: dict) -> int:
    now = util.now_iso()
    tokens = len(util.tokens(f"{post.get('title') or ''} {post.get('text') or ''}"))
    cluster_id = db.insert_returning_id(
        """INSERT INTO clusters (channel, created_at, updated_at, first_seen_at, title,
                                 simhash, entities, tokens, source_count, status)
           VALUES (?,?,?,?,?,?,?,?,1,'new') RETURNING id""",
        (post.get("channel") or "A", now, now, post["published_at"],
         (post.get("title") or "")[:500], post.get("simhash"),
         post.get("entities") or "[]", tokens),
    )
    db.execute("UPDATE posts SET cluster_id = ? WHERE id = ?", (cluster_id, post["id"]))
    return int(cluster_id)


def _llm_same_event(llm: LLM, pairs: list[tuple[dict, dict]]) -> dict[int, bool]:
    """Пограничные пары уходят на проверку батчами по 20."""
    if not llm.available or not pairs:
        return {}
    verdicts: dict[int, bool] = {}
    for start in range(0, len(pairs), 20):
        batch = pairs[start:start + 20]
        for local, same in _llm_same_event_batch(llm, batch).items():
            verdicts[start + local] = same
    return verdicts


def _llm_same_event_batch(llm: LLM, pairs: list[tuple[dict, dict]]) -> dict[int, bool]:
    lines = []
    for index, (post, cluster) in enumerate(pairs, start=1):
        lines.append(
            f"{index}.\nA: {util.shorten(post.get('title') or post.get('text') or '', 200)}\n"
            f"B: {util.shorten(cluster.get('title') or '', 200)}"
        )
    prompt = (
        "Ниже пары новостных сообщений. Для каждой пары определи, описывают ли A и B "
        "ОДНО И ТО ЖЕ событие (тот же факт, те же участники, тот же момент), "
        "или это разные события одной темы.\n\n"
        + "\n\n".join(lines)
        + "\n\nОтветь только JSON-массивом вида "
          '[{"i": 1, "same": true}, {"i": 2, "same": false}] без пояснений.'
    )
    data = llm.ask_json(prompt, purpose="dedup", max_tokens=500)
    result: dict[int, bool] = {}
    if isinstance(data, list):
        for item in data:
            try:
                result[int(item["i"]) - 1] = bool(item["same"])
            except (KeyError, TypeError, ValueError):
                continue
    return result


def run(db: DB, llm: LLM, limit: int = 500) -> dict:
    conf = config()["dedup"]
    posts = db.query(
        """SELECT * FROM posts
           WHERE cluster_id IS NULL AND dropped = 0
           ORDER BY published_at ASC LIMIT ?""",
        (limit,),
    )
    stats = {"posts": len(posts), "merged": 0, "created": 0, "llm_checked": 0,
             "pending": 0}
    pending: list[tuple[dict, dict]] = []
    touched: set[int] = set()
    index = ClusterIndex(db, conf["window_hours"])

    def create(post: dict) -> int:
        cluster_id = _create(db, post)
        index.add({
            "id": cluster_id,
            "simhash": post.get("simhash"),
            "entities": post.get("entities") or "[]",
            "title": post.get("title") or "",
            "channel": post.get("channel") or "A",
            "tokens": len(util.tokens(f"{post.get('title') or ''} {post.get('text') or ''}")),
        })
        return cluster_id

    def attach(post: dict, cluster: dict) -> None:
        _attach(db, post, int(cluster["id"]))
        index.merge(int(cluster["id"]), set(loads(post.get("entities"), []) or []))

    for post in posts:
        post_hash = int(post.get("simhash") or 0)
        post_ents = set(loads(post.get("entities"), []) or [])
        post_tokens = len(util.tokens(f"{post.get('title') or ''} {post.get('text') or ''}"))
        best: tuple[float, dict] | None = None
        maybe: tuple[float, dict] | None = None

        post_title_tokens = set(util.tokens(post.get("title") or ""))

        for cand in index.candidates(post_ents, post.get("channel") or "A"):
            cand_hash = int(cand.get("simhash") or 0)
            cand_ents = cand.get("_ents") or set(loads(cand.get("entities"), []) or [])
            cand_tokens = int(cand.get("tokens") or 0)
            # на коротких текстах simhash сближается сам по себе — доверяем только сущностям
            hash_usable = (post_hash and cand_hash
                           and min(post_tokens, cand_tokens) >= conf["min_tokens_for_hash"])
            distance = util.hamming(post_hash, cand_hash) if hash_usable else 64
            overlap = util.jaccard(post_ents, cand_ents)
            common = len(post_ents & cand_ents)
            title_sim = util.jaccard(post_title_tokens, set(util.tokens(cand.get("title") or "")))

            same = (distance <= conf["near_distance"]
                    or (overlap >= conf["entity_jaccard_same"] and common >= 2)
                    or (title_sim >= conf["title_jaccard_same"] and common >= 1))
            close = (distance <= conf["maybe_distance"]
                     or (overlap >= conf["entity_jaccard_maybe"] and common >= 2)
                     or (title_sim >= conf["title_jaccard_maybe"] and common >= 1)
                     # разноязычные версии одного события: заголовки не совпадают,
                     # но имена и цифры общие — такие пары решает модель
                     or (common >= 2 and overlap >= conf["entity_jaccard_weak"]))
            rank = max(overlap, title_sim) * 100 - distance / 10
            if same:
                if best is None or rank > best[0]:
                    best = (rank, cand)
            elif close:
                if maybe is None or rank > maybe[0]:
                    maybe = (rank, cand)

        if best:
            attach(post, best[1])
            touched.add(int(best[1]["id"]))
            stats["merged"] += 1
        elif maybe and len(pending) < conf["llm_check_max_pairs"]:
            pending.append((post, maybe[1]))
        else:
            touched.add(create(post))
            stats["created"] += 1

    stats["pending"] = len(pending)
    if pending:
        verdicts = _llm_same_event(llm, pending)
        stats["llm_checked"] = len(pending) if verdicts else 0
        fallback_threshold = (conf["entity_jaccard_maybe"] + conf["entity_jaccard_same"]) / 2
        for pair_index, (post, cluster) in enumerate(pending):
            if verdicts:
                same = verdicts.get(pair_index, False)
            else:
                # без модели решаем по пересечению сущностей и заголовков
                ents_close = util.jaccard(
                    set(loads(post.get("entities"), []) or []),
                    set(loads(cluster.get("entities"), []) or []),
                ) >= fallback_threshold
                titles_close = util.jaccard(
                    set(util.tokens(post.get("title") or "")),
                    set(util.tokens(cluster.get("title") or "")),
                ) >= 0.5
                same = ents_close or titles_close
            if same:
                attach(post, cluster)
                touched.add(int(cluster["id"]))
                stats["merged"] += 1
            else:
                touched.add(create(post))
                stats["created"] += 1

    for cluster_id in touched:
        recompute(db, cluster_id)
    return stats
