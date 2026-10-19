"""Вход через Госуслуги: по паролю (с кодом из SMS, «Макс» или приложения) и по QR-коду.

Вход в библиотеке — одна длинная корутина, которая по ходу спрашивает код и
организацию через колбэки. Здесь она крутится фоном, а HTTP-запросы сайта
забирают её события («нужен код», «покажи QR», «готово») и передают ответы.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import netschoolpy
from fastapi import HTTPException
from netschoolpy import NetSchool

from . import accounts
from .accounts import Account

log = logging.getLogger("uvicorn.error")

FLOW_TTL = 600  # незаконченный вход живёт 10 минут
POLL = 25  # столько держим запрос в ожидании события
ANSWER_TIMEOUT = 300  # столько ждём код или выбор организации от человека

CODE_HINTS = {
    "SMS": "Код из SMS",
    "MAX": "Код из приложения «Макс»",
    "TOTP": "Код из приложения-аутентификатора",
}


@dataclass
class Flow:
    web: NetSchool
    school: str = ""
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    answer: asyncio.Future | None = None
    task: asyncio.Task | None = None
    created: float = field(default_factory=time.monotonic)
    last: dict = field(default_factory=lambda: {"state": "wait"})

    async def ask(self, event: dict) -> Any:
        self.answer = asyncio.get_running_loop().create_future()
        await self.events.put(event)
        try:
            return await asyncio.wait_for(self.answer, ANSWER_TIMEOUT)
        finally:
            self.answer = None


_flows: dict[str, Flow] = {}


def _cleanup() -> None:
    now = time.monotonic()
    for key, flow in list(_flows.items()):
        if now - flow.created > FLOW_TTL:
            if flow.task and not flow.task.done():
                flow.task.cancel()
            del _flows[key]


def _label(user: dict) -> str:
    return (
        user.get("displayName")
        or user.get("name")
        or user.get("schoolName")
        or user.get("organizationName")
        or str(user.get("id", "?"))
    )


def _error_text(exc: Exception) -> str:
    if isinstance(exc, netschoolpy.InvalidCredentials):
        return "Неверный логин или пароль Госуслуг"
    if isinstance(exc, netschoolpy.AccountLocked):
        return str(exc) or "Учётная запись Госуслуг временно заблокирована"
    if isinstance(exc, netschoolpy.MFAError):
        return str(exc) or "Код не подошёл"
    if isinstance(exc, asyncio.TimeoutError):
        return "Слишком долго ждали — начни вход заново"
    if isinstance(exc, netschoolpy.NetSchoolError):
        return str(exc) or "Госуслуги не пустили"
    return "Госуслуги не отвечают, попробуй ещё раз"


MAX_FLOWS = 20  # одновременных незаконченных входов на весь сайт


def _start(url: str, school: str, run) -> str:
    _cleanup()
    if len(_flows) >= MAX_FLOWS:
        raise HTTPException(429, "Сейчас слишком много входов через Госуслуги, попробуй через минуту")
    if not url.startswith(("https://", "http://")):
        raise HTTPException(400, "Выбери регион или укажи адрес сервера")
    flow = Flow(NetSchool(url.rstrip("/")), school=school.strip())

    async def otp(kind: str, info: dict) -> str:
        kind = (kind or "").upper()
        return await flow.ask(
            {
                "state": "code",
                "kind": kind,
                "hint": CODE_HINTS.get(kind, "Код подтверждения"),
                "length": info.get("code_length") or 6,
                "phone": info.get("phone") or "",
            }
        )

    async def choose(users: list[dict]) -> int:
        labels = [_label(u) for u in users]
        # Школу уже выбрали в форме — берём её без лишнего вопроса.
        needle = flow.school.casefold()
        if needle:
            for i, label in enumerate(labels):
                if needle in label.casefold() or label.casefold() in needle:
                    return i
        return int(await flow.ask({"state": "choose", "options": labels}))

    async def show_qr(data: str) -> None:
        await flow.events.put({"state": "qr", "qr": data})

    async def scanned(event: dict) -> None:
        if str(event.get("status") or event.get("event") or "").upper() in {"SCANNED", "SCAN"}:
            await flow.events.put({"state": "scanned"})

    async def task() -> None:
        try:
            await run(flow.web, otp=otp, choose=choose, show_qr=show_qr, scanned=scanned)
            account = Account(flow.web, await accounts.attach_mobile(flow.web))
            await flow.events.put({"state": "done", "account": account})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — любую ошибку показываем человеку
            log.warning("Вход через Госуслуги не удался: %s", str(exc)[:200])
            await flow.events.put({"state": "error", "message": _error_text(exc)})

    key = secrets.token_urlsafe(18)
    flow.task = asyncio.create_task(task())
    _flows[key] = flow
    return key


def start_password(url: str, login: str, password: str, school: str) -> str:
    if not login.strip() or not password:
        raise HTTPException(400, "Нужны логин и пароль Госуслуг")

    async def run(web: NetSchool, *, otp, choose, **_) -> None:
        await web.login_via_gosuslugi(login.strip(), password, otp_callback=otp, user_callback=choose)

    return _start(url, school, run)


def start_qr(url: str, school: str) -> str:
    async def run(web: NetSchool, *, otp, choose, show_qr, scanned) -> None:
        await web.login_via_gosuslugi_qr(
            show_qr, qr_timeout=180, otp_callback=otp, scan_callback=scanned, user_callback=choose
        )

    return _start(url, school, run)


async def step(key: str, answer: str | int | None = None) -> tuple[dict, Account | None]:
    """Передать ответ (код или номер организации) и дождаться следующего события."""
    flow = _flows.get(key)
    if flow is None:
        raise HTTPException(404, "Вход устарел — начни заново")
    if answer is not None and flow.answer is not None and not flow.answer.done():
        flow.answer.set_result(answer)
        flow.last = {"state": "wait"}
    try:
        event = await asyncio.wait_for(flow.events.get(), POLL)
    except asyncio.TimeoutError:
        return flow.last, None  # ничего нового — клиент спросит ещё раз
    if event["state"] in {"done", "error"}:
        _flows.pop(key, None)
    account = event.pop("account", None)
    if event["state"] in {"qr", "code", "choose", "scanned"}:
        flow.last = event
    return event, account


def cancel(key: str) -> None:
    flow = _flows.pop(key, None)
    if flow and flow.task and not flow.task.done():
        flow.task.cancel()
