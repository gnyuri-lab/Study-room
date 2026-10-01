"""Чтение файла списка и квалификация его строк (FR-24, FR-26, FR-31,
BR-01…BR-09).

Модуль ничего не записывает в базу: он только читает файл и говорит,
какие строки годятся и почему остальные не годятся. Записью занимается
``importer`` — и только после подтверждения администратора (FR-25).

Разделение сделано ради одного свойства: **предпросмотр и подтверждение
обязаны считать одно и то же**. Оба шага вызывают ``qualify`` с одними и
теми же аргументами, поэтому отчёт не может разойтись с тем, что реально
попало в базу.

Три неочевидных места, на которые стоит посмотреть перед правкой:

* **Номера строк.** В отчёте и в ``import_errors.row_number`` пишется
  номер строки в файле, а не номер записи в списке (FR-27). Первая
  строка файла — заголовок, поэтому нумерация данных начинается с 2;
  это же зафиксировано ``CHECK (row_number >= 2)`` в схеме.
* **Пустые строки не считаются данными.** Строка, в которой пусты все
  ячейки, пропускается целиком: иначе пустая строка в конце файла
  попала бы в отчёт как отклонённая сразу по пяти причинам.
* **Формула пароля.** В реестрах пароль хранится формулой
  ``=LEFT(D2,4)&F2`` (FR-31). Сначала берётся вычисленное Excel значение,
  и только если его нет — вычисляется сам шаблон. Всё остальное — BR-08.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from typing import Any, Iterable

from app import config, passwords
from app.services import ServiceError

#: Столбцы реестра в порядке §7.14. Порядок важен только для отчёта.
ROSTER_FIELDS = ("full_name", "role", "group", "email", "password")

#: Имена полей в сообщениях — те же, что в §7.14 и в заголовке файла.
FIELD_LABELS = {
    "full_name": "ФИО",
    "role": "роль",
    "group": "группа",
    "email": "e-mail",
    "password": "пароль",
}

#: Допустимые названия столбцов заголовка. Ключ — нормализованный
#: заголовок, значение — поле реестра. Нормализация убирает пробелы,
#: точки и дефисы, поэтому «Ф.И.О.», «Ф И О» и «фио» совпадают.
HEADER_FIELDS = {
    "фио": "full_name",
    "роль": "role",
    "группа": "group",
    "email": "email",
    "почта": "email",
    "пароль": "password",
}

#: Поддерживаемый шаблон формулы пароля: ``=LEFT(D2,4)&F2``.
#: Ссылки должны указывать на ту же строку, в которой стоит формула:
#: иначе это уже не «пароль из четырёх первых символов логина плюс
#: суффикс», а произвольная формула (BR-08).
_FORMULA_PATTERN = re.compile(
    r"^=\s*LEFT\s*\(\s*([A-Z]{1,3})\s*(\d{1,7})\s*,\s*4\s*\)"
    r"\s*&\s*([A-Z]{1,3})\s*(\d{1,7})\s*$",
    re.IGNORECASE,
)

#: Разделители для CSV: запятая, точка с запятой, табуляция.
_CSV_DELIMITERS = (",", ";", "\t")

#: Кодировки CSV: сначала UTF-8 (в том числе с BOM), затем cp1251 —
#: русский Excel по умолчанию пишет именно его (чек-лист §6.4).
_CSV_ENCODINGS = ("utf-8-sig", "cp1251")

#: Тексты ошибок BR-01…BR-08 дословно из §8.1. Живут здесь, а не в
#: ``config.MESSAGES``, потому что §9.3 перечисляет «ключевые» тексты и
#: не содержит сообщений поимпортной квалификации; в config лежат
#: только тексты, нужные интерфейсу импорта как таковому.
BR_MESSAGES = {
    "BR-01": "Не заполнено поле: {label}",
    "BR-02": "Недопустимая роль: {value}. Допустимо: студент или куратор",
    "BR-03": "Некорректный e-mail: отсутствует символ @",
    "BR-04": "Некорректный e-mail: не указан домен",
    "BR-05": "e-mail уже используется в строке {row}",
    "BR-06": "Этот e-mail уже зарегистрирован в курсе",
    "BR-07": "Пароль должен содержать не менее 5 символов",
    "BR-08": "Не удалось получить значение пароля: неподдерживаемая формула",
}


class RosterError(ServiceError):
    """Проблема с файлом целиком: читать строки нельзя.

    Отдельный подкласс нужен маршруту: такой отказ относится к файлу и
    показывается без номера строки, тогда как отказ по BR-01…BR-08
    относится к строке.
    """


# --------------------------------------------------------------------------
# Чтение файла
# --------------------------------------------------------------------------


def read_roster(path, *, file_name: str) -> dict[str, Any]:
    """Прочитать ``.xlsx`` или ``.csv`` в строки реестра.

    Возвращает ``{"file_name": ..., "header": {...}, "rows": [...]}``,
    где ``header`` отображает поле в номер столбца (с нуля), а строка —
    ``{"row_number", "cells", "cached"}``. В ``cached`` лежат вычисленные
    значения формул: в ``.xlsx`` их возвращает Excel, в ``.csv`` их нет
    вовсе, и пароль приходится вычислять самим.
    """
    extension = extension_of(file_name)
    if extension == "xlsx":
        header_cells, rows = _read_xlsx(path)
    elif extension == "csv":
        header_cells, rows = _read_csv(path)
    else:
        raise RosterError(config.MESSAGES["roster_extension"])

    header = _build_header(header_cells)
    if not rows:
        raise RosterError(config.MESSAGES["no_roster_rows"])
    if len(rows) > config.IMPORT_ROSTER_MAX_ROWS:
        raise RosterError(
            config.MESSAGES["roster_too_many_rows"].format(
                limit=config.IMPORT_ROSTER_MAX_ROWS
            )
        )
    return {"file_name": file_name, "header": header, "rows": rows}


def extension_of(file_name: str) -> str:
    """Расширение по имени файла, приведённое к нижнему регистру."""
    cleaned = (file_name or "").strip()
    if "." not in cleaned:
        return ""
    return cleaned.rsplit(".", 1)[-1].lower()


def _read_xlsx(path) -> tuple[list[Any], list[dict[str, Any]]]:
    """Первая страница книги: значения и формулы одной и той же книги."""
    import openpyxl

    try:
        formulas = openpyxl.load_workbook(path, data_only=False)
        values = openpyxl.load_workbook(path, data_only=True)
    except Exception as failure:  # openpyxl бросает свой набор исключений
        raise RosterError(config.MESSAGES["roster_unreadable"]) from failure

    try:
        sheet_formulas = formulas.worksheets[0]
        sheet_values = values[sheet_formulas.title]
    except (IndexError, KeyError) as failure:
        raise RosterError(config.MESSAGES["roster_unreadable"]) from failure

    last_row = sheet_formulas.max_row or 0
    if last_row < 1:
        return [], []
    header_cells = [cell.value for cell in sheet_formulas[1]]
    rows = []
    for number in range(2, last_row + 1):
        cells = [cell.value for cell in sheet_formulas[number]]
        if _is_blank_row(cells):
            continue
        cached = [cell.value for cell in sheet_values[number]]
        rows.append(
            {"row_number": number, "cells": cells, "cached": _pad(cached, cells)}
        )
    return header_cells, rows


def _read_csv(path) -> tuple[list[Any], list[dict[str, Any]]]:
    """Текстовый файл: разделитель определяем по строке заголовка."""
    try:
        text = _decode_csv(path.read_bytes())
    except RosterError:
        raise
    except OSError as failure:
        raise RosterError(config.MESSAGES["roster_unreadable"]) from failure

    lines = text.splitlines()
    if not any(line.strip() for line in lines):
        return [], []
    delimiter = _detect_delimiter(lines[0])
    # Строки берутся тем же разбиением, что и заголовок: ``io.StringIO``
    # с ``newline=""`` разрывает «\r\n» на две строки и вставил бы пустую
    # запись, сдвинув все номера строк в отчёте.
    all_rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
    if not all_rows:
        return [], []
    header_cells = all_rows[0]
    rows = []
    for number, cells in enumerate(all_rows[1:], start=2):
        if _is_blank_row(cells):
            continue
        # Вычисленных значений у CSV нет: в cells лежит сам текст, и если
        # он начинается с «=», это либо поддерживаемый шаблон, либо BR-08.
        rows.append({"row_number": number, "cells": cells, "cached": []})
    return header_cells, rows


def _decode_csv(raw: bytes) -> str:
    for encoding in _CSV_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RosterError(config.MESSAGES["roster_unreadable"])


def _detect_delimiter(line: str) -> str:
    """Запятая, точка с запятой или табуляция — по строке заголовка.

    Разделитель определяется по заголовку, а не по всему файлу: в
    заголовке названия столбцов, запятых в нём быть не может, а в
    данных они могут встретиться внутри названий групп.
    """
    counts = [(line.count(mark), mark) for mark in _CSV_DELIMITERS]
    best = max(counts, key=lambda pair: pair[0])
    return best[1] if best[0] else ","


def _build_header(cells: Iterable[Any]) -> dict[str, int]:
    """Найти номера столбцов по строке заголовка.

    Все пять столбцов обязательны: реестр без столбца «пароль» — это
    не список участников, и молча импортировать его нельзя. Сообщение
    то же, что и для отсутствующего заголовка (``no_roster_header``):
    в обоих случаях файл не пригоден для чтения. Лишние столбцы
    игнорируются — так игнорируется служебный столбец F с суффиксом
    пароля (§7.14).
    """
    header: dict[str, int] = {}
    for index, value in enumerate(cells):
        field = HEADER_FIELDS.get(_normalise_header(value))
        if field is not None and field not in header:
            header[field] = index
    if any(field not in header for field in ROSTER_FIELDS):
        raise RosterError(config.MESSAGES["no_roster_header"])
    return header


def _normalise_header(value: Any) -> str:
    """«Ф.И.О.», «Ф И О», «E-mail» → «фио», «email»."""
    text = _cell_text(value).lower().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def _cell_text(value: Any) -> str:
    """Значение ячейки как строка с обрезанными краями."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _is_blank_row(cells: Iterable[Any]) -> bool:
    return not any(_cell_text(value) for value in cells)


