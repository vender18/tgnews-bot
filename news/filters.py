"""Rule-based отсев до всякой LLM: дешёвый способ убрать 60–80% потока."""
from __future__ import annotations

from .config import Source, compiled_filters, config


def _hits(patterns, text: str) -> list[str]:
    return [p.pattern for p in patterns if p.search(text)]


def hard_triggers(text: str) -> list[str]:
    return _hits(compiled_filters()["hard_triggers"], text)


def noise(text: str) -> list[str]:
    return _hits(compiled_filters()["noise_patterns"], text)


def geo_relevant(text: str) -> bool:
    return bool(_hits(compiled_filters()["geo_relevant"], text))


def substance_drop(text: str) -> bool:
    return bool(_hits(compiled_filters()["substance_drop"], text))


def detect_geo(text: str, src: Source) -> list[str]:
    """Гео поста: то, что объявлено у источника, плюс то, что нашлось в тексте."""
    found = set(src.geo or [])
    low = (text or "").lower()
    for geo, keywords in config()["filters"]["geo_keywords"].items():
        if any(word in low for word in keywords):
            found.add(geo)
    return sorted(found)


def classify(src: Source, title: str | None, text: str) -> tuple[bool, str | None, list[str]]:
    """Решение на входе: брать пост или дропнуть. Возвращает (keep, причина, гео)."""
    blob = f"{title or ''}\n{text or ''}".strip()
    geo = detect_geo(blob, src)

    if len(blob) < 40:
        return False, "пустой текст", geo
    if noise(blob):
        return False, "шум", geo

    if src.channel == "B":
        # локальные новости на 90% состоят из бытового шума — порог выше
        if not geo:
            return False, "нет привязки к Москве или Краснодару", geo
        if not geo_relevant(blob):
            return False, "локальная новость, которая ничего не меняет", geo

    if src.channel == "C" and src.strict_substance and substance_drop(blob):
        return False, "заметка без содержания", geo

    return True, None, geo
