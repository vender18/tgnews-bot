"""Самопроверка конвейера на синтетических данных: без сети и без токенов.

Запуск:  .venv/bin/python scripts/selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="newsbot-selftest-"))
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["DRY_RUN"] = "1"
os.environ["LLM_PROVIDER"] = "none"
os.environ["TG_CHANNEL_A"] = "-1000000000001"
os.environ["TG_CHANNEL_B"] = "-1000000000002"
os.environ["TG_CHANNEL_C"] = "-1000000000003"
os.environ["TG_OWNER_ID"] = "424242"
os.environ["TG_BOT_TOKEN"] = "test:token"

from news import commands, dedup, filters, french, publish, score, util  # noqa: E402
from news.config import Source, config, sources  # noqa: E402
from news.db import DB, dumps, loads  # noqa: E402
from news.llm import LLM, parse_json  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append(f"{name} — {detail}" if detail else name)


class FakeLLM(LLM):
    """Модель-заглушка: отвечает заранее заданным JSON, расход не считается."""

    def __init__(self, db: DB, answers: dict[str, object]) -> None:
        super().__init__(db)
        self.answers = answers
        self.calls: list[str] = []
        self.enabled = True

    @property
    def available(self) -> bool:  # type: ignore[override]
        return True

    def ask(self, prompt, *, system=None, smart=False, purpose="", max_tokens=None,
            effort="low"):
        self.calls.append(purpose)
        answer = self.answers.get(purpose)
        return None if answer is None else dumps(answer)


def make_post(db: DB, source_id: str, title: str, text: str, *, channel: str = "A",
              hours_ago: float = 1, geo: list[str] | None = None, external: str | None = None):
    blob = f"{title}\n{text}"
    published = util.iso(util.now_utc() - timedelta(hours=hours_ago))
    db.execute(
        """INSERT INTO posts (source_id, external_id, title, text, url, publisher, lang,
                              published_at, fetched_at, simhash, entities, channel, geo, dropped)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (source_id, external or f"{source_id}-{title[:20]}", title, text,
         f"https://example.com/{abs(hash(title)) % 10000}",
         (sources().get(source_id).publisher if sources().get(source_id) else source_id), "ru",
         published, util.now_iso(), str(util.simhash(blob)), dumps(sorted(util.entities(blob))),
         channel, dumps(geo or [])),
    )


# --- 1. утилиты ------------------------------------------------------------

def test_util() -> None:
    check("translit склеивает языки",
          util.entity_key("Месси") == util.entity_key("Messi"),
          f"{util.entity_key('Месси')} != {util.entity_key('Messi')}")
    a = util.simhash("ЦБ повысил ключевую ставку до 18 процентов годовых сегодня")
    b = util.simhash("Банк России повысил ключевую ставку до 18 процентов годовых")
    c = util.simhash("В Краснодаре открыли новый фонтан в парке Галицкого летом")
    check("simhash различает темы", util.hamming(a, c) > util.hamming(a, b),
          f"{util.hamming(a, b)} vs {util.hamming(a, c)}")

    msk = util.now_msk().replace(hour=2, minute=0)
    check("тихие часы ночью", util.in_quiet_hours(msk, "23:00", "07:30"))
    check("тихие часы днём выключены",
          not util.in_quiet_hours(msk.replace(hour=14), "23:00", "07:30"))

    check("HTML экранируется", util.esc("<b>&x</b>") == "&lt;b&gt;&amp;x&lt;/b&gt;")
    check("boilerplate чистится",
          util.strip_boilerplate('31.08.2026 17:47. Агентство "Москва". Поезд пошёл')
          == "Поезд пошёл",
          util.strip_boilerplate('31.08.2026 17:47. Агентство "Москва". Поезд пошёл'))
    check("промо-хвост убирается",
          "подписывайтесь" not in util.strip_promo("Новость дня\nПодписывайтесь на наш канал").lower())


