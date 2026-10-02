"""Выгрузка ведомости успеваемости в XLSX (FR-72, BR-38, этап Э-11).

Файл собирается из того же контекста дашборда, что и экран, — сервис
не считает ничего своего и не читает базу: три листа повторяют сводку,
распределение по пяти статусам, четыре разреза (FR-60) и таблицу
успеваемости (FR-59) в границах области видимости и применённых
фильтров (BR-38). Пустая выборка даёт файл с заголовками и нулями, а
не отказ (по образцу выгрузки журнала, AC-32).

**Только значения, без формул и ссылок.** §4.2 запрещает вычисление
формул внутри ``.xlsx``, поэтому все ячейки — литералы, вычисленные на
сервере; внешних ссылок в файле нет (NFR-03).

**Имя файла (FR-72).** В него входят применённые фильтры в порядке
экрана — курс, группа, подстрока поиска по ФИО, — и дата выгрузения в
формате ``DD.MM.YYYY``; фильтр «Задание» в имя не входит. Пустой слот
пропускается целиком (без двойных ``_``), без фильтров имя —
``vedomost_full_DD.MM.YYYY``. Недопустимые символы заменяются на ``_``,
итоговая длина ограничена ``GRADEBOOK_FILENAME_MAX`` с сохранением
даты в конце — так кириллица остаётся кириллицей, а браузер получает
имя, допустимое в Windows.
"""

from __future__ import annotations

import io
import re
from datetime import date

from flask import Response, send_file
from openpyxl import Workbook
from openpyxl.styles import Font

from app import config

#: Листы файла в порядке чтения: показатели, разрезы, строки.
SHEET_SUMMARY = "Сводка"
SHEET_BREAKDOWNS = "Разрезы"
SHEET_ROWS = "Ведомость"

#: Заголовки таблицы успеваемости — те же пять колонок, что на экране
#: (FR-59): e-mail и пароли сюда не попадают по построению (BR-38).
ROW_HEADERS = ("ФИО", "Группа", "Задание", "Дедлайн", "Статус")

#: Подпись отсутствующего дедлайна — слово в слово как на экране.
NO_DUE_LABEL = "не задан"

#: Суффикс архивного задания в колонке «Задание» (чек-лист §12).
ARCHIVED_LABEL = " (архив)"

#: Символы, запрещённые в именах файлов Windows, плюс управляющие
#: символы: и то и другое заменяется на ``_`` (FR-72).
_FORBIDDEN_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_BOLD = Font(bold=True)


# --------------------------------------------------------------------------
# Содержимое листов
# --------------------------------------------------------------------------


def _write_summary(sheet, dashboard: dict) -> None:
    """Сводные показатели и распределение по пяти статусам (FR-55)."""
    sheet.append(["Ведомость успеваемости"])
    sheet["A1"].font = _BOLD
    sheet.append(["Область", dashboard["scope_title"]])
    sheet.append([])

    sheet.append(["Сводные показатели"])
    sheet.cell(row=sheet.max_row, column=1).font = _BOLD
    summary = dashboard["summary"]
    for title, key in (
        ("Студентов", "students"),
        ("Заданий", "assignments"),
        ("Работ", "works"),
        ("Сдано", "accepted"),
    ):
        sheet.append([title, summary[key]])
    sheet.append([])

    sheet.append(["Распределение работ по статусам"])
    sheet.cell(row=sheet.max_row, column=1).font = _BOLD
    sheet.append(["Статус", "Работ"])
    for column in (1, 2):
        sheet.cell(row=sheet.max_row, column=column).font = _BOLD
    for code in config.STATUS_ORDER:
        sheet.append([config.STATUSES[code]["label"], dashboard["counts"][code]])
    sheet.append(["Всего", dashboard["total"]])
    for column in (1, 2):
        sheet.cell(row=sheet.max_row, column=column).font = _BOLD

    sheet.column_dimensions["A"].width = 34
    sheet.column_dimensions["B"].width = 46


def _write_breakdown(sheet, dashboard: dict) -> None:
    """Четыре разреза прогресса (FR-60) — состав и порядок как на
    экране; пустой разрез даёт одну строку заголовка."""
    captions = (
        ("course", "Прогресс по курсам", "Курс"),
        ("group", "Прогресс по группам", "Группа"),
        ("assignment", "Прогресс по заданиям", "Задание"),
        ("student", "Прогресс по студентам", "Студент"),
    )
    status_labels = [config.STATUSES[code]["label"] for code in config.STATUS_ORDER]

    first = True
    for key, title, caption in captions:
        if not first:
            sheet.append([])
        first = False
        sheet.append([title])
        sheet.cell(row=sheet.max_row, column=1).font = _BOLD
        sheet.append([caption] + status_labels + ["Всего"])
        for column in range(1, len(status_labels) + 3):
            sheet.cell(row=sheet.max_row, column=column).font = _BOLD
        for row in dashboard["breakdowns"][key]:
            counts = [row["counts"][code] for code in config.STATUS_ORDER]
            sheet.append([row["label"]] + counts + [row["total"]])

    sheet.column_dimensions["A"].width = 34
    for index in range(2, len(status_labels) + 3):
        sheet.column_dimensions[chr(ord("A") + index - 1)].width = 16


