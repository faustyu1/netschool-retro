"""Гостевая книга: общая для всех, хранится в томе рядом со входами.

Читать может кто угодно, писать — только вошедшие через «Сетевой город».
Защита от спама, по слоям:
- автор — это ученик «Сетевого города» (хэш сервера и id ученика), а не cookie:
  перелогин не обнуляет лимиты;
- лимиты: пауза между сообщениями, сообщений в час на автора, общий потолок на сайт;
- одноразовый подписанный токен формы: бот, отправивший форму быстрее MIN_FILL
  секунд или повторно, отсекается;
- поле-ловушка, которое человек не видит;
- длина, ссылки, повторы одного символа, дубликаты текста.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import unicodedata
from collections import deque
from pathlib import Path

from fastapi import HTTPException

from .accounts import SESSIONS_FILE, Account

DATA = SESSIONS_FILE.parent
FILE = Path(os.environ.get("GUESTBOOK_FILE") or DATA / "guestbook.json")
SECRET_FILE = DATA / "guestbook.key"

MAX_NAME = 32
MAX_TEXT = 500
MAX_ENTRIES = 1000
MAX_REPLIES = 50
COOLDOWN = 30  # секунд между сообщениями одного автора
PER_HOUR = 10  # сообщений в час на автора
SITE_WINDOW, SITE_LIMIT = 600, 60  # на весь сайт: не больше 60 сообщений за 10 минут
MIN_FILL = 3  # быстрее человек форму не заполнит
TOKEN_TTL = 2 * 3600

LINK = re.compile(r"https?://|www\.|t\.me/|@\w{4,}|\b[\w-]{2,}\.(?:ru|com|net|org|io|me|xyz|su|top|site|online|рф)\b", re.I)
REPEAT = re.compile(r"(.)\1{11,}", re.S)
CONTROL = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f​-‏‪-‮⁠-⁤﻿]")

_lock = asyncio.Lock()
_posts: dict[str, deque[float]] = {}
_site_posts: deque[float] = deque()
_used_tokens: dict[str, float] = {}


def _secret() -> bytes:
    if key := os.environ.get("GUESTBOOK_SECRET"):
        return key.encode()
    try:
        return SECRET_FILE.read_bytes()
    except OSError:
        key = secrets.token_bytes(32)
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_bytes(key)
        SECRET_FILE.chmod(0o600)
        return key


SECRET = _secret()


def _sign(value: str) -> str:
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()[:24]


def author_of(account: Account) -> str:
    student = (account.mobile or account.web).current_student
    if student is None:
        raise HTTPException(403, "Не удалось определить ученика — войди заново")
    return _sign(f"author:{account.url}:{student.id}")


def _load() -> list[dict]:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return []


def _save(entries: list[dict]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False))
    tmp.chmod(0o600)
    tmp.replace(FILE)


def form_token() -> str:
    stamp = f"{int(time.time())}.{secrets.token_hex(8)}"
    return f"{stamp}.{_sign('form:' + stamp)}"


def _check_token(token: str) -> None:
    now = time.time()
    for used, at in list(_used_tokens.items()):
        if now - at > TOKEN_TTL:
            del _used_tokens[used]
    try:
        issued, nonce, signature = token.split(".")
        age = now - int(issued)
    except ValueError:
        raise HTTPException(400, "Обнови страницу и попробуй ещё раз") from None
    if not hmac.compare_digest(signature, _sign(f"form:{issued}.{nonce}")) or age > TOKEN_TTL:
        raise HTTPException(400, "Форма устарела — обнови страницу")
    if age < MIN_FILL:
        raise HTTPException(429, "Слишком быстро! Подожди пару секунд")
    if nonce in _used_tokens:
        raise HTTPException(400, "Это сообщение уже отправлено")
    _used_tokens[nonce] = now


def _clean(value: str, limit: int, what: str) -> str:
    value = CONTROL.sub("", unicodedata.normalize("NFKC", value or ""))
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if not value:
        raise HTTPException(400, f"{what}: пусто")
    if len(value) > limit:
        raise HTTPException(400, f"{what}: не длиннее {limit} символов")
    if LINK.search(value):
        raise HTTPException(400, "Ссылки и упоминания в гостевой книге нельзя")
    if REPEAT.search(value):
        raise HTTPException(400, "Слишком много одинаковых символов подряд")
    return value


def _rate_limit(author: str) -> None:
    now = time.time()
    while _site_posts and now - _site_posts[0] > SITE_WINDOW:
        _site_posts.popleft()
    if len(_site_posts) >= SITE_LIMIT:
        raise HTTPException(429, "Гостевая книга перегружена, попробуй через несколько минут")
    times = _posts.setdefault(author, deque())
    while times and now - times[0] > 3600:
        times.popleft()
    if times and now - times[-1] < COOLDOWN:
        raise HTTPException(429, f"Подожди {int(COOLDOWN - (now - times[-1])) + 1} с перед следующим сообщением")
    if len(times) >= PER_HOUR:
        raise HTTPException(429, "Хватит на этот час — возвращайся позже")


def _public(entry: dict, author: str | None) -> dict:
    return {key: entry[key] for key in ("id", "parent", "reply_to", "name", "text", "at")} | {
        "mine": author is not None and entry["author"] == author
    }


def listing(author: str | None) -> dict:
    return {"entries": [_public(e, author) for e in _load()], "token": form_token()}


async def post(author: str, name: str, text: str, parent: str | None, token: str, trap: str) -> dict:
    if trap:  # поле-ловушку заполняют только боты
        raise HTTPException(400, "Обнови страницу и попробуй ещё раз")
    name = _clean(name, MAX_NAME, "Имя")
    text = _clean(text, MAX_TEXT, "Сообщение")
    async with _lock:
        _check_token(token)
        _rate_limit(author)
        entries = _load()
        reply_to = None
        if parent:
            target = next((e for e in entries if e["id"] == parent), None)
            if target is None:
                raise HTTPException(404, "Сообщение, на которое ты отвечаешь, удалено")
            # Ветки одного уровня: ответ на ответ попадает в ту же ветку с пометкой «кому».
            reply_to = target["name"] if target["parent"] else None
            parent = target["parent"] or target["id"]
            if sum(e["parent"] == parent for e in entries) >= MAX_REPLIES:
                raise HTTPException(400, "В этой ветке уже слишком много ответов")
        now = time.time()
        if any(e["text"].casefold() == text.casefold() and now - e["at"] < 86400 for e in entries):
            raise HTTPException(400, "Такое сообщение уже есть")
        entry = {
            "id": secrets.token_hex(6),
            "parent": parent,
            "reply_to": reply_to,
            "name": name,
            "text": text,
            "at": int(now),
            "author": author,
        }
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:  # старые ветки уходят целиком, вместе с ответами
            roots = [e["id"] for e in entries if not e["parent"]]
            gone = set(roots[: len(roots) // 10 or 1])
            entries = [e for e in entries if e["id"] not in gone and e["parent"] not in gone]
        _save(entries)
        _posts.setdefault(author, deque()).append(now)
        _site_posts.append(now)
    return _public(entry, author)


async def delete(author: str, entry_id: str) -> None:
    async with _lock:
        entries = _load()
        entry = next((e for e in entries if e["id"] == entry_id), None)
        if entry is None:
            raise HTTPException(404, "Сообщение уже удалено")
        if entry["author"] != author:
            raise HTTPException(403, "Удалять можно только свои сообщения")
        _save([e for e in entries if e["id"] != entry_id and e["parent"] != entry_id])
