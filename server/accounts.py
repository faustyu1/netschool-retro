"""Входы пользователей сайта: клиенты «Сетевого города» и их хранение между перезапусками."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import netschoolpy
from fastapi import HTTPException, Request
from netschoolpy import MobileNetSchool, NetSchool

log = logging.getLogger("uvicorn.error")

COOKIE = "ns_sid"
# В Docker — на томе (/data), чтобы входы переживали пересборку контейнера.
SESSIONS_FILE = Path(os.environ.get("SESSIONS_FILE") or Path(__file__).resolve().parent.parent / ".sessions.json")
# Ключ приложения parent-mobile. С библиотекой он не поставляется: без него
# сайт работает через веб-API, с ним — через мобильный (полный дневник, без лимита отчётов).
MOBILE_SECRET = os.environ.get("NETSCHOOL_MOBILE_CLIENT_SECRET", "").strip()


@dataclass
class Account:
    """Один вход: веб-клиент есть всегда, мобильный — если задан ключ."""

    web: NetSchool
    mobile: MobileNetSchool | None = None
    # Ленивый кэш данных, которые дорого получать (группы, звонки, отчёты).
    cache: dict[str, Any] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        return "mobile" if self.mobile else "web"

    @property
    def url(self) -> str:
        return str(self.web._http.base_url)

    def export(self) -> dict:
        return {
            "url": self.url,
            "session": self.web.export_session(),
            "mobile": self.mobile.export_session() if self.mobile else None,
        }


_accounts: dict[str, Account] = {}
_restore_lock = asyncio.Lock()


def _load_saved() -> dict[str, dict]:
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(sid: str, account: Account | None) -> None:
    saved = _load_saved()
    if account is None:
        saved.pop(sid, None)
    else:
        saved[sid] = account.export()
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(saved))
    SESSIONS_FILE.chmod(0o600)


async def attach_mobile(web: NetSchool) -> MobileNetSchool | None:
    """Мобильный клиент из живой веб-сессии; без ключа или при отказе — None."""
    if not MOBILE_SECRET:
        return None
    try:
        return await MobileNetSchool.from_web(web, client_secret=MOBILE_SECRET)
    except Exception as e:
        log.warning("Мобильный API недоступен, остаёмся на веб-API: %s", str(e)[:160])
        return None


async def _restore(sid: str) -> Account:
    if (account := _accounts.get(sid)) is not None:
        return account
    saved = _load_saved().get(sid)
    if not saved:
        raise HTTPException(401, "Не выполнен вход")
    web = NetSchool(saved["url"])
    try:
        await web.import_session(saved["session"])
    except netschoolpy.NetSchoolError:
        _save(sid, None)
        raise HTTPException(401, "Сессия истекла, войдите снова")
    mobile = None
    if MOBILE_SECRET:
        if saved.get("mobile"):
            mobile = MobileNetSchool(saved["url"], client_secret=MOBILE_SECRET)
            try:
                await mobile.import_session(saved["mobile"])
            except netschoolpy.NetSchoolError:
                mobile = None
        mobile = mobile or await attach_mobile(web)
    account = _accounts[sid] = Account(web, mobile)
    _save(sid, account)
    return account


async def current(request: Request) -> Account:
    """Вход по cookie; после перезапуска сервера — восстановление из файла."""
    sid = request.cookies.get(COOKIE, "")
    if (account := _accounts.get(sid)) is not None:
        return account
    async with _restore_lock:  # параллельные запросы не должны восстанавливать вход дважды
        return await _restore(sid)


def register(account: Account) -> str:
    sid = secrets.token_urlsafe(24)
    _accounts[sid] = account
    _save(sid, account)
    return sid


async def forget(sid: str) -> None:
    account = _accounts.pop(sid, None)
    _save(sid, None)
    if account is not None:
        try:
            await account.web.logout()
        except netschoolpy.NetSchoolError:
            pass


def drop(request: Request) -> None:
    """Сессию отверг сервер — забыть её, чтобы следующий запрос попросил вход."""
    sid = request.cookies.get(COOKIE, "")
    _accounts.pop(sid, None)
    _save(sid, None)
