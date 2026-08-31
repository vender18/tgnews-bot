"""Обёртка над LLM: провайдеры, дневной бюджет, учёт расхода.

Конвейер обязан работать и без модели — все вызовы возвращают None вместо
исключения, а вызывающий код имеет rule-based запасной путь.
"""
from __future__ import annotations

import json
import logging
import re

from . import config as cfg
from . import util
from .db import DB

log = logging.getLogger("llm")

# модели, которые понимают output_config.effort
_EFFORT_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
                  "claude-fable-5")


class LLM:
    def __init__(self, db: DB) -> None:
        self.db = db
        conf = cfg.config()["llm"]
        self.conf = conf
        self.provider = (cfg.env("LLM_PROVIDER") or conf.get("provider") or "none").lower()
        self.models = conf["models"].get(self.provider, {})
        self._client = None
        self.enabled = bool(conf.get("enabled", True)) and self.provider != "none"
        if self.enabled and self.provider == "anthropic" and not cfg.env("ANTHROPIC_API_KEY"):
            self.enabled = False
        if self.enabled and self.provider == "groq" and not cfg.env("GROQ_API_KEY"):
            self.enabled = False

    # --- бюджет ------------------------------------------------------------

    def spent_today(self) -> float:
        day = util.now_msk().strftime("%Y-%m-%d")
        return float(self.db.scalar(
            "SELECT COALESCE(SUM(cost), 0) AS s FROM llm_usage WHERE day = ?", (day,), 0.0))

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        budget = float(self.conf.get("daily_budget_usd") or 0)
        if budget <= 0:
            return True
        return self.spent_today() < budget

    def _record(self, model: str, purpose: str, in_tokens: int, out_tokens: int) -> None:
        prices = self.conf.get("pricing", {}).get(model, {"input": 0.0, "output": 0.0})
        cost = (in_tokens * prices.get("input", 0.0) + out_tokens * prices.get("output", 0.0)) / 1_000_000
        self.db.execute(
            """INSERT INTO llm_usage (day, model, purpose, in_tokens, out_tokens, cost, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (util.now_msk().strftime("%Y-%m-%d"), model, purpose, in_tokens, out_tokens,
             cost, util.now_iso()),
        )

    # --- вызов -------------------------------------------------------------

    def model_for(self, smart: bool) -> str:
        return self.models.get("smart" if smart else "cheap", "")

    def ask(self, prompt: str, *, system: str | None = None, smart: bool = False,
            purpose: str = "", max_tokens: int | None = None,
            effort: str = "low") -> str | None:
        if not self.available:
            return None
        model = self.model_for(smart)
        if not model:
            return None
        max_tokens = max_tokens or int(self.conf.get("max_output_tokens", 2000))
        try:
            if self.provider == "anthropic":
                return self._ask_anthropic(model, prompt, system, max_tokens, purpose, effort)
            if self.provider == "groq":
                return self._ask_groq(model, prompt, system, max_tokens, purpose)
        except Exception as exc:  # noqa: BLE001 — модель не должна ронять конвейер
            log.warning("LLM (%s, %s) отказала: %s", self.provider, purpose, exc)
        return None

    def _ask_anthropic(self, model: str, prompt: str, system: str | None,
                       max_tokens: int, purpose: str, effort: str) -> str | None:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic(api_key=cfg.env("ANTHROPIC_API_KEY"),
                                               max_retries=3, timeout=90.0)
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        if model.startswith(_EFFORT_MODELS):
            kwargs["output_config"] = {"effort": effort}
        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            log.warning("Anthropic %s: %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError as exc:
            log.warning("Anthropic недоступна: %s", exc)
            return None

        usage = response.usage
        self._record(model, purpose, usage.input_tokens or 0, usage.output_tokens or 0)
        if response.stop_reason == "refusal":
            return None
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def _ask_groq(self, model: str, prompt: str, system: str | None,
                  max_tokens: int, purpose: str) -> str | None:
        import httpx

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {cfg.env('GROQ_API_KEY')}"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens,
                  "temperature": 0.2},
            timeout=90.0,
        )
        if response.status_code >= 400:
            log.warning("Groq %s: %s", response.status_code, response.text[:200])
            return None
        payload = response.json()
        usage = payload.get("usage", {})
        self._record(model, purpose, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        choices = payload.get("choices") or []
        if not choices:
            return None
        return (choices[0].get("message", {}).get("content") or "").strip()

    # --- JSON --------------------------------------------------------------

    def ask_json(self, prompt: str, **kwargs):
        raw = self.ask(prompt, **kwargs)
        return parse_json(raw)


def parse_json(raw: str | None):
    """Достаёт JSON из ответа модели, даже если вокруг него есть текст или ```-обёртка."""
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return None
