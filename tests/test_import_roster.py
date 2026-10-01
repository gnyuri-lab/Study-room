"""Чтение файла списка и квалификация строк (FR-24, FR-26, FR-31, BR-01…BR-09).

Модуль проверяет то, что не видно в сценарии целиком: правила по
отдельности, шаблон формулы пароля, CSV и границы файла. Сквозные AC
(AC-23…AC-27, AC-38, AC-39) живут в ``test_import_fixtures.py`` — здесь
оценка вызывается напрямую, без веб-приложения и без клиента.

``read_roster`` не требует базы вовсе, а ``qualify`` — только читает из
неё (BR-06), поэтому половина тестов обходится фикстурой ``connection``
без ``app``. Так видно, что модуль ничего не пишет.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import openpyxl
import pytest

from app import config, passwords
from app.services import ServiceError, roster_validation as rv
from tests.conftest import insert_course, insert_group, insert_user

HEADER = ("ФИО", "роль", "группа", "e-mail", "пароль")


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def course_id(connection):
    return insert_course(connection, title="Курс импорта")


def write_xlsx(path: Path, rows, header=HEADER) -> Path:
    """Книга с одной страницей: то, что администратор приносит из Excel."""
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    book.save(path)
    return path


def write_csv(path: Path, rows, header=HEADER, delimiter=";") -> Path:
    lines = [delimiter.join(header)]
    for row in rows:
        lines.append(delimiter.join("" if cell is None else str(cell) for cell in row))
    # newline="": без него Windows допишет ещё один перевод строки к каждому
    # CRLF и между заголовком и данными появится пустая строка — файл
    # перестанет совпадать с .xlsx из того же набора строк.
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    return path


def qualify(path: Path, *, course_id: int, connection: sqlite3.Connection):
    roster = rv.read_roster(path, file_name=path.name)
    return rv.qualify(roster, course_id=course_id, connection=connection)


def texts(verdict) -> list[str]:
    return [error["text"] for error in verdict["errors"]]


# --------------------------------------------------------------------------
# Чтение файла
# --------------------------------------------------------------------------


def test_файл_без_столбца_пароль_отвергается(tmp_path):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov@example.ru", None)],
        header=("ФИО", "роль", "группа", "e-mail"),
    )
    with pytest.raises(ServiceError) as failure:
        rv.read_roster(path, file_name=path.name)
    assert str(failure.value) == config.MESSAGES["no_roster_header"]


def test_файл_только_с_заголовком_даёт_нет_строк(tmp_path):
    path = write_xlsx(tmp_path / "roster.xlsx", [])
    with pytest.raises(ServiceError) as failure:
        rv.read_roster(path, file_name=path.name)
    assert str(failure.value) == config.MESSAGES["no_roster_rows"]


def test_чужое_расширение_отвергается(tmp_path):
    path = tmp_path / "roster.txt"
    path.write_text("ФИО;роль", encoding="utf-8")
    with pytest.raises(ServiceError) as failure:
        rv.read_roster(path, file_name=path.name)
    assert str(failure.value) == config.MESSAGES["roster_extension"]


def test_нечитаемый_xlsx_даёт_понятный_отказ(tmp_path):
    path = tmp_path / "roster.xlsx"
    path.write_bytes("это вовсе не таблица".encode("utf-8"))
    with pytest.raises(ServiceError) as failure:
        rv.read_roster(path, file_name=path.name)
    assert str(failure.value) == config.MESSAGES["roster_unreadable"]


def test_слишком_много_строк_отвергается(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMPORT_ROSTER_MAX_ROWS", 2)
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [
            ("Студент Один", "студент", "ФК-11", "s1@example.ru", "пароль"),
            ("Студент Два", "студент", "ФК-11", "s2@example.ru", "пароль"),
            ("Студент Три", "студент", "ФК-11", "s3@example.ru", "пароль"),
        ],
    )
    with pytest.raises(ServiceError) as failure:
        rv.read_roster(path, file_name=path.name)
    assert "больше 2 строк" in str(failure.value)


def test_пустые_строки_в_конце_файла_не_считаются_данными(tmp_path):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [
            ("Студент Один", "студент", "ФК-11", "s1@example.ru", "пароль"),
            (None, None, None, None, None),
        ],
    )
    roster = rv.read_roster(path, file_name=path.name)
    assert [row["row_number"] for row in roster["rows"]] == [2]


def test_заголовок_узнаётся_в_другом_регистре_и_с_точками(tmp_path):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov@example.ru", "пароль")],
        header=("Ф.И.О.", "РОЛЬ", " Группа ", "E-mail", "Пароль"),
    )
    roster = rv.read_roster(path, file_name=path.name)
    assert roster["header"] == {
        "full_name": 0,
        "role": 1,
        "group": 2,
        "email": 3,
        "password": 4,
    }


def test_лишние_столбцы_игнорируются(tmp_path, connection, course_id):
    """§7.14: служебный столбец F с суффиксом пароля в дело не идёт."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov@example.ru", "пароль", "!")],
        header=("ФИО", "роль", "группа", "e-mail", "пароль", "суффикс"),
    )
    verdicts = qualify(path, course_id=course_id, connection=connection)
    assert verdicts[0]["accepted"]
    assert verdicts[0]["values"]["password"] == "пароль"