def test_parse_json() -> None:
    check("JSON в ```-обёртке", parse_json('```json\n[{"i":1}]\n```') == [{"i": 1}])
    check("JSON с текстом вокруг",
          parse_json('Вот ответ: [{"i": 2, "score": 70}] — готово') == [{"i": 2, "score": 70}])
    check("мусор даёт None", parse_json("совсем не json") is None)


# --- 2. фильтры ------------------------------------------------------------

def test_filters() -> None:
    b_source = Source(id="test_b", type="rss", channel="B", geo=["krasnodar"])
    keep, reason, _ = filters.classify(
        b_source, "В Краснодаре перекрыли улицу Красную из-за аварии на водопроводе",
        "Движение ограничено до вечера, объезд по улице Северной, подробности у мэрии города")
    check("канал B пропускает транспорт", keep, str(reason))

    keep, reason, _ = filters.classify(
        b_source, "В парке Краснодара открыли новый фонтан",
        "Городской фонтан заработал после реконструкции, теперь он подсвечивается вечером")
    check("канал B режет бытовой шум", not keep, "пропустил фонтан")

    a_source = Source(id="test_a", type="rss", channel="A")
    keep, _, _ = filters.classify(a_source, "Гороскоп на сентябрь для всех знаков зодиака",
                                  "Астрологи рассказали, что ждёт каждый знак зодиака этой осенью")
    check("шум отсеивается", not keep)

    check("hard-триггер ловит ставку",
          bool(filters.hard_triggers("ЦБ сохранил ключевую ставку на уровне 17%")))
    check("гео определяется по тексту",
          "moscow" in filters.detect_geo("В Москве закрыли станцию метро", a_source))


# --- 3. дедупликация -------------------------------------------------------

def test_dedup(db: DB) -> None:
    db.execute("DELETE FROM posts")
    db.execute("DELETE FROM clusters")
    make_post(db, "interfax", "ЦБ повысил ключевую ставку до 18% годовых",
              "Совет директоров Банка России повысил ключевую ставку на 100 базисных пунктов, "
              "до 18 процентов годовых, говорится в сообщении регулятора")
    make_post(db, "tass", "Банк России поднял ключевую ставку до 18 процентов",
              "Регулятор повысил ключевую ставку до 18% годовых на заседании совета директоров, "
              "сообщила пресс-служба Банка России")
    make_post(db, "bbc_world", "Russia's central bank raises key rate to 18%",
              "The Bank of Russia raised its key interest rate to 18 percent, the regulator said")
    make_post(db, "lenta", "В Сочи открылся новый терминал аэропорта",
              "Пассажирский терминал аэропорта Сочи принял первых пассажиров после реконструкции")

    llm = FakeLLM(db, {"dedup": [{"i": i, "same": True} for i in range(1, 11)]})
    dedup.run(db, llm)
    rows = db.query("SELECT id, source_count, title FROM clusters ORDER BY source_count DESC")
    check("одно событие — один кластер", rows and int(rows[0]["source_count"]) >= 3,
          f"{[dict(r) for r in rows]}")
    check("разные события не слиплись", len(rows) >= 2, f"кластеров: {len(rows)}")
    check("иностранный источник даёт две оптики",
          int(db.scalar("SELECT MAX(divergence) AS d FROM clusters", (), 0) or 0) == 1)

    posts = db.query("SELECT * FROM posts WHERE cluster_id = ?", (rows[0]["id"],))
    count, names = dedup.independent_sources(posts)
    check("независимые источники считаются", count >= 3, f"{count}: {names}")


