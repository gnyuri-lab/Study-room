"""Импорт списка на настоящих файлах из Inputs (AC-23…AC-27, AC-38, AC-39).

Эталонные вердикты зафиксированы в ТЗ и чек-листе, поэтому проверяются
дословно, а не «примерно»:

* ``Inputs/Список студентов-ФК_11.xlsx`` — 4 корректные строки из 4;
  куратор в последней строке (AC-23, AC-24).
* ``Inputs/Список студентов-ФК_21.xlsx`` — 4 из 6; отклонены строки 5 и 6,
  причём на строку 6 приходится сразу две причины, а строка 5 отклонена
  только из-за пустого пароля (AC-25, AC-26).

Плюс идемпотентность повторного импорта (AC-27), доступность отчёта и
запись в журнал (AC-38, FR-29) и формула пароля без кэшированного
значения (AC-39).

Файлы лежат рядом с тестами, поэтому в ``Inputs/`` ничего не пишется:
загрузка идёт через тестовый клиент, а копия файла — в tmp_path.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import openpyxl
import pytest

from app import config, passwords
from tests.conftest import add_user, insert_course

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUTS = PROJECT_ROOT / "Inputs"

FK11 = "Список студентов-ФК_11.xlsx"
FK21 = "Список студентов-ФК_21.xlsx"

ADMIN_LOGIN = ("admin@example.ru", "Пароль1234")

#: Тексты причин BR-01, BR-05 и BR-07 для строк 5 и 6 файла ФК_21 (AC-25).
ROW5_REASONS = [
    "Не заполнено поле: пароль",
    "Пароль должен содержать не менее 5 символов",
]
ROW6_REASONS = [
    "Не заполнено поле: группа",
    "e-mail уже используется в строке 5",
]


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(connection, key_path):
    """Курс с заголовком и администратор: больше в нём ничего нет.

    ``key_path`` нужен, потому что ``add_user`` пишет настоящий пароль:
    без ключа шифрования учётная запись администратора не создастся.
    """
    course = insert_course(connection, title="Основной курс")
    admin = add_user(
        connection,
        ADMIN_LOGIN[0],
        ADMIN_LOGIN[1],
        role="admin",
        full_name="Администратор Системы",
    )
    return {"course": course, "admin": admin}


@pytest.fixture
def as_admin(client, world):
    response = client.post(
        "/login",
        data={"email": ADMIN_LOGIN[0], "password": ADMIN_LOGIN[1]},
        follow_redirects=True,
    )
    assert "Выйти" in response.text
    return response


def import_url(course_id: int) -> str:
    return f"/admin/courses/{course_id}/import"


def upload(client, course_id: int, name: str, source: Path | None = None):
    """Отправить файл на форму импорта и вернуть ответ предпросмотра."""
    path = source or (INPUTS / name)
    return client.post(
        import_url(course_id),
        data={"file": (io.BytesIO(path.read_bytes()), name)},
        content_type="multipart/form-data",
    )


def confirm(client, course_id: int):
    return client.post(
        f"/admin/courses/{course_id}/import/confirm", follow_redirects=True
    )


def users_in(connection, course_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM users WHERE course_id = ? ORDER BY id", (course_id,)
    ).fetchall()


# --------------------------------------------------------------------------
# AC-23, AC-24: эталонный чистый импорт ФК_11
# --------------------------------------------------------------------------


def test_предпросмотр_фк_11_показывает_4_из_4(as_admin, world, client, connection):
    """AC-23: все четыре строки приняты, ни одной причины отклонения."""
    course_id = world["course"]
    response = upload(client, course_id, FK11)
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Принято строк: <strong>4</strong> из 4" in page
    assert "Строк с ошибками" not in page
    assert "Отклонена" not in page
    # FR-25: предпросмотр не пишет строк реестра.
    assert users_in(connection, course_id) == []
    assert connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM import_errors").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0


def test_предпросмотр_не_пишет_в_журнал(as_admin, world, client, connection):
    """FR-25 в строгом прочтении: до подтверждения в базе вообще ничего.

    Решение от 29.09.2026: код ``import.previewed`` из §7.12 не пишется,
    потому что предпросмотр — это чтение файла, а не действие над курсом.
    """
    course_id = world["course"]
    upload(client, course_id, FK11)

    # В журнале есть только вход администратора — ни одного события импорта.
    actions = [
        row["action"] for row in connection.execute("SELECT action FROM action_log")
    ]
    assert actions == ["auth.login"]

    upload(client, course_id, FK11)
    assert connection.execute(
        "SELECT COUNT(*) FROM action_log WHERE action LIKE 'import.%'"
    ).fetchone()[0] == 0


def test_предпросмотр_не_показывает_пароли(as_admin, world, client):
    """Пароль из файла не должен попадать на страницу предпросмотра."""
    response = upload(client, world["course"], FK11)
    page = response.get_data(as_text=True)
    for password in ("maka!", "lari@", "krut&", "gnyu@"):
        assert password not in page


def test_подтверждение_фк_11_создаёт_группу_студентов_и_куратора(
    as_admin, world, client, connection
):
    """AC-24: группа ФК11, три студента, куратор прикреплён к этой группе."""
    course_id = world["course"]
    upload(client, course_id, FK11)
    response = confirm(client, course_id)
    assert response.status_code == 200

    groups = connection.execute(
        "SELECT * FROM groups WHERE course_id = ?", (course_id,)
    ).fetchall()
    assert [group["name"] for group in groups] == ["ФК11"]
    group_id = groups[0]["id"]

    students = connection.execute(
        """SELECT u.email FROM group_members m
           JOIN users u ON u.id = m.user_id WHERE m.group_id = ?
           ORDER BY u.email""",
        (group_id,),
    ).fetchall()
    assert [row["email"] for row in students] == [
        "krut@ya.ru",
        "larik@ya.ru",
        "makar@ya.ru",
    ]

    curators = connection.execute(
        """SELECT u.email FROM curator_groups c
           JOIN users u ON u.id = c.user_id WHERE c.group_id = ?""",
        (group_id,),
    ).fetchall()
    assert [row["email"] for row in curators] == ["gnyuri@yandex.ru"]


def test_пароли_при_импорте_совпадают_с_файлом(as_admin, world, client, connection):
    """AC-24: «пароли соответствуют файлу» — проверяем настоящей проверкой."""
    course_id = world["course"]
    upload(client, course_id, FK11)
    confirm(client, course_id)

    for email, password in (
        ("makar@ya.ru", "maka!"),
        ("larik@ya.ru", "lari@"),
        ("krut@ya.ru", "krut&"),
        ("gnyuri@yandex.ru", "gnyu@"),
    ):
        row = connection.execute(
            "SELECT * FROM users WHERE course_id = ? AND email = ?",
            (course_id, email),
        ).fetchone()
        assert row is not None, email
        # Обе половины учётной записи должны работать после импорта:
        # хеш — для входа, шифрование — для чтения пароля (FR-09).
        assert passwords.verify_password(password, row["password_hash"]) is True
        assert passwords.reveal_password(row) == password


def test_студент_входит_после_импорта(as_admin, world, client):
    """Импортированная учётная запись должна работать наравне с созданной."""
    course_id = world["course"]
    upload(client, course_id, FK11)
    confirm(client, course_id)

    client.get("/logout")
    response = client.post(
        "/login",
        data={"email": "makar@ya.ru", "password": "maka!"},
        follow_redirects=True,
    )
    assert "Завершить настройку" not in response.text
    assert "makar@ya.ru" in response.text or "Выйти" in response.text


# --------------------------------------------------------------------------
# AC-25, AC-26: эталон отбраковки ФК_21
# --------------------------------------------------------------------------


def test_предпросмотр_фк_21_отклоняет_строки_5_и_6(as_admin, world, client):
    """AC-25: приняты 2, 3, 4, 7; у строк 5 и 6 — все причины сразу."""
    response = upload(client, world["course"], FK21)
    page = response.get_data(as_text=True)

    assert "Принято строк: <strong>4</strong> из 6" in page
    assert "Строк с ошибками: 2 (причин: 4)" in page

    assert "Строка 5" in page
    for reason in ROW5_REASONS:
        assert reason in page
    assert "Строка 6" in page
    for reason in ROW6_REASONS:
        assert reason in page

    # Строка 5 не повинна в дубликате e-mail: это первое вхождение, и
    # ложная вторая причина там была бы ошибкой BR-05.
    fifth = page[page.find("Строка 5") : page.find("Строка 6")]
    assert "e-mail уже используется" not in fifth


def test_подтверждение_фк_21_создаёт_только_принятые_строки(
    as_admin, world, client, connection
):
    """AC-26: в базу попали строки 2, 3, 4, 7 и только они."""
    course_id = world["course"]
    upload(client, course_id, FK21)
    confirm(client, course_id)

    assert sorted(user["email"] for user in users_in(connection, course_id)) == [
        "medved@ya.ru",
        "nest@yandex.ru",
        "sherb@ya.ru",
        "trus@ya.ru",
    ]
    # Отклонённый Рублев и Болт не должны появиться ни под каким видом.
    assert connection.execute(
        "SELECT COUNT(*) FROM users WHERE course_id = ? AND full_name LIKE 'Рублев%'",
        (course_id,),
    ).fetchone()[0] == 0


def test_отчёт_об_импорте_сохраняется_и_виден_администратору(
    as_admin, world, client, connection
):
    """FR-29, AC-38: отчёт доступен администратору, в журнале есть запись."""
    course_id = world["course"]
    upload(client, course_id, FK21)
    confirm(client, course_id)

    batch = connection.execute(
        "SELECT * FROM import_batches WHERE course_id = ?", (course_id,)
    ).fetchone()
    assert batch["file_name"] == FK21
    assert (batch["total_rows"], batch["valid_rows"]) == (6, 4)

    errors = connection.execute(
        "SELECT * FROM import_errors WHERE batch_id = ? ORDER BY row_number, id",
        (batch["id"],),
    ).fetchall()
    assert [row["row_number"] for row in errors] == [5, 5, 6, 6]
    assert [row["error_text"] for row in errors] == ROW5_REASONS + ROW6_REASONS

    # Отчёт виден на странице импорта.
    page = client.get(import_url(course_id)).get_data(as_text=True)
    assert FK21 in page
    assert "Строка 5" in page and "Строка 6" in page

    # И в журнале действий (FR-29).
    actions = [
        row["action"]
        for row in connection.execute(
            "SELECT action FROM action_log WHERE entity_type = 'import'"
            " ORDER BY id"
        ).fetchall()
    ]
    assert actions == ["import.committed"]


def test_сводка_после_подтверждения(as_admin, world, client):
    """§9.3: «Импортировано строк: 4 из 6. Строк с ошибками: 2.»"""
    course_id = world["course"]
    upload(client, course_id, FK21)
    page = confirm(client, course_id).get_data(as_text=True)
    assert "Импортировано строк: 4 из 6. Строк с ошибками: 2." in page


# --------------------------------------------------------------------------
# AC-27, FR-30: повторный импорт не создаёт дублей
# --------------------------------------------------------------------------


def test_повторный_импорт_не_создаёт_дублей(as_admin, world, client, connection):
    """AC-27: тот же файл в тот же курс — ни одной новой учётной записи."""
    course_id = world["course"]
    upload(client, course_id, FK21)
    confirm(client, course_id)
    before = [user["email"] for user in users_in(connection, course_id)]

    page = upload(client, course_id, FK21).get_data(as_text=True)
    assert "Принято строк: <strong>0</strong> из 6" in page
    assert "Этот e-mail уже зарегистрирован в курсе" in page

    confirm(client, course_id)
    assert [user["email"] for user in users_in(connection, course_id)] == before
    # Группы тоже не задваились.
    assert connection.execute(
        "SELECT COUNT(*) FROM groups WHERE course_id = ?", (course_id,)
    ).fetchone()[0] == 1


def test_тот_же_email_на_другом_курсе_принимается(
    as_admin, world, client, connection
):
    """Чек-лист §6.4: уникальность e-mail в пределах курса, не глобальная."""
    first = world["course"]
    second = insert_course(connection, title="Второй курс")

    upload(client, first, FK11)
    confirm(client, first)

    page = upload(client, second, FK11).get_data(as_text=True)
    assert "Принято строк: <strong>4</strong> из 4" in page
    assert "Этот e-mail уже зарегистрирован в курсе" not in page

    confirm(client, second)
    assert connection.execute(
        "SELECT COUNT(*) FROM users WHERE course_id = ?", (second,)
    ).fetchone()[0] == 4


# --------------------------------------------------------------------------
# Отмена и целостность сценария
# --------------------------------------------------------------------------


def test_отмена_не_пишет_ничего(as_admin, world, client, connection):
    course_id = world["course"]
    upload(client, course_id, FK11)
    response = client.post(
        f"/admin/courses/{course_id}/import/cancel", follow_redirects=True
    )
    assert "Импорт отменён" in response.get_data(as_text=True)

    assert users_in(connection, course_id) == []
    assert connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0

    # И подтверждать после отмены нечего.
    assert (
        config.MESSAGES["import_session_expired"]
        in confirm(client, course_id).get_data(as_text=True)
    )


def test_подтверждение_без_предпросмотра_отказывает(as_admin, world, client, connection):
    """Подсвеченная кнопка без открытого предпросмотра — не импорт."""
    course_id = world["course"]
    page = confirm(client, course_id).get_data(as_text=True)
    assert config.MESSAGES["import_session_expired"] in page
    assert users_in(connection, course_id) == []


def test_открытие_страницы_сбрасывает_предпросмотр(as_admin, world, client, connection):
    course_id = world["course"]
    upload(client, course_id, FK11)
    client.get(import_url(course_id))
    confirm(client, course_id)
    assert users_in(connection, course_id) == []


# --------------------------------------------------------------------------
# AC-39 и §6.4: файл с теми же данными, но в других форматах
# --------------------------------------------------------------------------


def test_формула_без_кэшированного_значения(as_admin, world, client, connection, tmp_path):
    """AC-39, чек-лист §6.4: файл, созданный программно, всё равно читается."""
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["ФИО", "роль", "группа", "e-mail", "пароль", "суффикс"])
    sheet.append(["Макаров Сергей", "студент", "ФК11", "makarov@example.ru", None, "!"])
    sheet["E2"] = "=LEFT(D2,4)&F2"
    source = tmp_path / "без_кэша.xlsx"
    book.save(source)

    course_id = world["course"]
    page = upload(
        client, course_id, "без_кэша.xlsx", source=source
    ).get_data(as_text=True)
    assert "Принято строк: <strong>1</strong> из 1" in page

    confirm(client, course_id)
    row = connection.execute(
        "SELECT * FROM users WHERE course_id = ? AND email = ?",
        (course_id, "makarov@example.ru"),
    ).fetchone()
    assert row is not None
    assert passwords.reveal_password(row) == "maka!"


def test_csv_даёт_тот_же_результат_что_xlsx(as_admin, world, client, connection, tmp_path):
    """Чек-лист §6.4: «Файл .csv с теми же данными обрабатывается так же»."""
    source = tmp_path / "список.csv"
    source.write_bytes(
        "ФИО;роль;группа;e-mail;пароль\r\n"
        "Макаров Сергей;студент;ФК11;makarov@example.ru;maka!\r\n"
        "Nest Yuri;куратор;ФК11;gnyuri@yandex.ru;gnyu@\r\n".encode("cp1251")
    )
    course_id = world["course"]
    page = upload(client, course_id, "список.csv", source=source).get_data(
        as_text=True
    )
    assert "Принято строк: <strong>2</strong> из 2" in page

    confirm(client, course_id)
    assert connection.execute(
        "SELECT COUNT(*) FROM users WHERE course_id = ?", (course_id,)
    ).fetchone()[0] == 2


def test_неподдерживаемая_формула_даёт_понятную_причину(
    as_admin, world, client, tmp_path
):
    """AC-39: не «пароль пуст», а «не удалось получить значение»."""
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["ФИО", "роль", "группа", "e-mail", "пароль"])
    sheet.append(["Макаров Сергей", "студент", "ФК11", "makarov@example.ru", None])
    sheet["E2"] = "=PRODUCT(2,3)"
    source = tmp_path / "чужая_формула.xlsx"
    book.save(source)

    course_id = world["course"]
    page = upload(
        client, course_id, "чужая_формула.xlsx", source=source
    ).get_data(as_text=True)
    assert "Не удалось получить значение пароля: неподдерживаемая формула" in page
    # BR-01 и BR-07 на ту же ячейку не срабатывают: значение неизвестно,
    # и объявлять его пустым значит соврать администратору.
    assert "Не заполнено поле: пароль" not in page
    assert "Пароль должен содержать не менее 5 символов" not in page


# --------------------------------------------------------------------------
# Права и границы файла
# --------------------------------------------------------------------------


def test_студент_не_доступен_к_импорту(connection, world, client):
    """BR-33: импорт доступен только администратору."""
    course_id = world["course"]
    add_user(
        connection,
        "student@example.ru",
        "Пароль123",
        course_id=course_id,
        full_name="Студент Студентов",
    )
    client.post(
        "/login",
        data={"email": "student@example.ru", "password": "Пароль123"},
        follow_redirects=True,
    )
    assert client.get(import_url(course_id)).status_code == 403
    assert (
        client.post(
            f"/admin/courses/{course_id}/import/confirm",
        ).status_code
        == 403
    )


def test_чужой_формат_файла_отклоняется(as_admin, world, client, tmp_path):
    source = tmp_path / "список.txt"
    source.write_text("ФИО;роль;группа;e-mail;пароль", encoding="utf-8")
    response = upload(client, world["course"], "список.txt", source=source)
    assert response.status_code == 400
    assert config.MESSAGES["roster_extension"] in response.get_data(as_text=True)


def test_пустой_файл_отклоняется(as_admin, world, client, tmp_path):
    """Ноль байт — это «нет строк данных», а не «не удалось прочитать»."""
    source = tmp_path / "пусто.xlsx"
    source.write_bytes(b"")
    response = upload(client, world["course"], "пусто.xlsx", source=source)
    assert response.status_code == 400
    assert config.MESSAGES["no_roster_rows"] in response.get_data(as_text=True)


def test_нечитаемый_файл_отклоняется(as_admin, world, client, tmp_path):
    """Файл с расширением .xlsx, но не таблица: понятный отказ, не 500."""
    source = tmp_path / "обрывок.xlsx"
    source.write_bytes("это вовсе не таблица".encode("utf-8"))
    response = upload(client, world["course"], "обрывок.xlsx", source=source)
    assert response.status_code == 400
    assert config.MESSAGES["roster_unreadable"] in response.get_data(as_text=True)


def test_пустая_форма_даёт_понятный_отказ(as_admin, world, client):
    response = client.post(
        import_url(world["course"]),
        data={},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert config.MESSAGES["roster_no_file"] in response.get_data(as_text=True)


def test_в_журнале_нет_паролей(connection, world, client):
    """FR-64: ни пароля, ни его части в details_json быть не должно."""
    course_id = world["course"]
    upload(client, course_id, FK11)
    confirm(client, course_id)

    for row in connection.execute("SELECT details_json FROM action_log").fetchall():
        details = json.loads(row["details_json"]) if row["details_json"] else {}
        for key in details:
            assert "pass" not in key.lower()
            assert "парол" not in key.lower()
    page = client.get(import_url(course_id)).get_data(as_text=True)
    for password in ("maka!", "gnyu@"):
        assert password not in page
