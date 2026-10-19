"""Ретро-сайт «Моё расписание» поверх netschoolpy: API и раздача собранного фронта."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from urllib.parse import quote
from pathlib import Path

import netschoolpy
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from netschoolpy import REGIONS, NetSchool
from pydantic import BaseModel

from . import accounts, esia, guestbook, school, sources
from . import schools as schools_search
from .accounts import COOKIE, Account

log = logging.getLogger("uvicorn.error")
DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

app = FastAPI(title="Моё расписание")
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """Файлы из assets/ с хэшем в имени — навсегда; всё остальное браузер перепроверяет."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/icons/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
    else:
        response.headers["Cache-Control"] = "no-cache"
    return response


def _upstream_errors(action: str):
    """Ошибки «Сетевого города» -> понятные HTTP-ответы."""

    class _Guard:
        def __enter__(self):
            return self

        def __exit__(self, kind, exc, tb):
            if exc is None or isinstance(exc, HTTPException):
                return False
            if isinstance(exc, netschoolpy.SessionExpired):
                raise HTTPException(401, "Сессия истекла, войдите снова") from exc
            if isinstance(exc, sources.ReportLimit):
                raise HTTPException(429, "«Сетевой город» ограничивает число запросов. Попробуй через пару минут.") from exc
            if isinstance(exc, netschoolpy.NetSchoolError):
                raise HTTPException(502, str(exc) or f"{action}: сервер не ответил") from exc
            log.exception("%s failed", action)
            raise HTTPException(502, "Сервер «Сетевого города» не отвечает, попробуй ещё раз") from exc

    return _Guard()


# ───────────────────────── вход ─────────────────────────


class LoginBody(BaseModel):
    url: str
    login: str
    password: str
    school: str


@app.get("/api/regions")
async def regions() -> dict[str, str]:
    return dict(sorted(REGIONS.items()))


@app.get("/api/schools")
async def schools(url: str, q: str = "") -> list[dict]:
    with _upstream_errors("Поиск школ"):
        return await schools_search.search(url, q)


