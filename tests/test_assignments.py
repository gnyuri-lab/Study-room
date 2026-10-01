"""Задания и материалы: создание, правка, удаление, студенческие экраны
(FR-32…FR-37, FR-51…FR-54, BR-23, BR-24, AC-05, AC-06, AC-29, AC-33, AC-40).

Проверяется не «страница открылась», а решения этапа:

* предпросмотр действительно ничего не пишет — до подтверждения в базе
  нет ни задания, ни файла, ни записи в журнале (FR-34, FR-25);
* дедлайн не уезжает: то, что администратор ввёл, он же и увидит;
  тест не завязан на часовой пояс машины, потому что на Windows
  сменить его для процесса нельзя;
* задание со сдачами архивируется, без сдач удаляется (BR-23, BR-24);
* файл хранится под своим именем, а не под присланным, и путь из
  ``original_name`` никуда не ведёт (FR-54, NFR-06);
* студент чужого курса получает отказ, а не файл (AC-33, FR-53).
"""

from __future__ import annotations

import io
import sqlite3

import pytest

from app import config, db, passwords
from tests.conftest import add_user, insert_course, insert_group, add_student_to_group

ADMIN_LOGIN = ("admin@example.ru", "Пароль1234")
STUDENT_LOGIN = ("student@example.ru", "Пароль123")
OTHER_STUDENT_LOGIN = ("other@example.ru", "Пароль123")


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(connection, key_path):
    """Курс, группа, администратор, студент и студент чужого курса."""
    course = insert_course(connection, title="Основной курс")
    group = insert_group(connection, name="ФК-1", course_id=course)
    admin = add_user(
        connection,
        ADMIN_LOGIN[0],
        ADMIN_LOGIN[1],
        role="admin",
        full_name="Администратор Системы",
    )
    student = add_user(
        connection,
        STUDENT_LOGIN[0],
        STUDENT_LOGIN[1],
        course_id=course,
        full_name="Студент Первый",
    )
    add_student_to_group(connection, group, student)

    other = insert_course(connection, title="Другой курс")
    other_group = insert_group(connection, name="ФК-9", course_id=other)
    other_student = add_user(
        connection,
        OTHER_STUDENT_LOGIN[0],
        OTHER_STUDENT_LOGIN[1],
        course_id=other,
        full_name="Студент Второй",
    )
    add_student_to_group(connection, other_group, other_student)
    return {
        "course": course,
        "group": group,
        "admin": admin,
        "student": student,
        "other_course": other,
        "other_student": other_student,
    }


def login(client, credentials):
    """Войти под указанной учётной записью.

    Выход делается обязательно: форма входа уводит уже авторизованного
    пользователя на главную (FR-07), поэтому без выхода второй вход в
    том же клиенте просто не произойдёт и тест продолжил бы работать
    под учётной записью предыдущего шага.
    """
    client.post("/logout")
    response = client.post(
        "/login",
        data={"email": credentials[0], "password": credentials[1]},
        follow_redirects=True,
    )
    assert "Выйти" in response.text
    return response


def as_admin(client, world):
    return login(client, ADMIN_LOGIN)


def as_student(client, world):
    return login(client, STUDENT_LOGIN)


def rows(connection, table, where="", args=()):
    query = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
    return [dict(row) for row in connection.execute(query, args).fetchall()]


def log_actions(connection) -> list[str]:
    """Коды действий в журнале.

    Вход в систему тоже пишется в журнал, поэтому проверки ведутся по
    списку кодов, а не по числу строк: иначе пришлось бы считать
    лишние записи о входе в каждом тесте.
    """
    return [entry["action"] for entry in rows(connection, "action_log")]


# --------------------------------------------------------------------------
# Создание с предпросмотром (FR-32, FR-34, AC-40)
# --------------------------------------------------------------------------


def text_file(name: str, content: str = "материал задания\n"):
    return (io.BytesIO(content.encode("utf-8")), name)


