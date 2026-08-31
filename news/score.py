"""Скоринг кластеров: дешёвая формула для всего потока, LLM — только для верхушки."""
from __future__ import annotations

import logging
from datetime import timedelta

from . import filters, util
from .collect import effective_weight
from .config import config, source as get_source
from .db import DB, chunks
from .dedup import cluster_posts, independent_sources
from .llm import LLM

log = logging.getLogger("score")


def _focus_terms(db: DB) -> list[str]:
    return [t.lower() for t in (db.kv_get("focus", []) or [])]


def base_score(db: DB, cluster: dict, posts: list[dict], focus: list[str]) -> tuple[float, dict]:
    conf = config()["score"]
    w = conf["weights"]
    parts: dict[str, float] = {}

    parts["base"] = float(conf.get("base_offset", {}).get(cluster["channel"], 0))

    count, _ = independent_sources(posts)
    parts["sources"] = min(count - 1, w["max_extra_sources"]) * w["per_extra_source"]

    weights = [effective_weight(db, get_source(p["source_id"]))
               for p in posts if get_source(p["source_id"])]
    parts["weight"] = (max(weights) if weights else 0.5) * w["source_weight_factor"]

    blob = " ".join(filter(None, [cluster.get("title") or ""] +
                           [p.get("title") or "" for p in posts] +
                           [util.shorten(p.get("text") or "", 400) for p in posts[:3]]))
    parts["hard"] = w["hard_trigger"] if filters.hard_triggers(blob) else 0.0
    parts["noise"] = -w["noise_penalty"] if filters.noise(blob) else 0.0

    low = blob.lower()
    parts["focus"] = w["focus_bonus"] if any(term in low for term in focus) else 0.0

    published = util.parse_dt(cluster.get("first_seen_at")) or util.now_utc()
    age_hours = (util.now_utc() - published).total_seconds() / 3600
    if age_hours <= 2:
        parts["fresh"] = w["freshness"]
    elif age_hours <= 6:
        parts["fresh"] = w["freshness"] / 2
    elif age_hours >= 18:
        parts["fresh"] = -w["freshness"] / 2
    else:
        parts["fresh"] = 0.0

    if cluster["channel"] == "B":
        geo_hit = any(p.get("geo") not in (None, "", "[]") for p in posts)
        if not geo_hit:
            parts["geo"] = -w["geo_mismatch_penalty"]
        elif filters.geo_relevant(blob):
            parts["geo"] = 8.0
        else:
            parts["geo"] = 0.0

    if cluster.get("divergence"):
        parts["divergence"] = 5.0

    if db.kv_get("exam_mode", False) and cluster["channel"] == "C":
        tags = {tag for p in posts for tag in ((get_source(p["source_id"]).tags or [])
                                               if get_source(p["source_id"]) else [])}
        if tags & {"education", "science", "history", "fr"}:
            parts["exam"] = 8.0

    total = max(0.0, min(100.0, sum(parts.values())))
    return total, parts


PERSONA_FALLBACK = (
    "Читатель — старшеклассник из России, готовится к поступлению на экономику, "
    "живёт между Москвой и Краснодаром, учит французский."
)


def _llm_scores(llm: LLM, clusters: list[dict], db: DB) -> dict[int, tuple[float, str]]:
    if not llm.available or not clusters:
        return {}
    persona = config().get("persona") or PERSONA_FALLBACK
    blocks = []
    for index, cluster in enumerate(clusters, start=1):
        posts = cluster_posts(db, cluster["id"])
        _, names = independent_sources(posts)
        text = util.shorten(posts[0].get("text") or "", 350) if posts else ""
        blocks.append(
            f"{index}. [{cluster['channel']}] {cluster.get('title') or ''}\n"
            f"источники: {', '.join(names[:6])}\n{text}"
        )
    prompt = (
        f"{persona}\n\n"
        "Оцени, насколько каждая новость важна лично для этого читателя, по шкале 0–100.\n"
        "100 — меняет его планы или картину мира; 60 — полезно знать; "
        "20 — фон; 0 — мусор, шум, реклама, светская хроника.\n"
        "Учитывай: событие уровня страны и экономики важнее происшествия; "
        "локальная новость важна, только если касается транспорта, служб, погоды или ЧП; "
        "материал в развивающую полосу важен, если объясняет механизм, а не просто сообщает факт.\n\n"
        + "\n\n".join(blocks)
        + "\n\nОтветь только JSON-массивом: "
          '[{"i": 1, "score": 72, "why": "одна короткая фраза"}]'
    )
    data = llm.ask_json(prompt, purpose="score", max_tokens=1200)
    result: dict[int, tuple[float, str]] = {}
    if isinstance(data, list):
        for item in data:
            try:
                index = int(item["i"]) - 1
                if 0 <= index < len(clusters):
                    result[int(clusters[index]["id"])] = (
                        max(0.0, min(100.0, float(item["score"]))),
                        str(item.get("why") or "")[:200],
                    )
            except (KeyError, TypeError, ValueError):
                continue
    return result


def run(db: DB, llm: LLM) -> dict:
    conf = config()["score"]
    since = util.iso(util.now_utc() - timedelta(hours=30))
    clusters = db.query(
        """SELECT * FROM clusters
           WHERE status IN ('new', 'scored', 'queued') AND updated_at >= ?
           ORDER BY updated_at DESC LIMIT 800""",
        (since,),
    )
    focus = _focus_terms(db)
    stats = {"scored": len(clusters), "llm": 0}
    need_llm: list[dict] = []

    for cluster in clusters:
        posts = cluster_posts(db, cluster["id"])
        if not posts:
            continue
        base, _parts = base_score(db, cluster, posts, focus)
        if cluster.get("llm_score") is None:
            final = base
        else:
            final = base * (1 - conf["llm_weight"]) + float(cluster["llm_score"]) * conf["llm_weight"]
        db.execute(
            "UPDATE clusters SET base_score = ?, score = ?, "
            "status = CASE WHEN status = 'new' THEN 'scored' ELSE status END WHERE id = ?",
            (base, final, cluster["id"]),
        )
        if cluster.get("llm_score") is None and base >= conf["llm_threshold"]:
            cluster["base_score"] = base
            need_llm.append(cluster)

    batch_size = int(config()["llm"].get("score_batch_size", 10))
    for batch in chunks(need_llm, batch_size):
        scores = _llm_scores(llm, batch, db)
        if not scores:
            break  # модель недоступна или бюджет исчерпан — работаем на базовой формуле
        stats["llm"] += len(scores)
        for cluster in batch:
            got = scores.get(int(cluster["id"]))
            if not got:
                continue
            llm_score, why = got
            base = float(cluster.get("base_score") or 0)
            mixed = base * (1 - conf["llm_weight"]) + llm_score * conf["llm_weight"]
            db.execute(
                "UPDATE clusters SET llm_score = ?, llm_note = ?, score = ? WHERE id = ?",
                (llm_score, why, mixed, cluster["id"]),
            )
    return stats
