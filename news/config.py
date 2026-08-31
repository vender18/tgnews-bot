"""Загрузка конфигов и переменных окружения."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Читает .env, если он есть. В GitHub Actions переменные приходят из secrets."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


@lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    with (ROOT / "config.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass
class Source:
    id: str
    type: str
    channel: str
    weight: float = 0.8
    lang: str = "ru"
    url: str | None = None
    fallback_url: str | None = None
    gnews_fallback: str | None = None
    query: str | None = None
    handle: str | None = None
    publisher: str | None = None
    geo: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    foreign: bool = False
    strict_substance: bool = False
    low_volume: bool = False  # выходит редко: живость проверяется мягче
    selectors: dict[str, str] = field(default_factory=dict)
    note: str | None = None

    @property
    def display(self) -> str:
        return self.publisher or self.id

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Source":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@lru_cache(maxsize=1)
def sources() -> dict[str, Source]:
    with (ROOT / "sources.yaml").open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    result: dict[str, Source] = {}
    for item in raw.get("sources", []):
        src = Source.from_dict(item)
        result[src.id] = src
    return result


def source(source_id: str) -> Source | None:
    return sources().get(source_id)


@lru_cache(maxsize=1)
def compiled_filters() -> dict[str, list[re.Pattern[str]]]:
    filters = config()["filters"]
    out: dict[str, list[re.Pattern[str]]] = {}
    for key in ("hard_triggers", "noise_patterns", "geo_relevant", "substance_drop"):
        out[key] = [re.compile(p, re.IGNORECASE) for p in filters.get(key, [])]
    return out


# --- окружение -------------------------------------------------------------

def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def channel_chat_id(channel: str) -> str | None:
    return env(f"TG_CHANNEL_{channel.upper()}")


def owner_chat_id() -> str | None:
    return env("TG_OWNER_ID")


def database_url() -> str:
    """Postgres (Neon) в проде, SQLite-файл локально."""
    return env("DATABASE_URL") or f"sqlite:///{ROOT / 'data' / 'news.db'}"


def dry_run() -> bool:
    return (env("DRY_RUN", "0") or "0").lower() in ("1", "true", "yes")