# --------------------------------------------------------------------------
# Формула пароля (FR-31, AC-39)
# --------------------------------------------------------------------------


def test_формула_без_кэша_вычисляется_по_шаблону(tmp_path, connection, course_id):
    """AC-39: пароль задан формулой, вычисленного значения нет."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Макаров Сергей", "студент", "ФК-11", "makarov@example.ru", None, "!")],
    )
    book = openpyxl.load_workbook(path)
    sheet = book.active
    sheet["E2"] = "=LEFT(D2,4)&F2"
    book.save(path)

    # Убеждаемся, что кэша действительно нет: иначе тест проверял бы не то.
    cached = openpyxl.load_workbook(path, data_only=True).active["E2"].value
    assert cached is None

    verdicts = qualify(path, course_id=course_id, connection=connection)
    assert verdicts[0]["values"]["password"] == "maka!"
    assert verdicts[0]["accepted"]


def test_кэшированное_значение_формулы_важнее_вычисления():
    """FR-31: сначала результат Excel, шаблон — только запасной путь.

    Через файл это не проверить: openpyxl не пишет кэш формулы. Поэтому
    проверяется ``_row_values`` — именно он реализует приоритет.
    """
    header = rv._build_header(["ФИО", "роль", "группа", "e-mail", "пароль", "суффикс"])
    row = {
        "row_number": 2,
        "cells": [
            "Макаров Сергей", "студент", "ФК-11",
            "makarov@example.ru", "=LEFT(D2,4)&F2", "!",
        ],
        "cached": [
            "Макаров Сергей", "студент", "ФК-11",
            "makarov@example.ru", "zzzz!", "!",
        ],
    }
    values, unreadable = rv._row_values(header, row)
    assert values["password"] == "zzzz!"
    assert not unreadable


def test_неподдерживаемая_формула_даёт_br08(tmp_path, connection, course_id):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov@example.ru", None)],
        header=("ФИО", "роль", "группа", "e-mail", "пароль"),
    )
    book = openpyxl.load_workbook(path)
    book.active["E2"] = "=SUM(1,2)"
    book.save(path)

    verdicts = qualify(path, course_id=course_id, connection=connection)
    assert verdicts[0]["errors"][0]["text"] == (
        "Не удалось получить значение пароля: неподдерживаемая формула"
    )
    # Нечитаемая формула — это BR-08, а не «пустой пароль»: BR-01 и
    # BR-07 на ту же ячейку не срабатывают.
    assert texts(verdicts[0]) == [
        "Не удалось получить значение пароля: неподдерживаемая формула"
    ]


def test_формула_ссылается_на_другую_строку_отвергается(tmp_path, connection, course_id):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [
            ("Первый Студент", "студент", "ФК-11", "one@example.ru", "пароль1"),
            ("Второй Студент", "студент", "ФК-11", "two@example.ru", None),
        ],
    )
    book = openpyxl.load_workbook(path)
    book.active["E3"] = "=LEFT(D2,4)&F2"
    book.save(path)

    verdicts = qualify(path, course_id=course_id, connection=connection)
    assert verdicts[1]["errors"][0]["text"].endswith("неподдерживаемая формула")


# --------------------------------------------------------------------------
# BR-01…BR-09
# --------------------------------------------------------------------------


def test_пустые_обязательные_поля(tmp_path, connection, course_id):
    """BR-01 на все три текстовых поля сразу."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("", "студент", "", "ivanov@example.ru", "пароль")],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert not verdict["accepted"]
    assert texts(verdict) == ["Не заполнено поле: ФИО", "Не заполнено поле: группа"]


