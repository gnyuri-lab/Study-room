"""Выгрузка ведомости успеваемости (FR-72, BR-38, AC-46, AC-47).

Проверяются решения этапа Э-11, а не «файл скачался»:

* файл повторяет экран: три листа, сводка и пять статусов совпадают
  с дашбордом, пустая выборка даёт файл с заголовками и нулями (FR-72);
* ссылка несёт применённые фильтры, и в файле оказываются ровно они
  же — как у выгрузки журнала (BR-38, AC-46);
* куратор выгружает только свои группы, чужой студент в файл не
  попадает (BR-38, AC-47);
* доступ: студенту — 403, анониму — редирект на вход (FR-07, BR-33);
* имя файла: фильтры в порядке экрана, без фильтра «Задание», пустые
  слоты пропускаются, дата DD.MM.YYYY, санитизация и обрезка (FR-72);
* выгрузка не пишется в журнал: действие не имеет кода §7.12 (FR-72).
"""

from __future__ import annotations

import io
import re
from datetime import date
from urllib.parse import unquote

import pytest
from openpyxl import load_workbook

from app import config, passwords
from app.services import gradebook
from tests.test_dashboard import as_admin, as_curator, as_student, world

ADMIN_URL = "/admin/dashboard"
CURATOR_URL = "/curator/dashboard"
ADMIN_EXPORT = "/admin/dashboard/export"
CURATOR_EXPORT = "/curator/dashboard/export"

TODAY = date(2026, 10, 2)
STAMP = "02.10.2026"

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


# --------------------------------------------------------------------------
# Разбор файла
# --------------------------------------------------------------------------


def workbook_of(response):
    """Книга XLSX из ответа сервера — по трем листам FR-72."""
    assert response.status_code == 200, response.status_code
    assert response.mimetype == CONTENT_TYPE, response.mimetype
    book = load_workbook(io.BytesIO(response.data))
    assert book.sheetnames == [
        gradebook.SHEET_SUMMARY,
        gradebook.SHEET_BREAKDOWNS,
        gradebook.SHEET_ROWS,
    ]
    return book


def summary_values(book):
    """Подпись → число с листа «Сводка» (последнее вхождение главно:
    строки идут сверху вниз, «Сдано» сводки и «Сдано» статуса равны)."""
    values = {}
    for row in book[gradebook.SHEET_SUMMARY].iter_rows(values_only=True):
        if row[0] is not None and len(row) > 1:
            values[row[0]] = row[1]
    return values


def sheet_rows(book, sheet_name):
    return list(book[sheet_name].iter_rows(values_only=True))


def download_name(response):
    """Имя файла из заголовка ``Content-Disposition``: кириллица уходит
    в ``filename*`` (RFC 5987), чистое ASCII — в обычный ``filename``."""
    header = response.headers["Content-Disposition"]
    assert header.startswith("attachment"), header
    match = re.search(r"filename\*=utf-8''([^;]+)", header, re.IGNORECASE)
    if match:
        return unquote(match.group(1))
    match = re.search(r'filename="?([^";]+)"?', header, re.IGNORECASE)
    assert match is not None, header
    return match.group(1)


# --------------------------------------------------------------------------
# Имя файла (FR-72): слоты, санитизация, обрезка
# --------------------------------------------------------------------------


