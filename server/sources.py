"""Откуда берутся уроки, оценки и задания.

Порядок такой:
1. мобильный API — полный дневник (кабинеты, учителя, оценки, домашка);
2. веб-дневник ``student/diary`` — то же, но без учителя;
3. если дневник пустой (так бывает на некоторых серверах) — расписание
   ``schedule/classmeetings``, оценки из отчёта StudentTotal, а домашка —
   из отчёта StudentGrades по клику на урок. Отчёты сервер ограничивает
   по числу на пользователя, поэтому они ленивые и кэшируются.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from html.parser import HTMLParser
from typing import Any

import netschoolpy

from .accounts import Account

log = logging.getLogger("uvicorn.error")

MARKS_TTL = 600  # отчёт об успеваемости: 10 минут
TASKS_TTL = 1800  # задания предмета: 30 минут
ATTENDANCE = {"НП", "УП", "Б", "ОТ", "ОП", "ОСВ", "Н"}
MONTHS = ["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]


class ReportLimit(Exception):
    """«Сетевой город» ограничивает число отчётов на пользователя (HTTP 409)."""


# ───────────────────────── неделя ─────────────────────────


async def week(account: Account, start: dt.date, end: dt.date) -> dict:
    if account.mobile is not None:
        try:
            diary = await account.mobile.diary(start, end)
            result = _from_diary(diary, start, end, source="mobile")
            student = account.mobile.current_student
            if not result["class"] and student:
                result["class"] = student.group_name or ""
            return result
        except netschoolpy.SessionExpired:
            log.info("Мобильный токен истёк — переходим на веб-API")
            account.mobile = None
        except netschoolpy.NetSchoolError as e:
            log.warning("Мобильный дневник не получен: %s", str(e)[:160])

    diary = await account.web.diary(start, end)
    result = _from_diary(diary, start, end, source="diary")
    if result["lessons"]:
        return result

    lessons, class_name = await _schedule_week(account, start, end)
    try:
        _attach_marks(lessons, await _marks(account))
    except ReportLimit:
        log.info("StudentTotal: лимит отчётов, оценки позже")
    except Exception:
        log.exception("StudentTotal report failed")
    # Задания предметов, которые уже открывали, — сразу в клетки, без новых отчётов.
    tasks = account.cache.get("tasks", {})
    for lesson in lessons:
        rows = tasks.get(lesson["sg"], (0, []))[1]
        lesson["assignments"] = [{**a, "mark": None} for a in rows if a["due"] == lesson["day"]]
    return {**result, "class": class_name, "lessons": lessons, "source": "schedule"}


def _hhmm(value: Any) -> str:
    return value.strftime("%H:%M") if value else ""


def _from_diary(diary: Any, start: dt.date, end: dt.date, *, source: str) -> dict:
    lessons = [
        {
            "day": lesson.day.isoformat() if lesson.day else None,
            "start": _hhmm(lesson.start),
            "end": _hhmm(lesson.end),
            "number": lesson.number,
            "subject": lesson.subject,
            "room": lesson.room,
            "teacher": _short_name(lesson.teacher) if lesson.teacher else "",
            "topic": lesson.topic,
            "absence": lesson.absence,
            "distance": lesson.is_distance,
            "elective": lesson.is_elective,
            "assignments": [
                {
                    "id": a.id,
                    "kind": a.kind,
                    "content": a.content,
                    "mark": a.mark_text or (str(a.mark) if a.mark is not None else None),
                    "due": a.deadline.isoformat() if a.deadline else None,
                }
                for a in lesson.assignments
            ],
        }
        for day in diary.schedule
        for lesson in day.lessons
    ]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "class": diary.class_name,
        "term": diary.term_name,
        "lessons": lessons,
        "source": source,
    }


def _short_name(full: str) -> str:
    """«Иванова Анна Петровна» -> «Иванова А.П.» — как на карточках макета."""
    parts = full.split()
    return parts[0] + " " + "".join(p[0] + "." for p in parts[1:]) if len(parts) > 1 else full


async def _schedule_week(account: Account, start: dt.date, end: dt.date) -> tuple[list[dict], str]:
    """Неделя из расписания: уроки, группы предметов и звонки — три запроса веб-API."""
    ns = account.web
    cache = account.cache
    if "groups" not in cache:
        resp = await ns._authed_get("subjectgroups", params={"studentId": ns._student_id, "yearId": ns._year_id})
        cache["groups"] = {g["id"]: g for g in resp.json()}
    if "times" not in cache:
        resp = await ns._authed_get("schedule/times")
        cache["times"] = {t["id"]: t for t in resp.json()}
    groups, times = cache["groups"], cache["times"]

    resp = await ns._authed_get(
        "schedule/classmeetings",
        # Сервер не включает день end в ответ — просим на день больше.
        params={"studentId": ns._student_id, "start": start.isoformat(),
                "end": (end + dt.timedelta(days=1)).isoformat()},
    )
    lessons = []
    for cm in resp.json():
        day = cm["day"][:10]
        if not start.isoformat() <= day <= end.isoformat():
            continue
        group = groups.get(cm["subjectGroupId"], {})
        bell = times.get(cm["scheduleTimeId"], {})
        subject = group.get("subject") or {}
        lessons.append({
            "day": day,
            "start": (bell.get("startTime") or "T00:00")[11:16],
            "end": (bell.get("endTime") or "T00:00")[11:16],
            "number": cm.get("number", 0),
            "subject": subject.get("name") or group.get("name") or "Урок",
            "room": "",
            "teacher": ", ".join(_short_name(t["name"]) for t in group.get("teachers") or []),
            "topic": "",
            "absence": "",
            "distance": False,
            "elective": bool(group.get("extraCurricular")),
            "group": group.get("name") or "",
            "sg": cm["subjectGroupId"],
            "assignments": [],
        })
    lessons.sort(key=lambda l: (l["day"], l["start"], l["number"]))
    class_name = next(
        (g["extName"] for g in groups.values() if not g.get("extraCurricular") and g.get("extName")), ""
    )
    return lessons, class_name


# ───────────────────────── отчёты ─────────────────────────

_report_locks: dict[tuple[str, int], asyncio.Lock] = {}


async def _report(account: Account, report_id: str, filters: dict) -> Any:
    """Отчёты ученика — строго по одному: параллельные сервер отклоняет."""
    ns = account.web
    lock = _report_locks.setdefault((account.url, ns._student_id), asyncio.Lock())
    async with lock:
        try:
            return await ns.report(report_id, filters)
        except Exception as e:
            # Повторять бессмысленно: каждый повтор тоже идёт в счёт лимита.
            if "409" in str(e):
                raise ReportLimit from e
            raise


class _TableParser(HTMLParser):
    """Строки таблиц отчёта с colspan: [(текст, colspan), ...]."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[tuple[str, int]]] = []
        self.text: list[str] = []
        self._row: list[tuple[str, int]] | None = None
        self._cell: list[str] | None = None
        self._span = 1

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._span = [], int(dict(attrs).get("colspan") or 1)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append((" ".join("".join(self._cell).split()), self._span))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)