@app.post("/api/login")
async def login(body: LoginBody, response: Response) -> dict:
    web = NetSchool(body.url.rstrip("/"))
    school: int | str = int(body.school) if body.school.isdigit() else body.school
    try:
        await web.login(body.login, body.password, school)
    except netschoolpy.InvalidCredentials:
        raise HTTPException(401, "Неверный логин или пароль")
    except netschoolpy.AccountLocked as e:
        raise HTTPException(423, str(e) or "Аккаунт временно заблокирован, подожди и попробуй позже")
    except netschoolpy.SchoolNotFound:
        raise HTTPException(404, "Школа не найдена")
    except Exception as e:
        with _upstream_errors("Вход"):
            raise e
    account = Account(web, await accounts.attach_mobile(web))
    sid = accounts.register(account)
    response.set_cookie(COOKIE, sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return sources.me(account)


@app.get("/api/login/methods")
async def login_methods(url: str) -> dict:
    """Какие способы входа включены на сервере региона."""
    try:
        m = await asyncio.wait_for(netschoolpy.get_login_methods(url), timeout=10)
    except Exception:  # noqa: BLE001 — не знаем, значит показываем всё
        return {"password": True, "esia": True, "esia_main": False}
    return {"password": m.password, "esia": m.esia or m.esia_button or m.esia_main, "esia_main": m.esia_main}


class EsiaBody(BaseModel):
    url: str
    school: str = ""
    login: str = ""
    password: str = ""


class StepBody(BaseModel):
    flow: str
    answer: str | int | None = None


async def _esia_reply(key: str, response: Response, answer: str | int | None = None) -> dict:
    event, account = await esia.step(key, answer)
    if account is not None:
        sid = accounts.register(account)
        response.set_cookie(COOKIE, sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return {"state": "done", "me": sources.me(account)}
    return event | {"flow": key}


@app.post("/api/login/esia")
async def login_esia(body: EsiaBody, response: Response) -> dict:
    key = esia.start_password(body.url, body.login, body.password, body.school)
    return await _esia_reply(key, response)


@app.post("/api/login/qr")
async def login_qr(body: EsiaBody, response: Response) -> dict:
    key = esia.start_qr(body.url, body.school)
    return await _esia_reply(key, response)


@app.post("/api/login/step")
async def login_step(body: StepBody, response: Response) -> dict:
    return await _esia_reply(body.flow, response, body.answer)


@app.post("/api/login/cancel")
async def login_cancel(body: StepBody) -> dict:
    esia.cancel(body.flow)
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request) -> dict:
    return sources.me(await accounts.current(request))


@app.post("/api/logout")
async def logout(request: Request, response: Response) -> dict:
    await accounts.forget(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ───────────────────────── дневник ─────────────────────────


@app.get("/api/week")
async def week(request: Request, start: dt.date | None = None) -> dict:
    account = await accounts.current(request)
    today = dt.date.today()
    start = start or today
    start -= dt.timedelta(days=start.weekday())
    try:
        with _upstream_errors("Дневник"):
            return await sources.week(account, start, start + dt.timedelta(days=5))
    except HTTPException as e:
        if e.status_code == 401:
            accounts.drop(request)
        raise


@app.get("/api/lesson")
async def lesson(request: Request, sg: int, day: dt.date) -> dict:
    """Окно урока без дневника: задания к нему и что задано на следующий урок предмета."""
    account = await accounts.current(request)
    try:
        with _upstream_errors("Задания"):
            rows = await sources.group_tasks(account, sg)
    except HTTPException as e:
        if e.status_code != 502 or not isinstance(e.__cause__, netschoolpy.NetSchoolError):
            raise
        # Внеурочных групп обычно нет в фильтре отчёта.
        return {"due": [], "next_day": None, "next": [], "note": "Для этого предмета «Сетевой город» не отдаёт задания."}
    iso_day = day.isoformat()
    later = sorted({a["due"] for a in rows if a["due"] > iso_day})
    return {
        "due": [a for a in rows if a["due"] == iso_day],
        "next_day": later[0] if later else None,
        "next": [a for a in rows if later and a["due"] == later[0]],
    }


@app.get("/api/assignment/{assignment_id}")
async def assignment(request: Request, assignment_id: int) -> dict:
    """Карточка задания из дневника: полное описание и вложения."""
    account = await accounts.current(request)
    with _upstream_errors("Задание"):
        return await sources.assignment(account, assignment_id)


# ───────────────────────── фронт ─────────────────────────

if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")
    app.mount("/icons", StaticFiles(directory=DIST / "icons"), name="icons")


# ---------- почта ----------


class MailBody(BaseModel):
    to: list[str]
    subject: str
    text: str


@app.get("/api/mail")
async def mail_list(request: Request, folder: str = "Inbox", page: int = 1) -> dict:
    account = await accounts.current(request)
    with _upstream_errors("Почта"):
        return await school.mail_list(account, folder, max(1, page))


@app.get("/api/mail/unread")
async def mail_unread(request: Request) -> dict:
    account = await accounts.current(request)
    with _upstream_errors("Почта"):
        return {"count": await school.mail_unread(account)}


@app.get("/api/mail/recipients")
async def mail_recipients(request: Request) -> list[dict]:
    account = await accounts.current(request)
    with _upstream_errors("Адресная книга"):
        return await school.mail_recipients(account)


@app.get("/api/mail/attachment/{attachment_id}")
async def mail_attachment(request: Request, attachment_id: int, name: str = "file") -> Response:
    account = await accounts.current(request)
    with _upstream_errors("Вложение"):
        data, kind = await school.mail_attachment(account, attachment_id, name)
    return Response(
        data,
        media_type=kind,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@app.get("/api/mail/{message_id}")
async def mail_read(request: Request, message_id: int) -> dict:
    account = await accounts.current(request)
    with _upstream_errors("Письмо"):
        return await school.mail_read(account, message_id)


@app.get("/api/mail/{message_id}/reply")
async def mail_reply(request: Request, message_id: int) -> dict:
    account = await accounts.current(request)
    with _upstream_errors("Ответ"):
        return await school.mail_reply(account, message_id)


@app.delete("/api/mail/{message_id}")
async def mail_delete(request: Request, message_id: int) -> dict:
    account = await accounts.current(request)
    with _upstream_errors("Удаление письма"):
        await school.mail_delete(account, message_id)
    return {"ok": True}


@app.post("/api/mail")
async def mail_send(request: Request, body: MailBody) -> dict:
    account = await accounts.current(request)
    with _upstream_errors("Отправка письма"):
        return {"id": await school.mail_send(account, body.to, body.subject, body.text)}


# ---------- отчёты ----------


@app.get("/api/reports/totals")
async def report_totals(request: Request) -> dict:
    account = await accounts.current(request)
    with _upstream_errors("Итоговые оценки"):
        return await school.totals(account)


@app.get("/api/reports/subject")
async def report_subject(request: Request, subject: int, term: int) -> dict:
    account = await accounts.current(request)
    with _upstream_errors("Успеваемость по предмету"):
        return await school.performance(account, subject, term)


# ---------- гостевая книга ----------


class GuestPost(BaseModel):
    name: str
    text: str
    parent: str | None = None
    token: str
    website: str = ""  # ловушка для ботов, человек её не видит


async def _author(request: Request) -> str | None:
    if not request.cookies.get(COOKIE):
        return None
    try:
        return guestbook.author_of(await accounts.current(request))
    except HTTPException:
        return None


@app.get("/api/guestbook")
async def guestbook_list(request: Request) -> dict:
    author = await _author(request)
    return guestbook.listing(author) | {"can_post": author is not None}


@app.post("/api/guestbook")
async def guestbook_post(request: Request, body: GuestPost) -> dict:
    author = guestbook.author_of(await accounts.current(request))
    return await guestbook.post(author, body.name, body.text, body.parent, body.token, body.website)


@app.delete("/api/guestbook/{entry_id}")
async def guestbook_delete(request: Request, entry_id: str) -> dict:
    await guestbook.delete(guestbook.author_of(await accounts.current(request)), entry_id)
    return {"ok": True}


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(404)
    file = DIST / path
    if path and file.is_file() and DIST in file.resolve().parents:
        return FileResponse(file)
    index = DIST / "index.html"
    if not index.is_file():
        raise HTTPException(503, "Фронт не собран: выполни `bun run build` в папке web")
    return FileResponse(index)