def _pad(cached: list[Any], cells: list[Any]) -> list[Any]:
    """Выровнять длины списков: openpyxl иногда отдаёт разные."""
    if len(cached) < len(cells):
        cached = list(cached) + [None] * (len(cells) - len(cached))
    return list(cached)


# --------------------------------------------------------------------------
# Формула пароля (FR-31)
# --------------------------------------------------------------------------


def _column_index(letters: str) -> int:
    """«A» → 1, «D» → 4, «AA» → 27 (нумерация как в Excel)."""
    total = 0
    for letter in letters.upper():
        total = total * 26 + (ord(letter) - 64)
    return total


def _cell_at(cells: Iterable[Any], column: int) -> Any:
    """Значение ячейки в столбце ``column`` (нумерация с единицы)."""
    index = column - 1
    if index < 0:
        return None
    for position, value in enumerate(cells):
        if position == index:
            return value
    return None


def _resolve_formula(formula: str, cells: list[Any], row_number: int) -> str | None:
    """Вычислить ``=LEFT(D2,4)&F2`` по значениям этой же строки.

    Возвращает ``None``, если шаблон не поддерживается: это BR-08, а не
    пустой пароль. Пустая ячейка, на которую ссылается формула, даёт
    пустую строку — дальше сработает обычная проверка BR-01/BR-07.
    """
    match = _FORMULA_PATTERN.match(formula.strip())
    if match is None:
        return None
    left_letter, left_row, right_letter, right_row = match.groups()
    if int(left_row) != row_number or int(right_row) != row_number:
        return None
    left = _cell_text(_cell_at(cells, _column_index(left_letter)))
    right = _cell_text(_cell_at(cells, _column_index(right_letter)))
    # Ссылка на формулу, а не на значение, вычислить нечем.
    if left.startswith("=") or right.startswith("="):
        return None
    return left[:4] + right


