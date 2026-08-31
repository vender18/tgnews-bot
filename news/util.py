"""Время, текст, simhash, сущности."""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
_URL_RE = re.compile(r"https?://\S+")
_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "⬀-⯿" "]+"
)
_SENT_RE = re.compile(r"(?<=[.!?…])\s+(?=[«\"A-ZА-ЯЁ])")
_STOP = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она",
    "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее",
    "мне", "было", "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда",
    "даже", "ну", "вдруг", "ли", "если", "уже", "или", "ни", "быть", "был", "него", "до",
    "вас", "нибудь", "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя", "их", "чем",
    "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже", "себе", "под", "будет",
    "the", "a", "an", "of", "to", "in", "on", "and", "for", "is", "are", "was", "were",
    "at", "by", "with", "from", "as", "it", "its", "that", "this", "has", "have", "be",
    "de", "la", "le", "les", "des", "du", "et", "en", "un", "une", "pour", "dans", "sur",
}


# --- время -----------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_msk() -> datetime:
    return datetime.now(MSK)


def iso(dt: datetime) -> str:
    """ISO-8601 в UTC без микросекунд — так время лежит в БД."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def now_iso() -> str:
    return iso(now_utc())


def ago_iso(hours: float = 0, days: float = 0) -> str:
    return iso(now_utc() - timedelta(hours=hours, days=days))


def after_iso(hours: float = 0, days: float = 0) -> str:
    return iso(now_utc() + timedelta(hours=hours, days=days))


def parse_dt(value) -> datetime | None:
    """Разбирает всё, что приходит из RSS/HTML: struct_time, ISO, RFC-822."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (tuple, list)) and len(value) >= 6:
        try:
            return datetime(*value[:6], tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(text)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    cleaned = text.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.fromisoformat(cleaned) if fmt is None else datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def to_msk(value: str | datetime) -> datetime:
    dt = parse_dt(value) or now_utc()
    return dt.astimezone(MSK)


def hhmm_to_minutes(value: str) -> int:
    hours, _, minutes = value.partition(":")
    return int(hours) * 60 + int(minutes)


def in_quiet_hours(moment: datetime, start: str, end: str) -> bool:
    minute = moment.hour * 60 + moment.minute
    start_m, end_m = hhmm_to_minutes(start), hhmm_to_minutes(end)
    if start_m <= end_m:
        return start_m <= minute < end_m
    return minute >= start_m or minute < end_m


# --- текст -----------------------------------------------------------------

def clean_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = text.replace("\r", "")
    text = _WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def strip_promo(text: str) -> str:
    """Убирает хвосты телеграм-постов: «подписывайтесь», ссылки на канал, эмодзи-мусор."""
    if not text:
        return ""
    drop = re.compile(
        r"(подпис(ывайтесь|аться|ка)|наш\s+(канал|телеграм)|прислать\s+новость|"
        r"читайте\s+(нас|в)|erid|реклама|@[\w_]+\s*$|"
        r"больше\s+новостей|источник:\s*$)",
        re.IGNORECASE,
    )
    lines = []
    for line in text.split("\n"):
        candidate = line.strip()
        if not candidate or drop.search(candidate):
            continue
        lines.append(candidate)
    text = "\n".join(lines)
    text = _URL_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


_BOILERPLATE = re.compile(
    r"^\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4}\s*(в\s*)?\d{1,2}[:.]\d{2}\s*[.,–—-]?\s*"
    r"|Агентство\s+[«\"„][^»\"“]+[»\"“]\.?\s*"
    r"|Фото:\s*[^.]{0,60}\.\s*"
    r"|Читайте\s+далее.*$)",
    re.IGNORECASE,
)


def strip_boilerplate(text: str) -> str:
    """Служебные хвосты RSS-описаний: дата-время, подпись агентства, «фото:»."""
    previous = None
    while previous != text:
        previous = text
        text = _BOILERPLATE.sub("", text or "").strip()
    return text


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я ]+", " ", text)


def tokens(text: str) -> list[str]:
    return [w for w in normalize(text).split() if len(w) > 2 and w not in _STOP]


def sentences(text: str, limit: int = 3) -> list[str]:
    parts = [p.strip() for p in _SENT_RE.split(text or "") if p.strip()]
    return parts[:limit]


def shorten(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:—-") + "…"


def esc(text: str | None) -> str:
    """Экранирование для parse_mode=HTML телеграма."""
    return html.escape(text or "", quote=False)


# --- дедупликация ----------------------------------------------------------

def simhash(text: str, bits: int = 64) -> int:
    """Простой word-level simhash: устойчив к перестановкам и мелким правкам."""
    words = tokens(text)
    if not words:
        return 0
    features: dict[str, int] = {}
    for word in words:
        features[word] = features.get(word, 0) + 1
    # биграммы дают чувствительность к порядку слов
    for a, b in zip(words, words[1:]):
        key = f"{a}_{b}"
        features[key] = features.get(key, 0) + 2

    vector = [0] * bits
    for feature, weight in features.items():
        digest = int(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).hexdigest(), 16)
        for i in range(bits):
            vector[i] += weight if (digest >> i) & 1 else -weight
    value = 0
    for i in range(bits):
        if vector[i] > 0:
            value |= 1 << i
    return value


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch",
    "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "u", "я": "a",
}


def translit(word: str) -> str:
    """Грубая транслитерация: «Месси» и «Messi» должны стать одним ключом."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in word.lower())


_LATIN_CANON = str.maketrans({"c": "k", "q": "k", "w": "v", "x": "k", "j": "i"})
_VOWELS = "aeiouy"


def entity_key(word: str) -> str:
    """Ключ сущности — согласный скелет транслита.

    Падежи («Банка» и «Банк») и переводы-соответствия («России» и «Russia»,
    «Трамп» и «Trump») дают одинаковый ключ; гласные, в которых языки и падежи
    расходятся сильнее всего, отбрасываются.
    """
    base = translit(word).translate(_LATIN_CANON)
    skeleton = "".join(ch for ch in base if ch.isalnum() and ch not in _VOWELS)
    if len(skeleton) < 2:
        return base[:4]
    return skeleton[:6]


_ENTITY_RE = re.compile(r"\b[А-ЯЁA-Z][\wа-яёa-z\-]{2,}\b")
_NUM_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?(?:%|процент\w*|percent|млн|млрд|тыс|million|billion|"
    r"руб|рублей|долларов|dollars|евро|euros|человек|people|км|km|градус\w*)\b",
    re.IGNORECASE,
)


def entities(text: str) -> set[str]:
    """Имена собственные и числовые факты — второй сигнал «то же событие».

    Ключи транслитерируются и обрезаются до основы, поэтому «Мессиподробнее»,
    «Месси» и «Messi» дают одно и то же — иначе русская и английская версии
    одного события никогда не склеятся.
    """
    found: set[str] = set()
    for match in _ENTITY_RE.findall(text or ""):
        low = normalize(match.strip()).strip()
        if len(low) > 2 and low not in _STOP:
            found.add(entity_key(low))
    for match in _NUM_RE.findall(text or ""):
        digits = re.sub(r"[^\d.,%]", "", match)
        if digits:
            found.add("#" + digits)
    return found


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0