def test_пустой_пароль_даёт_две_причины(tmp_path, connection, course_id):
    """BR-01 и BR-07 вместе — так же, как строка 5 файла ФК_21."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov@example.ru", None)],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == [
        "Не заполнено поле: пароль",
        "Пароль должен содержать не менее 5 символов",
    ]


def test_недопустимая_роль(tmp_path, connection, course_id):
    """BR-02: роль вне «студент»/«куратор» не квалифицируется."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "преподаватель", "ФК-11", "ivanov@example.ru", "пароль")],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == [
        "Недопустимая роль: преподаватель. Допустимо: студент или куратор"
    ]


def test_роль_распознаётся_в_любом_регистре(tmp_path, connection, course_id):
    """§7.14: регистр и лишние пробелы в роли не важны."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [
            ("Студент Один", " СТУДЕНТ ", "ФК-11", "s1@example.ru", "пароль"),
            ("Куратор Один", "Куратор", "ФК-11", "c1@example.ru", "пароль"),
        ],
    )
    verdicts = qualify(path, course_id=course_id, connection=connection)
    assert all(verdict["accepted"] for verdict in verdicts)


def test_e_mail_без_собаки(tmp_path, connection, course_id):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov.example.ru", "пароль")],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == ["Некорректный e-mail: отсутствует символ @"]


def test_e_mail_без_домена(tmp_path, connection, course_id):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov@", "пароль")],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == ["Некорректный e-mail: не указан домен"]


def test_e_mail_с_одно_буквенным_доменом_отвергается(tmp_path, connection, course_id):
    """Формат §7.3 строже BR-03/BR-04: «ivanov@localhost» не годится."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov@localhost", "пароль")],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == [config.MESSAGES["email_invalid"]]


def test_второе_вхождение_e_mail_отклоняется_с_номером_строки(
    tmp_path, connection, course_id
):
    """BR-05 указывает на первое вхождение, даже если та строка отклонена."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [
            ("Первый Студент", "студент", "ФК-11", "same@example.ru", "пароль"),
            ("Второй Студент", "студент", "ФК-11", "SAME@example.ru", "пароль"),
        ],
    )
    verdicts = qualify(path, course_id=course_id, connection=connection)
    assert verdicts[0]["accepted"]
    assert texts(verdicts[1]) == ["e-mail уже используется в строке 2"]


def test_занятый_в_курсе_e_mail(tmp_path, connection, course_id):
    # insert_user, а не add_user: квалификации нужен только e-mail, а
    # add_user шифрует пароль и требует ключ шифрования, которого здесь нет.
    insert_user(connection, "busy@example.ru", course_id=course_id)
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "busy@example.ru", "пароль")],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == ["Этот e-mail уже зарегистрирован в курсе"]


def test_короткий_пароль(tmp_path, connection, course_id):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov@example.ru", "1234")],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == ["Пароль должен содержать не менее 5 символов"]


def test_длинное_фио_отвергается_по_пределу_схемы(tmp_path, connection, course_id):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Ф" * 201, "студент", "ФК-11", "ivanov@example.ru", "пароль")],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == [config.MESSAGES["full_name_too_long"]]


def test_имя_архивной_группы_отвергается(tmp_path, connection, course_id):
    """Иначе подтверждение создало бы вторую группу с тем же именем."""
    insert_group(
        connection,
        name="ФК-11",
        course_id=course_id,
        archived_at="2026-01-02T00:00:00Z",
    )
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Иванов Иван", "студент", "ФК-11", "ivanov@example.ru", "пароль")],
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == [config.MESSAGES["group_name_taken"]]


def test_несколько_причин_в_одной_строке(tmp_path, connection, course_id):
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("", "некто", "", "нет", "1")],
        header=HEADER,
    )
    verdict = qualify(path, course_id=course_id, connection=connection)[0]
    assert texts(verdict) == [
        "Не заполнено поле: ФИО",
        "Не заполнено поле: группа",
        "Недопустимая роль: некто. Допустимо: студент или куратор",
        "Некорректный e-mail: отсутствует символ @",
        "Пароль должен содержать не менее 5 символов",
    ]


def test_полностью_корректная_строка_квалифицируется(tmp_path, connection, course_id):
    """BR-09."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [
            ("Студент Один", "студент", "ФК-11", "s1@example.ru", "пароль"),
            ("Куратор Один", "куратор", "ФК-21", "c1@example.ru", "пароль"),
        ],
    )
    verdicts = qualify(path, course_id=course_id, connection=connection)
    assert [verdict["accepted"] for verdict in verdicts] == [True, True]
    assert verdicts[0]["values"]["group"] == "ФК-11"