def parse_total_marks(html: str) -> dict[tuple[str, str], list[str]]:
    """{(полное название предмета, YYYY-MM-DD): ["5", "НП", ...]} из отчёта StudentTotal."""
    parser = _TableParser()
    parser.feed(html)
    m = re.search(r"Учебный год:\s*(\d{4})", "".join(parser.text))
    today = dt.date.today()
    first_year = int(m.group(1)) if m else today.year - (today.month < 8)
    header = next((i for i, r in enumerate(parser.rows) if r and r[0][0] == "Предмет"), None)
    if header is None or header + 1 >= len(parser.rows):
        return {}
    dates: list[str] = []
    days = iter(parser.rows[header + 1])
    for text, span in parser.rows[header][1:]:
        month = next((i + 1 for i, name in enumerate(MONTHS) if text.lower().startswith(name)), None)
        if month is None:
            break  # дальше — колонки средних и итоговых
        year = first_year if month >= 8 else first_year + 1
        for _ in range(span):
            day_text, _span = next(days, ("", 1))
            dates.append(dt.date(year, month, int(day_text)).isoformat() if day_text.isdigit() else "")
    marks: dict[tuple[str, str], list[str]] = {}
    for row in parser.rows[header + 2:]:
        if len(row) < len(dates) + 1:
            continue
        for day, (text, _span) in zip(dates, row[1:]):
            if day and text:
                marks[(row[0][0], day)] = text.split()
    return marks