def make_payload(course=None, group=None, assignment=None, student=""):
    """Минимальный контекст дашборда для сборки имени файла."""
    fields = [
        {
            "key": "course",
            "type": "select",
            "label": "Курс",
            "empty": "Все курсы",
            "value": course,
            "options": [
                {"value": 1, "label": "ФК-1"},
                {"value": 2, "label": "ФК-2"},
            ],
        },
        {
            "key": "group",
            "type": "select",
            "label": "Группа",
            "empty": "Все группы",
            "value": group,
            "options": [
                {"value": 11, "label": "ФК-11"},
                {"value": 12, "label": "ФК-12"},
            ],
        },
        {
            "key": "assignment",
            "type": "select",
            "label": "Задание",
            "empty": "Все задания",
            "value": assignment,
            "options": [{"value": 5, "label": "Домашняя работа 1"}],
        },
        {
            "key": "student",
            "type": "search",
            "label": "Поиск по имени",
            "placeholder": "Часть ФИО",
            "value": student,
            "options": [],
        },
    ]
    return {"filters": {"fields": fields, "active": bool(course or group or student)}}


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # Все три слота: курс, группа, подстрока ФИО и дата (пример ТЗ).
        (dict(course=1, group=11, student="Иванов"),
         f"vedomost_ФК-1_ФК-11_Иванов_{STAMP}.xlsx"),
        # Только курс: пустые слоты пропускаются целиком.
        (dict(course=1), f"vedomost_ФК-1_{STAMP}.xlsx"),
        # Курс и группа, поиск пуст.
        (dict(course=1, group=11), f"vedomost_ФК-1_ФК-11_{STAMP}.xlsx"),
        # Без фильтров — отдельный шаблон имени.
        (dict(), f"vedomost_full_{STAMP}.xlsx"),
        # Фильтр «Задание» в имя файла не входит (FR-72).
        (dict(assignment=5), f"vedomost_full_{STAMP}.xlsx"),
        # Недопустимые символы заменяются, кириллица сохраняется.
        (dict(course=1, student="Иванов/Петров?"),
         f"vedomost_ФК-1_Иванов_Петров_{STAMP}.xlsx"),
        # Поиск без курса и группы: один слот и дата.
        (dict(student="Иванов"), f"vedomost_Иванов_{STAMP}.xlsx"),
    ],
)
def test_filename_composition(args, expected):
    payload = make_payload(**args)
    assert gradebook.build_filename(payload, today=TODAY) == expected


def test_filename_is_truncated_but_keeps_date():
    payload = make_payload(course=1, group=11, student="Иванов" * 40)
    name = gradebook.build_filename(payload, today=TODAY)

    assert name.endswith(f"_{STAMP}.xlsx")
    assert len(name) <= config.GRADEBOOK_FILENAME_MAX


def test_filename_uses_current_date_when_not_given():
    payload = make_payload()
    name = gradebook.build_filename(payload)

    assert name == f"vedomost_full_{date.today().strftime('%d.%m.%Y')}.xlsx"


# --------------------------------------------------------------------------
# Файл целиком (FR-72): листы, сводка, пустая выборка
# --------------------------------------------------------------------------


def test_export_repeats_dashboard_numbers(client, world):
    as_admin(client)
    book = workbook_of(client.get(ADMIN_EXPORT))

    values = summary_values(book)
    assert values["Область"] == "Все курсы"
    assert values["Студентов"] == 3
    assert values["Заданий"] == 3
    assert values["Работ"] == 5
    assert values["Сдано"] == 1

    assert values[config.STATUSES["not_submitted"]["label"]] == 2
    assert values[config.STATUSES["submitted"]["label"]] == 1
    assert values[config.STATUSES["revision"]["label"]] == 1
    assert values[config.STATUSES["accepted"]["label"]] == 1
    assert values[config.STATUSES["accepted_late"]["label"]] == 0
    assert values["Всего"] == 5

    breakdowns = sheet_rows(book, gradebook.SHEET_BREAKDOWNS)
    titles = {row[0] for row in breakdowns if row[0]}
    assert {
        "Прогресс по курсам",
        "Прогресс по группам",
        "Прогресс по заданиям",
        "Прогресс по студентам",
    } <= titles

    rows = sheet_rows(book, gradebook.SHEET_ROWS)
    assert list(rows[0]) == list(gradebook.ROW_HEADERS)
    assert len(rows) == 1 + 5
    # Таблица без e-mail и паролей (BR-38, AC-46).
    cells = [str(cell) for row in rows for cell in row if cell is not None]
    assert not any("@" in cell for cell in cells)


