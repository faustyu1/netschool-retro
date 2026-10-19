"""Поиск школы для формы входа: свой, с приоритетом обычных школ.

Поиск «Сетевого города» ищет подстроку целиком и выдаёт детские сады вперемешку
со школами (по названию посёлка первыми часто идут сады-«отделения»). Поэтому
список организаций региона забираем один раз, кэшируем и ищем по нему сами:
по словам в любом порядке, без учёта регистра, «ё» и кавычек; школы — выше.
"""

from __future__ import annotations

import asyncio
import re
import time

import netschoolpy

TTL = 24 * 3600
# Поиск сервера требует хотя бы один символ — обходим самыми частыми,
# объединение покрывает весь список организаций региона.
PROBES = ["о", "а", "е", "и", "у", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]

KINDS = [
    # (вид, подпись, порядок) — проверяются по очереди, первый совпавший побеждает
    ("kindergarten", "детсад", 3, re.compile(r"детск\w* сад|д/с|\bдоу\b|мбдоу|гбдоу|ясли|дошкольн", re.I)),
    ("extra", "доп. образование", 2, re.compile(r"\bдши\b|\bдмш\b|\bдхш\b|\bдшм\b|дюсш|\bцдт\b|творчеств|спортивн|музыкальн|художествен|\bдод\b|\bцдо\b", re.I)),
    ("college", "колледж", 1, re.compile(r"колледж|техникум|училищ|\bспо\b", re.I)),
    ("branch", "отделение", 2, re.compile(r"\(до\)|отделени\w* до\b|структурн\w* подразделени|\bсп\b|филиал", re.I)),
    ("school", "школа", 0, re.compile(r"\bсош\b|\bоош\b|\bнош\b|школ|гимнази|лице[йя]|кадет|интернат|\bоц\b|центр образования", re.I)),
]

_cache: dict[str, tuple[float, list[dict]]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _norm(text: str) -> str:
    text = text.casefold().replace("ё", "е")
    text = re.sub(r"[«»\"'“”„]", " ", text)
    text = re.sub(r"№\s*", "№", text)
    return re.sub(r"\s+", " ", text).strip()


def _kind(name: str) -> tuple[str, str, int]:
    for kind, label, order, pattern in KINDS:
        if pattern.search(name):
            return kind, label, order
    return "other", "", 2


async def _all(url: str) -> list[dict]:
    hit = _cache.get(url)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    async with _locks.setdefault(url, asyncio.Lock()):
        hit = _cache.get(url)
        if hit and time.monotonic() - hit[0] < TTL:
            return hit[1]
        gate = asyncio.Semaphore(4)  # не долбим сервер региона пятнадцатью запросами разом

        async def probe(letter: str):
            async with gate:
                return await netschoolpy.search_schools(url, letter)

        results = await asyncio.gather(*(probe(letter) for letter in PROBES), return_exceptions=True)
        found: dict[int, dict] = {}
        for batch in results:
            if isinstance(batch, BaseException):
                continue
            for s in batch:
                if s.id in found:
                    continue
                short = (s.short_name or s.name).strip()
                full = s.name.strip()
                kind, label, order = _kind(full)
                found[s.id] = {
                    "id": s.id,
                    "name": short,
                    "full": full if full != short else "",
                    "kind": kind,
                    "label": label,
                    "order": order,
                    "key": _norm(f"{full} {short} {s.address}"),
                }
        if not found:
            # Сервер не ответил ни на один запрос — пусть ошибка дойдёт до формы.
            errors = [r for r in results if isinstance(r, BaseException)]
            if errors:
                raise errors[0]
        schools = list(found.values())
        _cache[url] = (time.monotonic(), schools)
        return schools


async def search(url: str, query: str, limit: int = 30) -> list[dict]:
    words = _norm(query).split()
    if not words:
        return []
    # Номер ищем целиком: «1» не должен находить № 10 и № 101.
    numbers = [re.compile(rf"(?<!\d){re.escape(w.lstrip('№'))}(?!\d)") for w in words if w.lstrip("№").isdigit()]
    texts = [w for w in words if not w.lstrip("№").isdigit()]
    matched = []
    for s in await _all(url):
        if all(w in s["key"] for w in texts) and all(n.search(s["key"]) for n in numbers):
            name = _norm(s["name"])
            # Школы выше садов; среди равных — где запрос в начале названия, потом короче.
            score = (s["order"], 0 if name.startswith(words[0]) else 1, len(s["name"]))
            matched.append((score, s))
    matched.sort(key=lambda pair: pair[0])
    return [
        {"id": s["id"], "name": s["name"], "full": s["full"], "kind": s["kind"], "label": s["label"]}
        for _, s in matched[:limit]
    ]