def submit_form(client, course_id, **fields):
    """Отправить форму создания и вернуть ответ предпросмотра."""
    data = {
        "title": fields.get("title", "Первое задание"),
        "description": fields.get("description", ""),
        "due_at": fields.get("due_at", ""),
    }
    files = fields.get("files", [])
    if files:
        data["files"] = files
    return client.post(
        f"/admin/courses/{course_id}/assignments/new",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def confirm(client, course_id):
    return client.post(
        f"/admin/courses/{course_id}/assignments/confirm", follow_redirects=True
    )


def create_assignment(client, world, **fields):
    """Полный сценарий: форма → предпросмотр → подтверждение."""
    course_id = world["course"]
    response = submit_form(client, course_id, **fields)
    assert "Предпросмотр" in response.text
    return confirm(client, course_id)


def test_preview_writes_nothing(client, connection, world):
    """До подтверждения не появляется ни строки в базе (FR-34).

    Строгое требование, унаследованное от импорта: предпросмотр не
    оставляет после себя ни задания, ни файла, ни записи в журнале.
    """
    as_admin(client, world)
    response = submit_form(client, world["course"], files=[text_file("материал.txt")])

    assert "Предпросмотр" in response.text
    assert rows(connection, "assignments") == []
    assert rows(connection, "assignment_files") == []
    # Вход в систему пишется всегда, поэтому проверяются действия
    # над сущностями: предпросмотр не должен оставить ни одного.
    assert [code for code in log_actions(connection) if code.split(".")[0] in
            {"assignment", "file", "group", "user", "course"}] == []
    # Файл лежит на проверке и ещё не записан в хранилище.
    from app import attachments

    staged = list(attachments.staging_dir().glob("staged-*.txt"))
    assert len(staged) == 1


def test_create_with_due_and_file(client, connection, world):
    """Сценарий AC-40 целиком: предпросмотр, запись, файл, журнал."""
    as_admin(client, world)
    response = create_assignment(
        client,
        world,
        title="Домашняя работа 1",
        description="Описание задания",
        due_at="2026-10-01T23:59",
        files=[text_file("методичка.txt")],
    )
    assert "Задание создано" in response.text

    saved = rows(connection, "assignments")
    assert len(saved) == 1
    assert saved[0]["title"] == "Домашняя работа 1"
    assert saved[0]["description"] == "Описание задания"
    assert saved[0]["due_at"].endswith("Z")

    files = rows(connection, "assignment_files")
    assert len(files) == 1
    assert files[0]["original_name"] == "методичка.txt"
    # Имя в базе и в каталоге не совпадает с исходным (FR-54).
    assert files[0]["stored_name"] != "методичка.txt"
    assert "/" not in files[0]["stored_name"]

    from app import attachments

    stored = attachments.stored_path(files[0]["stored_name"])
    assert stored.is_file()
    assert stored.read_text(encoding="utf-8") == "материал задания\n"

    log = log_actions(connection)
    assert log[-1] == "assignment.created"


def test_deadline_round_trip(client, connection, world):
    """Введённый дедлайн показывается тем же (допущение В-02).

    Проверка не завязана на часовой пояс: сравнивается то, что видит
    администратор, а не смещение в UTC.
    """
    from app.services import assignments as service

    typed = "2026-10-01T23:59"
    stored = service.parse_due(typed)
    assert service.to_input_value(stored) == typed
    assert service.format_due(stored) == "01.10.2026 23:59"


def test_deadline_optional(client, connection, world):
    """Дедлайн необязателен (FR-32): пустое поле хранит NULL."""
    as_admin(client, world)
    create_assignment(client, world, title="Без срока")
    assert rows(connection, "assignments")[0]["due_at"] is None


def test_deadline_invalid_rejected(client, connection, world):
    """Невозможная дата отклоняется текстом, а не молча теряется."""
    as_admin(client, world)
    response = submit_form(client, world["course"], due_at="31.12.2026")
    assert config.MESSAGES["assignment_due_invalid"] in response.text
    assert rows(connection, "assignments") == []


def test_title_required(client, connection, world):
    as_admin(client, world)
    response = submit_form(client, world["course"], title="   ")
    assert config.MESSAGES["assignment_title_required"] in response.text
    assert rows(connection, "assignments") == []


def test_title_too_long(client, connection, world):
    as_admin(client, world)
    response = submit_form(client, world["course"], title="а" * 201)
    assert config.MESSAGES["assignment_title_too_long"] in response.text
    assert rows(connection, "assignments") == []


def test_order_index_grows(client, connection, world):
    """Порядок вывода задаётся при создании и растёт (FR-32)."""
    as_admin(client, world)
    for title in ("Первое", "Второе", "Третье"):
        create_assignment(client, world, title=title)
    order = [item["order_index"] for item in rows(connection, "assignments")]
    assert order == sorted(order)
    assert len(set(order)) == 3


# --------------------------------------------------------------------------
# Описание из файла (FR-33)
# --------------------------------------------------------------------------


def test_description_from_file(client, connection, world):
    """Описание можно загрузить файлом — в базу попадёт текст (FR-33)."""
    as_admin(client, world)
    response = client.post(
        f"/admin/courses/{world['course']}/assignments/new",
        data={
            "title": "С длинным текстом",
            "description_file": (io.BytesIO("Первый абзац.\n".encode("utf-8")), "d.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Первый абзац" in response.text
    confirm(client, world["course"])
    assert rows(connection, "assignments")[0]["description"] == "Первый абзац."


def test_description_text_and_file_conflict(client, connection, world):
    """Оба поля сразу — отказ: иначе один из текстов потерялся бы молча."""
    as_admin(client, world)
    response = client.post(
        f"/admin/courses/{world['course']}/assignments/new",
        data={
            "title": "Оба поля",
            "description": "Ручной текст",
            "description_file": (io.BytesIO("Текст файла".encode("utf-8")), "d.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "не оба" in response.text
    assert rows(connection, "assignments") == []


def test_description_file_wrong_type(client, connection, world):
    as_admin(client, world)
    response = client.post(
        f"/admin/courses/{world['course']}/assignments/new",
        data={
            "title": "Картинка вместо текста",
            "description_file": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40), "d.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert config.MESSAGES["description_file_type"] in response.text
    assert rows(connection, "assignments") == []


# --------------------------------------------------------------------------
# Лимиты файлов (FR-52, AC-34)
# --------------------------------------------------------------------------


def test_six_files_rejected(client, connection, world):
    """Шестой файл отклоняется, пять сохраняются (AC-34)."""
    as_admin(client, world)
    files = [text_file(f"файл-{index}.txt") for index in range(6)]
    response = submit_form(client, world["course"], files=files)
    assert response.status_code == 400
    assert config.MESSAGES["files_limit_assignment"] in response.text
    assert rows(connection, "assignments") == []
    assert rows(connection, "assignment_files") == []


def test_five_files_accepted(client, connection, world):
    as_admin(client, world)
    files = [text_file(f"файл-{index}.txt") for index in range(5)]
    create_assignment(client, world, files=files)
    assert len(rows(connection, "assignment_files")) == 5


def test_file_limit_counts_existing(client, connection, world):
    """При правке учитываются уже прикреплённые файлы (FR-52)."""
    as_admin(client, world)
    create_assignment(
        client, world, files=[text_file(f"ф-{index}.txt") for index in range(4)]
    )
    assignment_id = rows(connection, "assignments")[0]["id"]
    response = client.post(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/edit",
        data={
            "title": "Первое задание",
            "files": [text_file("ещё.txt"), text_file("и-ещё.txt")],
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert config.MESSAGES["files_limit_assignment"] in response.text
    assert len(rows(connection, "assignment_files")) == 4


def test_oversize_file_rejected(client, connection, world):
    as_admin(client, world)
    big = io.BytesIO(b"a" * (config.MAX_FILE_SIZE_BYTES + 1))
    response = submit_form(
        client, world["course"], files=[(big, "огромный.txt")]
    )
    assert response.status_code == 400
    assert config.MESSAGES["file_too_large"] in response.text
    assert rows(connection, "assignment_files") == []


def test_bad_type_rejected(client, connection, world):
    as_admin(client, world)
    response = submit_form(
        client, world["course"], files=[(io.BytesIO(b"#!/bin/sh\n"), "скрипт.sh")]
    )
    assert response.status_code == 400
    assert "sh" in response.text
    assert rows(connection, "assignment_files") == []


# --------------------------------------------------------------------------
# Правка и отмена (FR-34, FR-35)
# --------------------------------------------------------------------------


def test_edit_keeps_existing_files(client, connection, world):
    """Правка не трогает уже прикреплённые файлы (FR-35)."""
    as_admin(client, world)
    create_assignment(client, world, files=[text_file("первый.txt")])
    assignment_id = rows(connection, "assignments")[0]["id"]

    client.post(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/edit",
        data={
            "title": "Переименованное",
            "files": [text_file("второй.txt")],
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    confirm(client, world["course"])

    saved = rows(connection, "assignments")[0]
    assert saved["title"] == "Переименованное"
    names = sorted(
        item["original_name"] for item in rows(connection, "assignment_files")
    )
    assert names == ["второй.txt", "первый.txt"]


def test_discard_removes_staged_files(client, connection, world):
    """Отмена черновика не пишет в базу и убирает файлы с проверки."""
    from app import attachments

    as_admin(client, world)
    submit_form(client, world["course"], files=[text_file("черновик.txt")])
    assert len(list(attachments.staging_dir().glob("staged-*.txt"))) == 1

    client.post(
        f"/admin/courses/{world['course']}/assignments/discard",
        follow_redirects=True,
    )
    assert rows(connection, "assignments") == []
    assert list(attachments.staging_dir().glob("staged-*.txt")) == []


def test_reopening_form_drops_unconfirmed_draft(client, connection, world):
    """Открытие формы сбрасывает незавершённый черновик.

    Иначе кнопка «Подтвердить» предложила бы сохранить то, что
    администратор уже не видел.
    """
    from app import attachments

    as_admin(client, world)
    submit_form(client, world["course"], title="Черновик", files=[text_file("a.txt")])
    client.get(f"/admin/courses/{world['course']}/assignments/new")
    assert list(attachments.staging_dir().glob("staged-*.txt")) == []


def test_preview_without_draft_shows_message(client, connection, world):
    """Предпросмотр без черновика объясняет, что делать (FR-34)."""
    as_admin(client, world)
    response = client.get(
        f"/admin/courses/{world['course']}/assignments/preview?preview=1",
        follow_redirects=True,
    )
    assert config.MESSAGES["assignment_preview_expired"] in response.text


def test_files_removed_when_log_write_fails(client, connection, world, monkeypatch):
    """Сбой записи в журнал не оставляет файлы-сироты (NFR-06).

    Файлы переносятся в хранилище до вставки строк, поэтому откат
    транзакции обязан убрать и их: строк в базе нет, а файл на диске
    остался бы навсегда — его никто не увидит и никогда не удалит.
    """
    from app import action_log, attachments

    as_admin(client, world)
    submit_form(client, world["course"], files=[text_file("материал.txt")])
    with client.session_transaction() as session:
        draft_name = session["assignment_draft"]

    def boom(*args, **kwargs):
        raise RuntimeError("журнал недоступен")

    monkeypatch.setattr(action_log, "log_action", boom)
    with pytest.raises(RuntimeError):
        confirm(client, world["course"])

    assert rows(connection, "assignments") == []
    assert rows(connection, "assignment_files") == []
    # Материалы убраны с диска, а черновик формы остался: после отказа
    # администратор должен иметь возможность повторить подтверждение,
    # а не заполнять всё заново.
    leftovers = [
        item for item in attachments.uploads_dir().rglob("*") if item.is_file()
    ]
    assert leftovers == [attachments.staging_dir() / name for name in (draft_name,)]
    assert attachments.staging_path(draft_name).is_file()


def test_edit_confirm_error_returns_to_own_preview(client, connection, world, monkeypatch):
    """После отказа на подтверждении правки предпросмотр открывает то
    же самое задание, а не форму создания.

    Иначе администратор увидел бы предпросмотр уже без прикреплённых
    файлов задания и мог бы подтвердить не тот вариант, который
    редактировал. Проверяется адрес, а не текст: правильное сообщение
    об отказе само по себе не говорит, какое задание открыто.
    """
    from app.services import ServiceError
    from app.services import assignments as service

    as_admin(client, world)
    create_assignment(
        client, world, title="Первое задание", files=[text_file("уже-прикреплён.txt")]
    )
    assignment_id = rows(connection, "assignments")[0]["id"]

    client.post(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/edit",
        data={"title": "Исправленное"},
        content_type="multipart/form-data",
    )

    def refuse(**kwargs):
        raise ServiceError("Отказ по требованию теста.")

    monkeypatch.setattr(service, "update_assignment", refuse)
    response = client.post(
        f"/admin/courses/{world['course']}/assignments/confirm",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert f"/assignments/{assignment_id}/preview" in response.headers["Location"]
    assert rows(connection, "assignments")[0]["title"] == "Первое задание"

    # На предпросмотре видно и правку, и уже прикреплённый файл того же
    # задания: при открытии чужого задания список файлов был бы пуст.
    page = client.get(response.headers["Location"], follow_redirects=True)
    assert "Исправленное" in page.text
    assert "уже-прикреплён.txt" in page.text


def test_remove_file(client, connection, world):
    """Материал открепляется, работа студентов не затрагивается (FR-35)."""
    from app import attachments

    as_admin(client, world)
    create_assignment(
        client, world, files=[text_file("первый.txt"), text_file("второй.txt")]
    )
    assignment_id = rows(connection, "assignments")[0]["id"]
    files = rows(connection, "assignment_files")
    target = next(item for item in files if item["original_name"] == "первый.txt")

    client.post(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}"
        f"/files/{target['id']}/delete",
        follow_redirects=True,
    )
    remaining = rows(connection, "assignment_files")
    assert [item["original_name"] for item in remaining] == ["второй.txt"]
    assert not attachments.stored_path(target["stored_name"]).exists()
    assert log_actions(connection)[-3:] == [
        "assignment.created",
        "file.deleted",
        "assignment.updated",
    ]


# --------------------------------------------------------------------------
# Удаление задания (BR-23, BR-24, AC-29)
# --------------------------------------------------------------------------


def add_submission(connection, assignment_id, student_id, status="submitted"):
    connection.execute(
        """
        INSERT INTO submissions (
            assignment_id, student_id, attempt_no, status, comment,
            submitted_at, created_at
        ) VALUES (?, ?, 1, ?, 'Работа', '2026-10-01T10:00:00Z',
                  '2026-10-01T10:00:00Z')
        """,
        (assignment_id, student_id, status),
    )


def test_delete_without_submissions_is_physical(client, connection, world):
    """Без сдач задание удаляется полностью, вместе с файлами (BR-24)."""
    from app import attachments

    as_admin(client, world)
    create_assignment(client, world, files=[text_file("материал.txt")])
    assignment_id = rows(connection, "assignments")[0]["id"]
    stored = rows(connection, "assignment_files")[0]["stored_name"]
    assert attachments.stored_path(stored).is_file()

    response = client.get(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/delete"
    )
    assert "будет удалено" in response.text

    client.post(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/delete",
        data={"confirm": "on"},
        follow_redirects=True,
    )
    assert rows(connection, "assignments") == []
    assert rows(connection, "assignment_files") == []
    assert not attachments.stored_path(stored).exists()
    # Запись в журнале остаётся: физически удалён только объект (FR-62).
    assert rows(connection, "action_log")[-1]["details_json"]


def test_delete_requires_confirmation(client, connection, world):
    """Без отметки подтверждения удаление не выполняется (§9.1)."""
    as_admin(client, world)
    create_assignment(client, world)
    assignment_id = rows(connection, "assignments")[0]["id"]

    response = client.post(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/delete",
        data={},
    )
    assert response.status_code == 400
    assert len(rows(connection, "assignments")) == 1


def test_delete_with_submissions_archives(client, connection, world):
    """Со сдачами — только архив: работы и файлы сохраняются (BR-23, AC-29)."""
    from app import attachments

    as_admin(client, world)
    create_assignment(client, world, files=[text_file("материал.txt")])
    assignment_id = rows(connection, "assignments")[0]["id"]
    stored = rows(connection, "assignment_files")[0]["stored_name"]
    add_submission(connection, assignment_id, world["student"])

    page = client.get(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/delete"
    )
    assert "в архив" in page.text

    response = client.post(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/delete",
        data={"confirm": "on"},
        follow_redirects=True,
    )
    assert "в архив" in response.text

    saved = rows(connection, "assignments")
    assert len(saved) == 1
    assert saved[0]["archived_at"] is not None
    assert len(rows(connection, "assignment_files")) == 1
    assert len(rows(connection, "submissions")) == 1
    assert attachments.stored_path(stored).is_file()
    assert [entry["action"] for entry in rows(connection, "action_log")][-1] == (
        "assignment.archived"
    )


def test_archived_assignment_hidden_from_student(client, connection, world):
    """Архивное задание не попадает в список студента (FR-23)."""
    as_admin(client, world)
    create_assignment(client, world, title="Активное")
    create_assignment(client, world, title="В архиве")
    archived_id = rows(connection, "assignments")[1]["id"]
    add_submission(connection, archived_id, world["student"])
    client.post(
        f"/admin/courses/{world['course']}/assignments/{archived_id}/delete",
        data={"confirm": "on"},
        follow_redirects=True,
    )

    as_student(client, world)
    response = client.get(
        f"/student/courses/{world['course']}/assignments", follow_redirects=True
    )
    assert "Активное" in response.text
    assert "В архиве" not in response.text


def test_archived_assignment_not_openable_by_student(client, connection, world):
    """На архивное задание по прямой ссылке — 404, а не карточка."""
    as_admin(client, world)
    create_assignment(client, world, title="В архиве")
    assignment_id = rows(connection, "assignments")[0]["id"]
    add_submission(connection, assignment_id, world["student"])
    client.post(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/delete",
        data={"confirm": "on"},
    )

    as_student(client, world)
    response = client.get(
        f"/student/courses/{world['course']}/assignments/{assignment_id}"
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Экраны студента (FR-37, AC-05, AC-06)
# --------------------------------------------------------------------------


def test_student_list_shows_status(client, connection, world):
    """Список показывает название, описание, дедлайн и статус (AC-05)."""
    as_admin(client, world)
    create_assignment(
        client,
        world,
        title="Задание с работой",
        description="Описание для студента",
        due_at="2026-10-01T23:59",
    )
    assignment_id = rows(connection, "assignments")[0]["id"]
    add_submission(connection, assignment_id, world["student"], status="revision")

    as_student(client, world)
    response = client.get(
        f"/student/courses/{world['course']}/assignments", follow_redirects=True
    )
    assert "Задание с работой" in response.text
    assert "Описание для студента" in response.text
    assert "01.10.2026 23:59" in response.text
    assert "На доработку" in response.text


def test_student_status_without_submission_is_not_submitted(client, connection, world):
    """Без сдач статус «Не сдано» (Д-10: строки в базе нет)."""
    as_admin(client, world)
    create_assignment(client, world, title="Новое задание")

    as_student(client, world)
    response = client.get(
        f"/student/courses/{world['course']}/assignments", follow_redirects=True
    )
    assert "Не сдано" in response.text


def test_student_card_lists_materials(client, connection, world):
    """Карточка показывает дедлайн и перечисляет материалы (AC-06)."""
    as_admin(client, world)
    create_assignment(
        client,
        world,
        title="С материалами",
        due_at="2026-10-01T23:59",
        files=[text_file("методичка.txt")],
    )
    assignment_id = rows(connection, "assignments")[0]["id"]

    as_student(client, world)
    response = client.get(
        f"/student/courses/{world['course']}/assignments/{assignment_id}"
    )
    assert "01.10.2026 23:59" in response.text
    # Подпись и дата красные, «не задан» приглушено (замечание приёмки
    # 01.10.2026).
    assert '<span class="deadline">Дедлайн сдачи:' in response.text
    assert "методичка.txt" in response.text


def test_preview_marks_deadline_red(client, connection, world):
    """На предпросмотре подпись «Дедлайн сдачи» и дата красные,
    «не задан» приглушено (замечание приёмки 01.10.2026, FR-34)."""
    as_admin(client, world)

    response = submit_form(
        client, world["course"], title="С дедлайном", due_at="2026-10-01T23:59"
    )
    text = response.get_data(as_text=True)
    assert '<dt class="deadline">Дедлайн сдачи</dt>' in text
    assert "01.10.2026 23:59" in text
    assert "не задан" not in text

    blank = submit_form(client, world["course"], title="Без дедлайна")
    blank_text = blank.get_data(as_text=True)
    assert '<dt class="deadline">Дедлайн сдачи</dt>' in blank_text
    assert '<span class="muted">не задан</span>' in blank_text


def test_student_cannot_open_foreign_course(client, connection, world):
    """Список заданий чужого курса недоступен (BR-28, FR-08)."""
    as_student(client, world)
    response = client.get(f"/student/courses/{world['other_course']}/assignments")
    assert response.status_code == 403


def test_curator_has_no_student_assignment_list(client, connection, world):
    """Маршруты списка заданий — только для студента (FR-19)."""
    curator_login = ("curator@example.ru", "Пароль123")
    add_user(
        connection,
        curator_login[0],
        curator_login[1],
        role="curator",
        course_id=world["course"],
        full_name="Куратор Первый",
    )
    login(client, curator_login)
    response = client.get(f"/student/courses/{world['course']}/assignments")
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Скачивание (FR-36, FR-53, FR-54, AC-33)
# --------------------------------------------------------------------------


def first_file_id(connection) -> int:
    return rows(connection, "assignment_files")[0]["id"]


def test_student_downloads_assignment_file(client, connection, world):
    """Студент своего курса скачивает материал (FR-36)."""
    as_admin(client, world)
    create_assignment(client, world, files=[text_file("методичка.txt")])
    file_id = first_file_id(connection)

    as_student(client, world)
    response = client.get(f"/files/assignment/{file_id}")
    assert response.status_code == 200
    assert response.data.decode("utf-8") == "материал задания\n"
    # В ответе — исходное имя, кириллица не ломает заголовок.
    assert "filename*=UTF-8''" in response.headers["Content-Disposition"]


def test_download_refused_for_foreign_student(client, connection, world):
    """Студент чужого курса получает отказ, а не файл (AC-33, FR-53)."""
    as_admin(client, world)
    create_assignment(client, world, files=[text_file("секрет.txt")])
    file_id = first_file_id(connection)

    login(client, OTHER_STUDENT_LOGIN)
    response = client.get(f"/files/assignment/{file_id}")
    assert response.status_code == 403
    # Свой текст отказа, а не общий «нет прав»: файл существует, но
    # текущему пользователю не виден.
    assert config.MESSAGES["no_file_access"] in response.get_data(as_text=True)
    assert "материал задания" not in response.get_data(as_text=True)


def test_download_refused_for_guest(client, connection, world):
    """Без входа файл не отдаётся (FR-07)."""
    as_admin(client, world)
    create_assignment(client, world, files=[text_file("секрет.txt")])
    file_id = first_file_id(connection)
    client.post("/logout")

    response = client.get(f"/files/assignment/{file_id}")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_download_unknown_file_is_404(client, connection, world):
    as_student(client, world)
    assert client.get("/files/assignment/99999").status_code == 404


def test_download_unknown_kind_is_404(client, connection, world):
    as_student(client, world)
    assert client.get("/files/что-то-итого/1").status_code == 404


def test_download_logged(client, connection, world):
    """Скачивание попадает в журнал (FR-62, чек-лист §14)."""
    as_admin(client, world)
    create_assignment(client, world, files=[text_file("методичка.txt")])
    file_id = first_file_id(connection)

    as_student(client, world)
    client.get(f"/files/assignment/{file_id}")

    log = rows(connection, "action_log")
    assert log[-1]["action"] == "file.downloaded"
    assert log[-1]["actor_role"] == "student"
    assert log[-1]["entity_type"] == "file"
    assert log[-1]["entity_id"] == file_id


def test_path_in_original_name_cannot_escape(client, connection, world, tmp_path):
    """Подстановка пути в исходном имени ничего не открывает (NFR-06)."""
    as_admin(client, world)
    response = submit_form(
        client, world["course"], files=[text_file("../../secret.txt")]
    )
    assert "Предпросмотр" in response.text
    confirm(client, world["course"])

    saved = rows(connection, "assignment_files")[0]
    assert saved["original_name"] == "secret.txt"

    as_student(client, world)
    response = client.get(f"/files/assignment/{saved['id']}")
    assert response.status_code == 200
    assert response.data == "материал задания\n".encode("utf-8")
    # Никакой файл вне uploads не создан и не прочитан.
    assert not (tmp_path / "secret.txt").exists()


def test_missing_file_on_disk_is_404(client, connection, world):
    """Файл, исчезнувший с диска, даёт отказ, а не 500 (FR-36)."""
    from app import attachments

    as_admin(client, world)
    create_assignment(client, world, files=[text_file("методичка.txt")])
    saved = rows(connection, "assignment_files")[0]
    attachments.stored_path(saved["stored_name"]).unlink()

    as_student(client, world)
    response = client.get(f"/files/assignment/{saved['id']}")
    assert response.status_code == 404
    assert config.MESSAGES["file_missing_on_disk"] in response.get_data(
        as_text=True
    )


# --------------------------------------------------------------------------
# Навигация: экран заданий должен быть достижим кнопками, а не только URL
# --------------------------------------------------------------------------


def test_course_screens_link_to_assignments(client, connection, world):
    """Кнопка «Задания» есть и в списке курсов, и на правке курса.

    Без неё экран заданий достижим только по прямой ссылке, то есть для
    администратора он не существует: 495 тестов проходили, пока экран был
    недостижим, потому что все обращались к нему по URL напрямую.
    """
    as_admin(client, world)
    expected = f"/admin/courses/{world['course']}/assignments"

    page = client.get("/admin/courses")
    assert expected in page.get_data(as_text=True)
    assert "Задания" in page.get_data(as_text=True)

    page = client.get(f"/admin/courses/{world['course']}/edit")
    text = page.get_data(as_text=True)
    assert expected in text
    assert "Задания" in text


def test_assignments_screen_offers_create(client, connection, world):
    """На самом экране заданий есть и кнопка создания, и создание из
    пустого состояния — иначе первое задание некуда завести."""
    as_admin(client, world)
    url = f"/admin/courses/{world['course']}/assignments"

    page = client.get(url)
    assert "Добавить задание" in page.get_data(as_text=True)

    # После создания задания кнопка остаётся в шапке.
    create_assignment(client, world, title="Первое задание")
    page = client.get(url)
    assert "Добавить задание" in page.get_data(as_text=True)


def test_assignments_lists_mark_deadline_red(client, connection, world):
    """Подпись «Дедлайн» и дата красные в списках заданий, «не задан»
    приглушено — это не дата (замечание приёмки 01.10.2026, FR-37)."""
    as_admin(client, world)
    create_assignment(
        client, world, title="С дедлайном", due_at="2026-10-01T23:59"
    )
    create_assignment(client, world, title="Без дедлайна")

    page = client.get(f"/admin/courses/{world['course']}/assignments")
    text = page.get_data(as_text=True)
    assert '<span class="deadline">Дедлайн:' in text
    assert '<span class="muted">не задан</span>' in text
    # Красный цвет задаёт одно правило .deadline в стилях приложения —
    # на экране нет собственного цвета, срок читается единой красной.
    css = client.get("/static/css/app.css").get_data(as_text=True)
    assert ".deadline" in css
    assert "--color-danger-text" in css

    # Тот же экран студента: надпись и дата тоже красные (FR-37, AC-05).
    as_student(client, world)
    student_text = client.get(
        f"/student/courses/{world['course']}/assignments"
    ).get_data(as_text=True)
    assert '<p class="deadline">Дедлайн:' in student_text
    assert '<span class="muted">не задан</span>' in student_text


def test_student_cabinet_links_to_assignments(client, connection, world):
    """У студента из кабинета есть переход к заданиям (FR-23)."""
    as_student(client, world)
    page = client.get("/")
    text = page.get_data(as_text=True)
    assert f"/student/courses/{world['course']}/assignments" in text


# --------------------------------------------------------------------------
# Навигация и вёрстка: кнопка обязана работать, а не просто существовать
# --------------------------------------------------------------------------


def form_depth(html: str) -> int:
    """Максимальная вложенность ``<form>`` в отданном HTML.

    Вложенная форма запрещена: браузер игнорирует её открывающий тег, а
    закрывающий закрывает внешнюю — и кнопка «Сохранить» остаётся уже
    вне формы и ничего не делает. Проверяется по отданному тексту, а не
    по шаблону: важно то, что реально увидит браузер.
    """
    depth = 0
    peak = 0
    cursor = 0
    while True:
        opened = html.find("<form", cursor)
        closed = html.find("</form", cursor)
        if opened == -1 and closed == -1:
            return peak
        if closed != -1 and (opened == -1 or closed < opened):
            depth -= 1
            cursor = closed + 1
        else:
            depth += 1
            peak = max(peak, depth)
            cursor = opened + 1


def test_assignment_edit_with_files_has_no_nested_forms(client, connection, world):
    """Правка задания с прикреплённым файлом: «Сохранить» остаётся в форме.

    Дефект был именно здесь: форма «Открепить» лежала внутри формы
    сохранения, поэтому браузер закрывал внешнюю форму раньше кнопки.
    """
    as_admin(client, world)
    create_assignment(
        client, world, title="Задание с файлом", files=[text_file("материал.txt")]
    )
    assignment_id = rows(connection, "assignments")[0]["id"]

    page = client.get(
        f"/admin/courses/{world['course']}/assignments/{assignment_id}/edit"
    )
    text = page.get_data(as_text=True)
    assert page.status_code == 200
    # Без прикреплённого файла ветка с «Открепить» не рисуется и
    # дефект не воспроизводится — поэтому проверяем, что файл есть.
    assert "Открепить" in text
    assert form_depth(text) <= 1, "формы вложены друг в друга"
    # Кнопка сохранения и поле названия должны быть в одной форме.
    # Форм в странице несколько (выход в шапке), поэтому ищем ту, что
    # содержит поле названия.
    field = text.find('name="title"')
    form_start = text.rfind("<form", 0, field)
    form_end = text.find("</form", field)
    assert field != -1 and form_start != -1 and form_end != -1
    inside = text[form_start:form_end]
    assert "Сохранить" in inside


def test_cabinet_admin_opens_course_and_assignments_as_buttons(client, world):
    """Курсы и «Задания» в кабинете администратора — кнопки, не подписи."""
    as_admin(client, world)
    text = client.get("/").get_data(as_text=True)
    assert f"/admin/courses/{world['course']}/groups" in text
    assert f"/admin/courses/{world['course']}/assignments" in text
    assert "Задания" in text


def test_header_returns_to_cabinet(client, world):
    """С любого рабочего экрана есть возврат в кабинет (FR-09)."""
    as_admin(client, world)
    text = client.get("/admin/courses").get_data(as_text=True)
    assert "header-home" in text
    assert "В кабинет" in text

    as_student(client, world)
    text = client.get(
        f"/student/courses/{world['course']}/assignments"
    ).get_data(as_text=True)
    assert "header-home" in text
    assert "В личный кабинет" in text

    # На самом кабинете возврат не нужен — там уже кабинет.
    text = client.get("/").get_data(as_text=True)
    assert "header-home" not in text


def test_password_field_offers_show_toggle(client, world):
    """У каждого поля пароля есть кнопка показа (скрипт дорисовывает её)."""
    page = client.get("/login")
    text = page.get_data(as_text=True)
    assert "js/password-toggle.js" in text
    assert 'name="password" type="password"' in text


# --------------------------------------------------------------------------
# Права по ролям (BR-33, FR-07)
# --------------------------------------------------------------------------


def test_student_cannot_reach_admin_routes(client, connection, world):
    """Административные маршруты заданий закрыты для студента (BR-33)."""
    as_student(client, world)
    for suffix in ("", "/new"):
        response = client.get(
            f"/admin/courses/{world['course']}/assignments{suffix}"
        )
        assert response.status_code == 403


def test_student_cannot_create_assignment(client, connection, world):
    as_student(client, world)
    response = client.post(
        f"/admin/courses/{world['course']}/assignments/new",
        data={"title": "Своё задание"},
    )
    assert response.status_code == 403
    assert rows(connection, "assignments") == []


def test_admin_cannot_open_student_route(client, connection, world):
    """Маршруты списка заданий студента закрыты и администратору.

    Администратор смотрит задания в своём разделе; если бы маршрут был
    открыт и ему, студенческая страница показала бы «не сдано» вместо
    реального состояния работ.
    """
    as_admin(client, world)
    response = client.get(f"/student/courses/{world['course']}/assignments")
    assert response.status_code == 403


def test_unknown_assignment_is_404(client, connection, world):
    as_admin(client, world)
    response = client.get(
        f"/admin/courses/{world['course']}/assignments/99999/edit"
    )
    assert response.status_code == 404


def test_assignment_of_another_course_not_reachable(client, connection, world):
    """Идентификатор из чужого курса не даёт доступа (FR-08)."""
    as_admin(client, world)
    create_assignment(client, world, title="Задание основного курса")
    assignment_id = rows(connection, "assignments")[0]["id"]
    response = client.get(
        f"/admin/courses/{world['other_course']}/assignments/{assignment_id}/edit"
    )
    assert response.status_code == 404
