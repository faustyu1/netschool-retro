"""Почта и отчёты «Сетевого города».

Почта работает и через мобильный, и через веб-API. Отчёты об успеваемости —
только через мобильный: там итоги и разбор оценок по предмету отдаются сразу,
без очереди отчётов веб-версии с её лимитом на пользователя.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import mimetypes
import time

from fastapi import HTTPException

from .accounts import Account

FOLDERS = {"Inbox": "Входящие", "Sent": "Отправленные", "Deleted": "Удалённые"}
PAGE_SIZE = 20


def _when(value: dt.datetime | str | None) -> str:
    return value.isoformat(timespec="minutes") if isinstance(value, dt.datetime) else (value or "")


async def _cached(account: Account, key: str, ttl: float, load):
    hit = account.cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = await load()
    account.cache[key] = (time.monotonic(), value)
    return value


def _forget(account: Account, prefix: str) -> None:
    for key in [k for k in account.cache if isinstance(k, str) and k.startswith(prefix)]:
        del account.cache[key]


# ---------- почта ----------


def _client(account: Account):
    return account.mobile or account.web


async def mail_list(account: Account, folder: str, page: int) -> dict:
    if folder not in FOLDERS:
        raise HTTPException(400, "Нет такой папки")
    data = await asyncio.wait_for(_client(account).mail_list(folder, page, PAGE_SIZE), timeout=20)
    return {
        "folder": folder,
        "page": data.page,
        "pages": max(1, -(-data.total_items // PAGE_SIZE)),
        "total": data.total_items,
        "entries": [
            {
                "id": e.id,
                "subject": e.subject,
                "author": e.author,
                "to": e.to_names or "",
                "sent": _when(e.sent),
                "read": e.read is not False,
            }
            for e in data.entries
        ],
    }


async def mail_unread(account: Account) -> int:
    client = _client(account)

    async def load() -> int:
        if account.mobile:
            return await account.mobile.mail_unread_count()
        return len(await client.mail_unread())

    return await _cached(account, "mail:unread", 60, load)


async def mail_read(account: Account, message_id: int) -> dict:
    m = await asyncio.wait_for(_client(account).mail_read(message_id), timeout=20)
    _forget(account, "mail:unread")  # чтение помечает письмо прочитанным
    return {
        "id": m.id,
        "subject": m.subject,
        "text": m.text,
        "sent": _when(m.sent),
        "author": m.author_name,
        "to": m.to_names,
        "folder": m.mailbox,
        "can_reply": m.can_reply,
        "attachments": [{"id": a.id, "name": a.name} for a in m.file_attachments if a.id],
    }


async def mail_attachment(account: Account, attachment_id: int, name: str) -> tuple[bytes, str]:
    data = await asyncio.wait_for(_client(account).download_attachment_as_bytes(attachment_id), timeout=60)
    return data, mimetypes.guess_type(name)[0] or "application/octet-stream"


async def mail_recipients(account: Account) -> list[dict]:
    async def load() -> list[dict]:
        people = await _client(account).mail_recipients()
        return sorted(
            ({"id": p.id, "name": p.name, "org": p.organization_name} for p in people),
            key=lambda p: p["name"],
        )

    return await _cached(account, "mail:recipients", 3600, load)


async def mail_reply(account: Account, message_id: int) -> dict:
    d = await _client(account).mail_draft(message_id, "reply")
    return {"subject": d.subject, "text": d.text, "to": [{"id": p.id, "name": p.name} for p in d.to]}


async def mail_send(account: Account, to: list[str], subject: str, text: str) -> int:
    if not to:
        raise HTTPException(400, "Выбери получателя")
    subject, text = subject.strip(), text.strip()
    if not subject or not text:
        raise HTTPException(400, "Нужны тема и текст письма")
    if len(subject) > 200 or len(text) > 10_000:
        raise HTTPException(400, "Слишком длинное письмо")
    return await _client(account).mail_send(subject, text, to)


async def mail_delete(account: Account, message_id: int) -> None:
    await _client(account).mail_delete([message_id])


# ---------- отчёты об успеваемости ----------


def _mobile(account: Account):
    if account.mobile is None:
        raise HTTPException(501, "Отчёты доступны только через мобильный API — задай NETSCHOOL_MOBILE_CLIENT_SECRET")
    return account.mobile


def _mark(total: dict) -> dict:
    """Итог за период: средний балл и выставленная отметка, если есть."""
    mark = total.get("mark") or total.get("markValue") or total.get("value")
    if isinstance(mark, dict):
        mark = mark.get("name") or mark.get("value")
    return {"avg": total.get("avgMark"), "mark": mark, "code": total.get("markCode")}


async def totals(account: Account) -> dict:
    mobile = _mobile(account)

    async def load() -> dict:
        subjects, terms, rows = await asyncio.gather(mobile.subjects_raw(), mobile.terms(), mobile.totals())
        names = {s["id"]: (s["name"], s.get("order", 0)) for s in subjects}
        periods: dict[str, str] = {}
        for t in terms:
            periods[f"t{t['id']}"] = t["name"]
        result = []
        for row in rows:
            cells: dict[str, dict] = {}
            for t in row.get("termTotals") or []:
                cells[f"t{t['term']['id']}"] = _mark(t)
            for y in row.get("yearTotals") or []:
                key = f"y{y['period']['id']}"
                periods.setdefault(key, y["period"]["periodName"])
                cells[key] = _mark(y)
            name, order = names.get(row["subjectId"], (f"Предмет {row['subjectId']}", 999))
            result.append({"id": row["subjectId"], "name": name, "order": order, "cells": cells})
        result.sort(key=lambda r: (r["order"], r["name"]))
        current = next((f"t{t['id']}" for t in terms if t.get("current")), None)
        return {
            "periods": [{"key": k, "name": v} for k, v in periods.items()],
            "terms": [{"id": t["id"], "name": t["name"], "current": bool(t.get("current"))} for t in terms],
            "current": current,
            "subjects": result,
        }

    return await _cached(account, "report:totals", 600, load)


async def performance(account: Account, subject_id: int, term_id: int) -> dict:
    mobile = _mobile(account)

    async def load() -> dict:
        p = await mobile.subject_performance(subject_id, term_id)
        stats = p.get("classmeetingsStats") or {}
        return {
            "subject": (p.get("subject") or {}).get("name", ""),
            "term": (p.get("term") or {}).get("name", ""),
            "avg": p.get("averageMark"),
            "class_avg": p.get("classAverageMark"),
            "max": p.get("maxMark") or 5,
            "lessons": {"passed": stats.get("passed"), "scheduled": stats.get("scheduled")},
            "teachers": [t.get("name", "") for t in p.get("teachers") or []],
            "results": [
                {
                    "date": (r.get("classMeetingDate") or r.get("date") or "")[:10],
                    "mark": r.get("result"),
                    "duty": bool(r.get("duty")),
                    "weight": r.get("weight"),
                    "kind": r.get("assignmentTypeName") or "",
                    "abbr": r.get("assignmentTypeAbbr") or "",
                    "comment": r.get("comment") or "",
                }
                for r in p.get("results") or []
            ],
            "stats": [{"mark": s.get("mark"), "count": s.get("count", 0)} for s in p.get("markStats") or []],
        }

    return await _cached(account, f"report:perf:{subject_id}:{term_id}", 600, load)