def test_gnews_not_double_counted(db: DB) -> None:
    db.execute("DELETE FROM posts")
    db.execute("DELETE FROM clusters")
    make_post(db, "rbc", "Минфин разместил ОФЗ на 100 млрд рублей",
              "Министерство финансов провело аукцион по размещению облигаций федерального займа")
    db.execute(
        """INSERT INTO posts (source_id, external_id, title, text, url, publisher, lang,
                              published_at, fetched_at, simhash, entities, channel, geo, dropped)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        ("gnews_krasnodar", "gn-1", "Минфин разместил ОФЗ на 100 млрд рублей",
         "Министерство финансов провело аукцион по размещению облигаций федерального займа",
         "https://news.google.com/x", "РБК", "ru", util.now_iso(), util.now_iso(),
         str(util.simhash("Минфин разместил ОФЗ на 100 млрд рублей")), "[]", "A", "[]"),
    )
    posts = db.query("SELECT * FROM posts")
    count, names = dedup.independent_sources(posts)
    check("Google News не удваивает издание", count == 1, f"{count}: {names}")


# --- 4. скоринг ------------------------------------------------------------

def test_score(db: DB) -> None:
    db.execute("DELETE FROM posts")
    db.execute("DELETE FROM clusters")
    make_post(db, "interfax", "ЦБ повысил ключевую ставку до 18% годовых",
              "Банк России повысил ключевую ставку, инфляция ускорилась до 9 процентов")
    make_post(db, "lenta", "Актёр рассказал о новом сериале",
              "Известный актёр поделился планами на новый сезон популярного сериала")
    llm = FakeLLM(db, {})
    dedup.run(db, llm)
    score.run(db, llm)
    rows = {r["title"]: float(r["score"] or 0) for r in db.query("SELECT title, score FROM clusters")}
    hard = [v for k, v in rows.items() if "ставку" in k]
    soft = [v for k, v in rows.items() if "сериал" in k]
    check("hard-триггер поднимает оценку", hard and soft and hard[0] > soft[0] + 15,
          f"{rows}")

    llm2 = FakeLLM(db, {"score": [{"i": i, "score": 90, "why": "важно"} for i in range(1, 5)]})
    score.run(db, llm2)
    scored = db.query("SELECT llm_score FROM clusters WHERE llm_score IS NOT NULL")
    check("оценка модели применяется", len(scored) >= 1, f"{scored}")


# --- 5. публикация ---------------------------------------------------------

def test_publish(db: DB) -> None:
    db.execute("DELETE FROM posts")
    db.execute("DELETE FROM clusters")
    db.execute("DELETE FROM publications")
    make_post(db, "interfax", "ЦБ повысил ключевую ставку до 18% годовых",
              "Банк России повысил ключевую ставку до 18 процентов годовых на заседании")
    make_post(db, "tass", "Банк России поднял ставку до 18%",
              "Регулятор поднял ключевую ставку, решение вступает в силу в понедельник")
    llm = FakeLLM(db, {
        "dedup": [{"i": 1, "same": True}],
        "summary": [{"i": 1, "headline": "ЦБ поднял ставку до 18%",
                     "summary": "Банк России повысил ключевую ставку на 1 пункт."}],
    })
    dedup.run(db, llm)
    score.run(db, llm)
    result = publish.publish_digest(db, llm, "A", slot="test", force=True)
    check("дайджест публикуется", result.get("published", 0) >= 1, str(result))
    check("кластер помечен опубликованным",
          int(db.scalar("SELECT COUNT(*) AS c FROM clusters WHERE status = 'published'", (), 0)) >= 1)
    check("публикация записана",
          int(db.scalar("SELECT COUNT(*) AS c FROM publications", (), 0)) == 1)

    second = publish.publish_digest(db, llm, "A", slot="test2", force=True)
    check("пустой дайджест не публикуется", second.get("published", 0) == 0, str(second))

    cluster_id = int(db.scalar("SELECT id FROM clusters LIMIT 1", (), 0))
    weight_before = db.one("SELECT weight FROM sources_state WHERE id = 'interfax'")
    publish.register_vote(db, cluster_id, 1)
    weight_after = db.one("SELECT weight FROM sources_state WHERE id = 'interfax'")
    check("👍 поднимает вес источника",
          weight_after and float(weight_after["weight"]) > (
              float(weight_before["weight"]) if weight_before and weight_before["weight"] else 1.0) - 0.001,
          f"{weight_before} -> {weight_after}")

    keyboard = publish.digest_keyboard([1, 2, 3])
    check("клавиатура дайджеста нумерует пункты",
          keyboard and keyboard[0][0]["callback_data"].startswith("p:1"))

    text = publish.format_item(db, 1, {"divergence": 1}, [], "Заголовок <b>",
                               "Заголовок")
    check("заголовок не дублируется в сути", text.count("Заголовок") == 1, text)
    check("значок двух оптик проставлен", text.startswith("⚖️"), text)


def test_quiet_hours(db: DB) -> None:
    db.kv_set("quiet_hours", {"start": "00:00", "end": "23:59", "disabled": False})
    check("тихие часы включаются", publish.quiet_now(db))
    db.execute("UPDATE clusters SET status = 'scored', published_at = NULL, score = 95, "
               "source_count = 2, channel = 'A'")
    llm = FakeLLM(db, {})
    stats = publish.publish_stream(db, llm, "A")
    queued = int(db.scalar("SELECT COUNT(*) AS c FROM clusters WHERE status = 'queued'", (), 0))
    check("ночью лента копит, а не публикует",
          stats.get("published", 0) == 0 and queued >= 1, f"{stats}, в очереди {queued}")
    db.kv_set("quiet_hours", {"start": "23:00", "end": "07:30", "disabled": True})
    check("тихие часы выключаются", not publish.quiet_now(db))


def test_limits(db: DB) -> None:
    db.execute("DELETE FROM publications")
    for _ in range(config()["limits"]["max_per_day"]["A"] + 1):
        db.execute(
            """INSERT INTO clusters (channel, created_at, updated_at, status, published_at, score)
               VALUES ('A', ?, ?, 'published', ?, 90)""",
            (util.now_iso(), util.now_iso(), util.now_iso()),
        )
    llm = FakeLLM(db, {})
    result = publish.publish_digest(db, llm, "A", slot="over")
    check("дневной лимит канала соблюдается", result.get("published", 0) == 0, str(result))


# --- 6. команды ------------------------------------------------------------

def test_commands(db: DB) -> None:
    llm = FakeLLM(db, {})
    check("/help отвечает", "/digest" in commands.handle_command(db, llm, "/help"))
    commands.handle_command(db, llm, "/focus ЕГЭ французский")
    check("/focus запоминает тему", "егэ французский" in (db.kv_get("focus") or []))
    commands.handle_command(db, llm, "/focus off")
    check("/focus off очищает", not db.kv_get("focus"))
    commands.handle_command(db, llm, "/exam_mode on")
    check("/exam_mode включается", db.kv_get("exam_mode") is True)
    check("порог в режиме экзаменов выше",
          publish.threshold(db, "A") > config()["score"]["publish_threshold"]["A"])
    commands.handle_command(db, llm, "/exam_mode off")
    reply = commands.handle_command(db, llm, "/quiet 22:00-08:00")
    check("/quiet принимает интервал", "22:00" in reply, reply)
    commands.handle_command(db, llm, "/quiet off")
    check("/sources показывает каналы", "Канал A" in commands.cmd_sources(db))
    check("/stats считает сутки", "Сутки" in commands.cmd_stats(db))
    check("неизвестная команда не падает", "/help" in commands.handle_command(db, llm, "/nope"))


# --- 7. французская рубрика ------------------------------------------------

def test_french(db: DB) -> None:
    db.execute("DELETE FROM posts")
    body = ("Le gouvernement a annoncé une réforme des retraites. "
            "Les syndicats appellent à la grève dès lundi prochain. "
            "Le texte sera examiné par l'Assemblée nationale en septembre.")
    make_post(db, "le_monde", "Réforme des retraites: le gouvernement avance", body, channel="C")
    llm = FakeLLM(db, {"french": {
        "sentences": ["Le gouvernement a annoncé une réforme des retraites.",
                      "Les syndicats appellent à la grève dès lundi prochain."],
        "words": [{"fr": "la grève", "ru": "забастовка"}, {"fr": "le texte", "ru": "законопроект"}],
    }})
    built = french.build_post(db, llm)
    check("французский пост собирается", built is not None)
    if built:
        text = built[0]
        check("текст остался французским", "réforme des retraites" in text, text[:120])
        check("словарь добавлен", "забастовка" in text, text[:200])
        check("нет перевода вместо оригинала", "правительство" not in text.lower())


# --- 8. расписание ---------------------------------------------------------

def test_schedule(db: DB) -> None:
    from news import pipeline

    original = util.now_msk
    fixed = original().replace(hour=20, minute=5, second=0, microsecond=0)
    util.now_msk = lambda: fixed  # type: ignore[assignment]
    try:
        due = {kind for kind, _key in pipeline._due_slots(db, fixed)}
        check("слот канала B наступил", "B" in due, str(due))
        llm = FakeLLM(db, {})
        pipeline.run_schedule(db, llm)
        due_after = {kind for kind, _key in pipeline._due_slots(db, fixed)}
        check("отработанный слот не повторяется", "B" not in due_after, str(due_after))

        late = fixed.replace(hour=23, minute=50)
        due_late = {kind for kind, _key in pipeline._due_slots(db, late)}
        check("просроченный слот не догоняется через три часа", "C" not in due_late, str(due_late))
    finally:
        util.now_msk = original  # type: ignore[assignment]

    check("очистка старых данных проходит", pipeline.retention(db).get("cleaned") is True)


def test_stream(db: DB) -> None:
    """Лента канала A: событие уходит отдельным постом, как только дозрело."""
    db.execute("DELETE FROM posts")
    db.execute("DELETE FROM clusters")
    db.execute("DELETE FROM publications")
    db.kv_set("quiet_hours", {"start": "23:00", "end": "07:30", "disabled": True})

    # подтверждённое событие часовой давности
    make_post(db, "interfax", "ЦБ поднял ключевую ставку до 18% годовых",
              "Банк России повысил ключевую ставку до 18 процентов годовых", hours_ago=1)
    make_post(db, "tass", "Банк России поднял ставку до 18 процентов",
              "Регулятор поднял ключевую ставку, решение вступает в силу в понедельник",
              hours_ago=1)
    # одинокая заметка слабого источника — в ленту не должна попасть
    make_post(db, "lenta", "Блогер рассказал о новом сериале",
              "Известный блогер поделился впечатлениями от нового сериала стриминга",
              hours_ago=1)
    # свежее событие, ещё не дозревшее
    make_post(db, "interfax", "Совет директоров Аэрофлота обсудит дивиденды",
              "Заседание совета директоров назначено на следующей неделе", hours_ago=0.01)

    llm = FakeLLM(db, {
        "summary": [{"i": i, "headline": f"Заголовок {i}", "summary": "Суть события."}
                    for i in range(1, 9)],
    })
    dedup.run(db, llm)
    score.run(db, llm)
    result = publish.publish_stream(db, llm, "A")
    check("лента публикует дозревшее событие", result.get("published", 0) >= 1, str(result))

    kinds = db.query("SELECT kind, cluster_ids FROM publications")
    check("в ленте один пост на событие",
          all(k["kind"] == "stream" and len(loads(k["cluster_ids"], [])) == 1 for k in kinds),
          str(kinds))

    titles = [r["title"] or "" for r in db.query(
        "SELECT title FROM clusters WHERE status = 'published'")]
    check("одинокая заметка слабого источника в ленту не идёт",
          not any("блогер" in t.lower() for t in titles), str(titles))
    check("сырое событие ждёт подтверждения",
          not any("Аэрофлот" in t for t in titles), str(titles))

    again = publish.publish_stream(db, llm, "A")
    check("опубликованное не повторяется", again.get("published", 0) == 0, str(again))


def main() -> int:
    db = DB()
    test_util()
    test_parse_json()
    test_filters()
    test_dedup(db)
    test_gnews_not_double_counted(db)
    test_score(db)
    test_publish(db)
    test_quiet_hours(db)
    test_limits(db)
    test_commands(db)
    test_french(db)
    test_stream(db)
    test_schedule(db)

    print(f"\nпройдено: {len(PASSED)}")
    for name in PASSED:
        print(f"  ✓ {name}")
    if FAILED:
        print(f"\nпровалено: {len(FAILED)}")
        for name in FAILED:
            print(f"  ✗ {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