def test_export_of_empty_selection_is_still_a_valid_file(client, world):
    as_admin(client)
    response = client.get(
        ADMIN_EXPORT, query_string={"student": "Несуществующее Имя"}
    )
    book = workbook_of(response)

    values = summary_values(book)
    assert values["Работ"] == 0
    assert values["Всего"] == 0
    assert values[config.STATUSES["accepted"]["label"]] == 0
    # Заголовки на месте, строк данных нет.
    assert list(sheet_rows(book, gradebook.SHEET_ROWS)[0]) == list(
        gradebook.ROW_HEADERS
    )
    assert len(sheet_rows(book, gradebook.SHEET_ROWS)) == 1
    # Пустая выборка тоже даёт осмысленное имя с фильтром.
    assert "НесуществующееИмя" in download_name(response).replace(" ", "")


def test_dashboard_has_export_button_with_current_filters(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL, query_string={"student": "Второй"}).text

    assert "Выгрузить ведомость" in html
    assert 'href="/admin/dashboard/export?student=' in html

    html = client.get(ADMIN_URL).text
    assert 'href="/admin/dashboard/export"' in html


# --------------------------------------------------------------------------
# Фильтры и область видимости (BR-38, AC-46, AC-47)
# --------------------------------------------------------------------------


def test_export_repeats_applied_filters(client, world):
    as_admin(client)
    response = client.get(
        ADMIN_EXPORT,
        query_string={"course": world["course"], "student": "Второй"},
    )
    book = workbook_of(response)

    rows = sheet_rows(book, gradebook.SHEET_ROWS)
    assert len(rows) == 1 + 2
    assert all("Второй" in row[0] for row in rows[1:])
    assert not any("Первый" in row[0] for row in rows[1:])

    values = summary_values(book)
    assert values["Студентов"] == 1
    assert values["Работ"] == 2

    # Название курса в имени файла — тот же, что показан в фильтре.
    name = download_name(response)
    assert name.startswith("vedomost_")
    assert "Основной курс" in name
    assert name.endswith(f"_{date.today().strftime('%d.%m.%Y')}.xlsx")


def test_curator_export_is_limited_to_own_groups(client, world):
    as_curator(client)
    book = workbook_of(client.get(CURATOR_EXPORT))

    values = summary_values(book)
    assert values["Область"] == "Мои группы"
    assert values["Студентов"] == 2
    assert values["Работ"] == 4
    assert values["Сдано"] == 1

    rows = sheet_rows(book, gradebook.SHEET_ROWS)
    assert len(rows) == 1 + 4
    joined = " ".join(str(cell) for row in rows for cell in row)
    assert "Студент Чужой" not in joined
    assert "ФК-9" not in joined
    assert not any("@" in str(cell) for row in rows for cell in row if cell)


def test_curator_export_without_filters_uses_full_name(client, world):
    as_curator(client)
    name = download_name(client.get(CURATOR_EXPORT))

    assert name == f"vedomost_full_{date.today().strftime('%d.%m.%Y')}.xlsx"


# --------------------------------------------------------------------------
# Доступ (FR-07, BR-33) и журнал (FR-72)
# --------------------------------------------------------------------------


def test_student_cannot_export(client, world):
    as_student(client)

    assert client.get(ADMIN_EXPORT).status_code == 403
    assert client.get(CURATOR_EXPORT).status_code == 403


def test_anonymous_is_redirected_to_login(client, world):
    response = client.get(ADMIN_EXPORT)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_export_is_not_written_to_action_log(client, world, connection):
    as_admin(client)
    before = connection.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]

    assert client.get(ADMIN_EXPORT).status_code == 200
    assert client.get(ADMIN_EXPORT, query_string={"student": "Второй"}).status_code == 200

    after = connection.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
    assert after == before