# --------------------------------------------------------------------------
# Квалификация строк (BR-01…BR-09)
# --------------------------------------------------------------------------


def qualify(
    roster: dict[str, Any],
    *,
    course_id: int,
    connection: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Оценить строки реестра и вернуть вердикт по каждой.

    Возвращает список того же порядка и той же длины, что ``rows``:
    ``{"row_number", "values", "errors", "accepted"}``. Пустой список
    ошибок означает BR-09 — строка квалифицирована.

    Проверки BR-05 и BR-06 обращаются к базе, но ничего в ней не
    меняют: предпросмотр должен быть безопасным (FR-25, AC-23).
    ``connection`` можно передать явно — этим пользуются тесты, которым
    нужен результат без приложения Flask.
    """
    if connection is None:
        from app import db

        connection = db.get_db()
    taken_emails = _emails_in_course(connection, course_id)
    group_names = _group_names_in_course(connection, course_id)
    seen_in_file: dict[str, int] = {}
    return [
        _qualify_row(
            roster["header"],
            row,
            taken_emails=taken_emails,
            group_names=group_names,
            seen_in_file=seen_in_file,
        )
        for row in roster["rows"]
    ]


def _qualify_row(
    header: dict[str, int],
    row: dict[str, Any],
    *,
    taken_emails: set[str],
    group_names: dict[str, bool],
    seen_in_file: dict[str, int],
) -> dict[str, Any]:
    """Вердикт по одной строке: все правила BR-01…BR-09 сразу (§8.1)."""
    values, unreadable_password = _row_values(header, row)
    errors: list[dict[str, str]] = []

    # BR-01. Для нечитаемой формулы пароля пропускаем и пустоту, и длину:
    # значение неизвестно, его нельзя объявлять пустым.
    for field in ROSTER_FIELDS:
        if field == "password" and unreadable_password:
            continue
        if not values[field]:
            errors.append(_issue(field, BR_MESSAGES["BR-01"]))

    # BR-02. Сравнение не по регистру и без лишних пробелов (§7.14).
    if values["role"] and role_code(values["role"]) is None:
        errors.append(
            _issue("role", BR_MESSAGES["BR-02"], value=values["role"])
        )

    email_usable = False
    email_key = ""
    email = values["email"]
    if email:
        # Порядок важен: BR-03 говорит про символ @, BR-04 — про домен
        # после него. Сначала ищем @, иначе «ivanov.example.ru» получил бы
        # неверную причину «не указан домен».
        if "@" not in email:
            errors.append(_issue("email", BR_MESSAGES["BR-03"]))
        elif not email.partition("@")[2]:
            errors.append(_issue("email", BR_MESSAGES["BR-04"]))
        elif passwords.validate_email(passwords.normalise_email(email)) is not None:
            # Формат §7.3 строже двух правил выше: «ivanov@localhost» и
            # «@example.ru» не годятся. Текст тот же, что у формы (§9.3).
            errors.append(
                _issue("email", config.MESSAGES["email_invalid"])
            )
        else:
            email_usable = True
            # Уникальность регистронезависима (BR-06): normalise_email
            # только обрезает края, регистр приводит вниз именно здесь —
            # так же, как это делает _emails_in_course для базы.
            email_key = passwords.normalise_email(email).lower()

    # BR-05. Первое вхождение в файле. Строка занимает e-mail, даже если
    # сама отклонена по другой причине: e-mail считается зантым в файле
    # независимо от того, дойдёт ли строка до базы (AC-25, строка 5).
    if email_usable:
        first_row = seen_in_file.get(email_key)
        if first_row is None:
            seen_in_file[email_key] = row["row_number"]
        else:
            errors.append(
                _issue("email", BR_MESSAGES["BR-05"], row=first_row)
            )

    # BR-06. Занятый в курсе e-mail отклоняется, чтобы повторный импорт
    # не создавал дублей (FR-30, AC-27). Уникальность составная и
    # регистронезависимая (BR-06 §8), поэтому другой курс не мешает.
    if email_usable and email_key in taken_emails:
        errors.append(_issue("email", BR_MESSAGES["BR-06"]))

    # BR-07 и BR-08.
    if unreadable_password:
        errors.append(_issue("password", BR_MESSAGES["BR-08"]))
    elif len(values["password"]) < config.PASSWORD_MIN_LENGTH_DEFAULT:
        errors.append(_issue("password", BR_MESSAGES["BR-07"]))

    # Лимиты §7.1–§7.2 теми же текстами, что и у формы участников. Без
    # них предпросмотр показал бы «принято», а подтверждение упало бы на
    # CHECK в схеме. Правил BR-01…BR-09 они не заменяют, а дополняют.
    if len(values["full_name"]) > config.FULL_NAME_MAX_LENGTH:
        errors.append(
            _issue("full_name", config.MESSAGES["full_name_too_long"])
        )
    if len(values["group"]) > config.GROUP_NAME_MAX_LENGTH:
        errors.append(
            _issue("group", config.MESSAGES["group_name_too_long"])
        )
    if values["group"] and group_names.get(values["group"]):
        # Имя совпадает с архивной группой: войти в неё нельзя (FR-23), а
        # создать вторую группу с тем же именем нельзя (UNIQUE в §7.5).
        errors.append(_issue("group", config.MESSAGES["group_name_taken"]))

    return {
        "row_number": row["row_number"],
        "values": values,
        "errors": errors,
        "accepted": not errors,
    }


def _row_values(
    header: dict[str, int], row: dict[str, Any]
) -> tuple[dict[str, str], bool]:
    """Значения пяти полей строки и признак нечитаемой формулы пароля."""
    cells = row["cells"]
    cached = row.get("cached") or []

    def cell(field: str, source: list[Any]) -> str:
        return _cell_text(_cell_at(source, header[field] + 1))

    raw_password = cell("password", cells)
    unreadable = False
    password_text = raw_password
    if raw_password.startswith("="):
        cached_password = cell("password", cached)
        if cached_password and not cached_password.startswith("="):
            # Excel посчитал формулу сам — берём его результат (FR-31).
            password_text = cached_password
        else:
            computed = _resolve_formula(raw_password, cells, row["row_number"])
            if computed is None:
                unreadable = True
                password_text = ""
            else:
                password_text = computed

    values = {
        field: password_text if field == "password" else cell(field, cells)
        for field in ROSTER_FIELDS
    }
    return values, unreadable


def _emails_in_course(connection: sqlite3.Connection, course_id: int) -> set[str]:
    """Занятые в курсе e-mail, включая отключённые учётные записи (В-01)."""
    rows = connection.execute(
        "SELECT email FROM users WHERE course_id = ?", (course_id,)
    ).fetchall()
    return {row["email"].lower() for row in rows}


def _group_names_in_course(
    connection: sqlite3.Connection, course_id: int
) -> dict[str, bool]:
    """Имя группы → архивна ли она."""
    rows = connection.execute(
        "SELECT name, archived_at FROM groups WHERE course_id = ?", (course_id,)
    ).fetchall()
    return {row["name"]: row["archived_at"] is not None for row in rows}


def role_code(role: str) -> str | None:
    """«Студент», « студент » → ``student``; неизвестное значение → None."""
    return config.ROSTER_ROLE_LABELS.get(role.strip().lower())


def _issue(field: str, template: str, **substitutions: Any) -> dict[str, str]:
    """Одна причина отклонения: имя поля и готовый текст.

    ``label`` подставляется всегда — он есть в BR-01, и текст без него
    бесполезен администратору. Остальные подстановки (значение роли,
    номер строки) приходят из ``substitutions``; ``str.format`` берёт их
    из kwargs и не жалуется на лишний ``label``, если шаблон его не
    упоминает.
    """
    label = FIELD_LABELS[field]
    return {
        "field": label,
        "text": template.format(label=label, **substitutions),
    }