async def _marks(account: Account) -> dict[tuple[str, str], list[str]]:
    cached = account.cache.get("marks")
    if cached and time.monotonic() - cached[0] < MARKS_TTL:
        return cached[1]
    try:
        report = await _report(account, "StudentTotal", {})
    except ReportLimit:
        if cached:
            return cached[1]  # лучше старые оценки, чем никаких
        raise
    marks = parse_total_marks(report.html)
    account.cache["marks"] = (time.monotonic(), marks)
    return marks


def _subject_key(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").rstrip(".").split())


def _attach_marks(lessons: list[dict], marks: dict[tuple[str, str], list[str]]) -> None:
    """Предмет из отчёта содержит название предмета урока; сдвоенный урок — оценки на первый."""
    used: set[tuple[str, str]] = set()
    for lesson in lessons:
        key = _subject_key(lesson["subject"])
        for (report_subject, day), tokens in marks.items():
            if day != lesson["day"] or (report_subject, day) in used:
                continue
            if key and key in _subject_key(report_subject):
                used.add((report_subject, day))
                lesson["marks"] = [t for t in tokens if t not in ATTENDANCE]
                lesson["absence"] = " ".join(t for t in tokens if t in ATTENDANCE)
                break


def _ru_date(text: str) -> str:
    """«11.09.2026» -> «2026-09-11»; пусто, если не дата."""
    try:
        d, m, y = (int(x) for x in text.split("."))
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return ""


async def group_tasks(account: Account, sg: int) -> list[dict]:
    """Все задания группы предмета за четверть (отчёт StudentGrades): домашка и контрольные."""
    cache = account.cache.setdefault("tasks", {})
    cached = cache.get(sg)
    if cached and time.monotonic() - cached[0] < TASKS_TTL:
        return cached[1]
    report = await _report(account, "StudentGrades", {"SGID": str(sg)})
    rows: list[dict] = []
    for table in report.tables():
        if not table or table[0][:2] != ["Тип задания", "Тема задания"]:
            continue
        for row in table[1:]:
            if len(row) < 5:
                continue
            mark = row[4].strip()
            rows.append({
                "kind": row[0],
                "content": row[1],
                "due": _ru_date(row[2]),
                "result": "" if mark in ("", "-") else mark,
            })
    cache[sg] = (time.monotonic(), rows)
    return rows


async def assignment(account: Account, assignment_id: int) -> dict:
    cards = account.cache.setdefault("cards", {})
    if assignment_id in cards:
        return cards[assignment_id]
    client = account.mobile or account.web
    d = await asyncio.wait_for(client.assignment_details(assignment_id), timeout=10)
    cards[assignment_id] = {
        "name": d.name,
        "description": d.description,
        "teacher": d.teacher,
        "attachments": [{"id": a.id, "name": a.name} for a in d.attachments],
    }
    return cards[assignment_id]


def me(account: Account) -> dict:
    student = (account.mobile or account.web).current_student
    return {
        "name": student.name if student else "",
        "class": (student.group_name or "") if student else "",
        "mode": account.mode,
    }