def _write_rows(sheet, dashboard: dict) -> None:
    """Таблица успеваемости (FR-59): пять колонок экрана без e-mail."""
    sheet.append(list(ROW_HEADERS))
    for column in range(1, len(ROW_HEADERS) + 1):
        sheet.cell(row=sheet.max_row, column=column).font = _BOLD

    for row in dashboard["rows"]:
        assignment = row["assignment_title"]
        if row["is_archived"]:
            assignment += ARCHIVED_LABEL
        sheet.append(
            [
                row["student_name"],
                row["group_label"],
                assignment,
                row["due_label"] or NO_DUE_LABEL,
                row["status_label"],
            ]
        )

    widths = (30, 24, 34, 18, 22)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(ord("A") + index - 1)].width = width


def build_workbook(dashboard: dict) -> bytes:
    """Готовый файл XLSX тремя листами в порядке FR-72."""
    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = SHEET_SUMMARY
    breakdowns_sheet = workbook.create_sheet(SHEET_BREAKDOWNS)
    rows_sheet = workbook.create_sheet(SHEET_ROWS)

    _write_summary(summary_sheet, dashboard)
    _write_breakdown(breakdowns_sheet, dashboard)
    _write_rows(rows_sheet, dashboard)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Имя файла (FR-72)
# --------------------------------------------------------------------------


def _sanitize(slot: str) -> str:
    """Один слот имени: недопустимые символы → ``_``, края чистятся,
    включая подчёркивания — иначе «Иванов/» склеилось бы с разделителем
    в двойное ``_``. Пустой результат слот отбрасывает."""
    cleaned = _FORBIDDEN_CHARS.sub("_", slot)
    return cleaned.strip().strip("_")


def _applied_label(field: dict | None) -> str:
    """Применённое значение поля в том виде, в каком оно показано на
    экране: подпись выбранного варианта списка или введённый текст
    поиска. Пусто, если фильтр не задан или значение вне опций."""
    if field is None:
        return ""
    value = field.get("value")
    if value is None or value == "":
        return ""
    if field.get("type") == "search":
        return _sanitize(str(value))
    for option in field.get("options", []):
        if option.get("value") == value:
            return _sanitize(str(option.get("label", "")))
    return ""


def build_filename(dashboard: dict, today: date | None = None) -> str:
    """Имя файла выгрузки по применённым фильтрам (FR-72).

    Слоты идут в порядке экрана — курс, группа, поиск по ФИО; фильтр
    «Задание» не участвует. Пустые и полностью вычищенные слоты
    пропускаются, поэтому двойных подчёркиваний не бывает. Дата в
    конце сохраняется и при обрезке: усекается только головная часть.
    """
    stamp = (today or date.today()).strftime("%d.%m.%Y")
    fields = {item["key"]: item for item in dashboard["filters"]["fields"]}

    slots = [
        label
        for label in (
            _applied_label(fields.get("course")),
            _applied_label(fields.get("group")),
            _applied_label(fields.get("student")),
        )
        if label
    ]
    head = (
        "_".join((config.GRADEBOOK_FILENAME_PREFIX, *slots))
        if slots
        else "_".join((config.GRADEBOOK_FILENAME_PREFIX, config.GRADEBOOK_FILENAME_EMPTY))
    )

    extension = ".xlsx"
    tail = "_" + stamp
    limit = config.GRADEBOOK_FILENAME_MAX - len(extension)
    if len(head) + len(tail) > limit:
        head = head[: max(1, limit - len(tail))].rstrip("_ ")
        if not head:
            head = config.GRADEBOOK_FILENAME_PREFIX
    return head + tail + extension


# --------------------------------------------------------------------------
# Отдача браузеру
# --------------------------------------------------------------------------


def download(dashboard: dict) -> Response:
    """Ответ Flask с файлом ведомости: браузер показывает диалог
    сохранения (FR-72), ``download_name`` уходит в RFC 5987 ``filename*``
    — так кириллица в имени переживает заголовок."""
    return send_file(
        io.BytesIO(build_workbook(dashboard)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=build_filename(dashboard),
    )
