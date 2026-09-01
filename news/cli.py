"""Точка входа: python -m news.cli <команда>."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from . import collect, config as cfg, french, pipeline, publish, reports, telegram, util
from .db import get_db
from .llm import LLM


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def print_check_report(report: list[dict]) -> None:
    ok = [r for r in report if r["ok"]]
    bad = [r for r in report if not r["ok"]]
    print(f"\nЖивых источников: {len(ok)} из {len(report)}\n")
    for row in sorted(report, key=lambda r: (r["channel"], r["id"])):
        mark = "OK " if row["ok"] else "BAD"
        detail = f"{row['fresh']} свежих из {row['items']}" if row["ok"] else (row["error"] or "")
        print(f"[{mark}] {row['channel']} {row['id']:<24} {detail}")
    if bad:
        print("\nПроверить вручную:", ", ".join(r["id"] for r in bad))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="news", description="Персональный новостной агрегатор")
    parser.add_argument("command", choices=[
        "tick", "serve", "collect", "dedup", "score", "digest", "urgent", "french",
        "check-sources", "check-channels", "weekly", "filter-report", "stats", "migrate",
    ])
    parser.add_argument("--interval", type=int, default=300,
                        help="пауза между тиками в режиме serve, секунды")
    parser.add_argument("--minutes", type=int, default=330,
                        help="сколько минут работает смена в режиме serve")
    parser.add_argument("--channel", default="A", choices=["A", "B", "C"])
    parser.add_argument("--force", action="store_true", help="игнорировать интервалы и лимиты")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    db = get_db()
    llm = LLM(db)

    if args.command == "migrate":
        print(f"Схема готова: {db.dialect}")
        return 0

    if args.command == "tick":
        stats = pipeline.tick(db, llm)
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "serve":
        # запуск по расписанию GitHub приходит нерегулярно, поэтому один прогон
        # работает сменой: тикает сам, пока не выйдет отведённое время
        deadline = time.monotonic() + args.minutes * 60
        ticks = 0
        while time.monotonic() < deadline:
            started = time.monotonic()
            try:
                stats = pipeline.tick(db, LLM(db))
                ticks += 1
                logging.getLogger("serve").info(
                    "тик %s: %s", ticks,
                    json.dumps(stats.get("timings", {}), ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 — смена не должна обрываться
                logging.getLogger("serve").exception("тик упал: %s", exc)
            pause = max(30.0, args.interval - (time.monotonic() - started))
            if time.monotonic() + pause > deadline:
                break
            time.sleep(pause)
        print(json.dumps({"ticks": ticks}, ensure_ascii=False))
        return 0

    if args.command == "check-channels":
        for channel in ("A", "B", "C"):
            chat_id = cfg.channel_chat_id(channel)
            if not chat_id:
                print(f"{channel}: не задан TG_CHANNEL_{channel}")
                continue
            print(f"{channel} ({chat_id}): {telegram.chat_title(chat_id)}")
        owner = cfg.owner_chat_id()
        print(f"владелец ({owner}): {telegram.chat_title(owner) if owner else 'не задан'}")
        return 0

    if args.command == "collect":
        print(json.dumps(collect.collect_all(db, force=args.force), ensure_ascii=False,
                         indent=2, default=str))
        return 0

    if args.command == "dedup":
        from . import dedup as dedup_mod
        print(json.dumps(dedup_mod.run(db, llm), ensure_ascii=False, indent=2))
        return 0

    if args.command == "score":
        from . import score as score_mod
        print(json.dumps(score_mod.run(db, llm), ensure_ascii=False, indent=2))
        return 0

    if args.command == "digest":
        result = publish.publish_digest(db, llm, args.channel, slot="manual", force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "urgent":
        print(json.dumps(publish.publish_urgent(db, llm), ensure_ascii=False, indent=2))
        return 0

    if args.command == "french":
        print(json.dumps(french.publish(db, llm), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "check-sources":
        report = collect.check_sources(db)
        print_check_report(report)
        bad = [r for r in report if not r["ok"]]
        if bad and args.force:
            telegram.notify_owner(
                "<b>Проверка источников</b>\nне отвечают: "
                + util.esc(", ".join(r["id"] for r in bad)), silent=True)
        return 0

    if args.command == "weekly":
        print(json.dumps(reports.weekly_review(db, llm), ensure_ascii=False, indent=2))
        return 0

    if args.command == "filter-report":
        print(json.dumps(reports.filter_report(db), ensure_ascii=False, indent=2))
        return 0

    if args.command == "stats":
        from .commands import cmd_stats
        print(cmd_stats(db).replace("<b>", "").replace("</b>", "")
              .replace("<i>", "").replace("</i>", ""))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