def test_квалификация_ничего_не_пишет(tmp_path, connection, course_id):
    """FR-25: оценка строк безопасна, даже когда все строки приняты."""
    path = write_xlsx(
        tmp_path / "roster.xlsx",
        [("Студент Один", "студент", "ФК-11", "s1@example.ru", "пароль")],
    )
    qualify(path, course_id=course_id, connection=connection)
    assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM action_log").fetchone()[0] == 0


# --------------------------------------------------------------------------
# CSV (§6.4: «.csv обрабатывается так же, как .xlsx»)
# --------------------------------------------------------------------------


def test_csv_даёт_те_же_вердикты_что_и_xlsx(tmp_path, connection, course_id):
    rows = [
        ("Студент Один", "студент", "ФК-11", "s1@example.ru", "пароль1"),
        ("", "студент", "ФК-11", "s2@example.ru", "пароль2"),
        ("Куратор Один", "куратор", "ФК-21", "c1@example.ru", "пароль3"),
    ]
    xlsx = qualify(
        write_xlsx(tmp_path / "roster.xlsx", rows),
        course_id=course_id,
        connection=connection,
    )
    csv_file = qualify(
        write_csv(tmp_path / "roster.csv", rows),
        course_id=course_id,
        connection=connection,
    )
    assert [(v["accepted"], texts(v)) for v in xlsx] == [
        (v["accepted"], texts(v)) for v in csv_file
    ]
    # Номера строк тоже должны совпасть: в отчёте и в import_errors пишется
    # номер строки файла, и сдвиг на один означал бы ссылки не туда.
    assert [v["row_number"] for v in csv_file] == [v["row_number"] for v in xlsx]


def test_csv_с_запятой_и_в_cp1251(tmp_path, connection, course_id):
    path = tmp_path / "roster.csv"
    path.write_bytes(
        "ФИО,роль,группа,e-mail,пароль\r\n"
        "Студент Один,студент,ФК-11,s1@example.ru,пароль\r\n".encode("cp1251")
    )
    verdicts = qualify(path, course_id=course_id, connection=connection)
    assert verdicts[0]["accepted"]
    assert verdicts[0]["values"]["full_name"] == "Студент Один"


def test_csv_с_формулой_без_кэша(tmp_path, connection, course_id):
    """FR-31 распространяется и на CSV: формула — это текст ячейки."""
    path = tmp_path / "roster.csv"
    path.write_text(
        "ФИО;роль;группа;e-mail;пароль;суффикс\r\n"
        "Макаров Сергей;студент;ФК-11;makarov@example.ru;=LEFT(D2,4)&F2;!\r\n",
        encoding="utf-8",
        newline="",
    )
    verdicts = qualify(path, course_id=course_id, connection=connection)
    assert verdicts[0]["values"]["password"] == "maka!"
    assert verdicts[0]["accepted"]
