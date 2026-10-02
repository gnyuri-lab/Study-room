"""Резервные копии: экспорт, предпросмотр, восстановление (FR-68…FR-71).

Проверяются решения этапа Э-10, а не «страница открылась»:

* экспорт отдаёт один JSON со всеми четырнадцатью таблицами и файлами
  в base64, имя файла содержит дату, действие уходит в журнал
  (FR-68, FR-69, AC-43);
* все четыре маршрута закрыты не-администраторам (BR-33, AC-43);
* предпросмотр принимает только корректную копию текущей версии схемы:
  не JSON, чужая версия, битая структура, ``true`` в INTEGER-поле,
  файл вне каталога вложений и несогласованные вложения отклоняются
  и не остаются на подтверждение (BR-34, BR-35, FR-70);
* превышение предела размера отклоняется по ходу копирования (FR-70);
* до подтверждения данные не меняются: открытый предпросмотр ничего
  не пишет, открытие экрана гасит незавершённую загрузку,
  восстановление без подтверждения не выполняется (BR-36);
* подтверждённое восстановление заменяет таблицы и файлы целиком,
  пишет `backup.restored` и завершает сессию (FR-69, FR-71, AC-45);
* отказ на любом шаге оставляет прежние данные и файлы (BR-37, AC-44);
* тексты сообщений выводятся дословно из `app/config.py` (§9.3).
"""

from __future__ import annotations

import base64
import io
import json

import pytest

from app import attachments, config, passwords
from app.services import backup
from tests.conftest import (
    add_curator_to_group,
    add_student_to_group,
    add_user,
    insert_course,
    insert_group,
)
from tests.test_submissions import (
    ADMIN_LOGIN,
    CURATOR_LOGIN,
    STUDENT_LOGIN,
    insert_assignment,
    login,
)

SCREEN_URL = "/admin/backup"
EXPORT_URL = "/admin/backup/export"
PREVIEW_URL = "/admin/backup/preview"
RESTORE_URL = "/admin/backup/restore"

#: Все маршруты этапа: для проверки отказа по роли (AC-43).
ROUTES = (
    ("get", SCREEN_URL),
    ("get", EXPORT_URL),
    ("post", PREVIEW_URL),
    ("post", RESTORE_URL),
)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(connection, key_path):
    """Один курс с группой, куратором, студентом, админом и заданием."""
    course = insert_course(connection, title="Основной курс")
    group = insert_group(connection, name="ФК-1", course_id=course)
    curator = add_user(
        connection,
        CURATOR_LOGIN[0],
        CURATOR_LOGIN[1],
        role="curator",
        course_id=course,
        full_name="Куратор Первый",
    )
    add_curator_to_group(connection, group, curator)
    student = add_user(
        connection,
        STUDENT_LOGIN[0],
        STUDENT_LOGIN[1],
        course_id=course,
        full_name="Студент Первый",
    )
    add_student_to_group(connection, group, student)
    admin = add_user(
        connection,
        ADMIN_LOGIN[0],
        ADMIN_LOGIN[1],
        role="admin",
        full_name="Администратор Системы",
    )
    assignment = insert_assignment(connection, course_id=course)
    return {
        "course": course,
        "group": group,
        "curator": curator,
        "student": student,
        "admin": admin,
        "assignment": assignment,
    }


# --------------------------------------------------------------------------
# Вспомогательные
# --------------------------------------------------------------------------


def as_admin(client):
    return login(client, ADMIN_LOGIN)


def export_bytes(client) -> bytes:
    """Скачать копию и проверить, что это файл, а не отказ."""
    response = client.get(EXPORT_URL)
    assert response.status_code == 200, response.status_code
    assert response.headers["Content-Type"].startswith("application/json")
    return response.data


def payload_of(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def upload(client, raw: bytes, name: str = "copy.json", url: str = PREVIEW_URL):
    return client.post(
        url,
        data={"file": (io.BytesIO(raw), name)},
        content_type="multipart/form-data",
    )


def records_total(payload: dict) -> int:
    return sum(len(payload["tables"][table]) for table in backup.TABLES)


def staged_files() -> list:
    directory = backup.staging_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def course_titles(connection) -> list:
    rows = connection.execute("SELECT title FROM courses ORDER BY id")
    return [row["title"] for row in rows]


def user_count(connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def journal_rows(connection, action: str) -> list:
    return connection.execute(
        "SELECT * FROM action_log WHERE action = ? ORDER BY id", (action,)
    ).fetchall()


def put_attachment(connection, world, stored_name="filealpha", content=b"payload"):
    """Файл вложения на диске + строка в `assignment_files`."""
    uploads = attachments.uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    attachments.stored_path(stored_name).write_bytes(content)
    connection.execute(
        """
        INSERT INTO assignment_files (
            assignment_id, original_name, stored_name, mime,
            size_bytes, uploaded_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            world["assignment"],
            "материал.txt",
            stored_name,
            "text/plain",
            len(content),
            world["admin"],
            "2026-01-01T00:00:00Z",
        ),
    )
    return stored_name


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def db_version(connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


# --------------------------------------------------------------------------
# Доступ (BR-33, AC-43)
# --------------------------------------------------------------------------


def test_routes_reject_curator_and_student(client, world):
    """Экран, экспорт, предпросмотр и восстановление — только админ."""
    for credentials in (CURATOR_LOGIN, STUDENT_LOGIN):
        login(client, credentials)
        for method, url in ROUTES:
            response = getattr(client, method)(url)
            assert response.status_code == 403, (credentials, url)


def test_anonymous_redirected_to_login(client, world):
    response = client.get(SCREEN_URL)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_screen_offers_export_and_warning(client, world):
    """AC-44 начинается с экрана: кнопка экспорта и предупреждение §9.3."""
    as_admin(client)
    response = client.get(SCREEN_URL)
    assert response.status_code == 200
    assert "Экспорт базы JSON" in response.text
    assert "Текущие данные будут полностью заменены." in response.text
    assert "Показать содержимое копии" in response.text


# --------------------------------------------------------------------------
# Экспорт (FR-68, FR-69, AC-43)
# --------------------------------------------------------------------------


def test_export_downloads_one_json_with_all_tables(client, world, connection):
    as_admin(client)
    data = export_bytes(client)

    response = client.get(EXPORT_URL)
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith('attachment; filename="study_room_backup_')
    assert disposition.endswith('.json"')
    stamp = disposition[len('attachment; filename="study_room_backup_'):-len('.json"')]
    assert len(stamp) == 8 and stamp.isdigit()

    payload = payload_of(data)
    assert set(payload) == {
        backup.SCHEMA_VERSION_KEY,
        backup.EXPORTED_AT_KEY,
        backup.TABLES_KEY,
        backup.FILES_KEY,
    }
    assert tuple(payload[backup.TABLES_KEY]) == backup.TABLES
    assert payload[backup.SCHEMA_VERSION_KEY] == db_version(connection)
    assert payload[backup.EXPORTED_AT_KEY].endswith("Z")

    emails = {row["email"] for row in payload[backup.TABLES_KEY]["users"]}
    assert ADMIN_LOGIN[0] in emails
    assert CURATOR_LOGIN[0] in emails
    assert STUDENT_LOGIN[0] in emails


def test_export_encodes_blobs_and_files_as_base64(client, world, connection):
    """password_enc (BLOB) и файлы вложения уходят в base64 (FR-68)."""
    put_attachment(connection, world, stored_name="filealpha", content=b"payload")
    as_admin(client)
    payload = payload_of(export_bytes(client))

    user = payload[backup.TABLES_KEY]["users"][0]
    assert isinstance(user["password_enc"], str)
    assert base64.b64decode(user["password_enc"], validate=True)

    assert set(payload[backup.FILES_KEY]) == {"filealpha"}
    assert base64.b64decode(
        payload[backup.FILES_KEY]["filealpha"], validate=True
    ) == b"payload"


def test_export_logs_action(client, world, connection):
    """FR-69: экспорт попадает в журнал с кодом `backup.exported`."""
    as_admin(client)
    payload = payload_of(export_bytes(client))

    rows = journal_rows(connection, "backup.exported")
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_id"] == world["admin"]
    assert row["actor_role"] == "admin"
    assert row["entity_type"] == "backup"
    details = json.loads(row["details_json"])
    assert details["records"] == records_total(payload)
    assert details["files"] == len(payload[backup.FILES_KEY])


def test_export_refuses_when_file_missing_on_disk(client, world, connection):
    """Тихо неполной копии не бывает: отказ и без записи в журнал."""
    stored = put_attachment(connection, world)
    attachments.stored_path(stored).unlink()
    as_admin(client)

    response = client.get(EXPORT_URL)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(SCREEN_URL)
    page = client.get(SCREEN_URL)
    assert "Файл не найден на диске." in page.text
    assert journal_rows(connection, "backup.exported") == []


# --------------------------------------------------------------------------
# Предпросмотр (FR-70, BR-34, BR-35)
# --------------------------------------------------------------------------


def test_preview_shows_counts_and_keeps_data_untouched(client, world, connection):
    """AC-44: счётчики по всем таблицам, строки базы не изменились."""
    as_admin(client)
    payload = payload_of(export_bytes(client))
    before_users = user_count(connection)
    before_titles = course_titles(connection)

    response = upload(client, json.dumps(payload).encode("utf-8"))
    assert response.status_code == 200, response.status_code
    text = response.text
    assert "данные пока не изменены" in text
    assert "Записей:" in text
    assert "Версия схемы:" in text
    for table in backup.TABLES:
        assert backup.TABLE_LABELS[table] in text
        assert str(len(payload[backup.TABLES_KEY][table])) in text

    assert user_count(connection) == before_users
    assert course_titles(connection) == before_titles
    assert len(staged_files()) == 1


def test_preview_without_file_rejected(client, world):
    as_admin(client)
    response = client.post(PREVIEW_URL, data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "Выберите файл резервной копии (.json)." in response.text
    assert staged_files() == []


def test_preview_rejects_not_json(client, world):
    """BR-34: обычный текст — не копия."""
    as_admin(client)
    response = upload(client, b"this is not a backup")
    assert response.status_code == 400
    assert "Файл не является резервной копией" in response.text
    assert staged_files() == []


def test_preview_rejects_wrong_schema_version(client, world, connection):
    """BR-35: чужая версия схемы получает свой текст отказа."""
    as_admin(client)
    payload = payload_of(export_bytes(client))
    payload[backup.SCHEMA_VERSION_KEY] = 999

    response = upload(client, json.dumps(payload).encode("utf-8"))
    assert response.status_code == 400
    expected = config.MESSAGES["backup_version"].format(
        file=999, current=db_version(connection)
    )
    assert expected in response.text
    assert staged_files() == []


DAMAGE_CASES = (
    ("нет ключа files", lambda p: p.pop("files")),
    ("нет таблицы users", lambda p: p["tables"].pop("users")),
    ("лишняя колонка", lambda p: p["tables"]["courses"][0].__setitem__("x", 1)),
    (
        "true в INTEGER",
        lambda p: p["tables"]["users"][0].__setitem__("must_change_password", True),
    ),
    ("строка вместо id", lambda p: p["tables"]["users"][0].__setitem__("id", "1")),
    (
        "файл не из вложений",
        lambda p: p["files"].__setitem__("ghost", b64(b"x")),
    ),
    (
        "выход за каталог вложений",
        lambda p: p["files"].__setitem__("../evil", b64(b"x")),
    ),
)


@pytest.mark.parametrize("label,damage", DAMAGE_CASES, ids=[c[0] for c in DAMAGE_CASES])
def test_preview_rejects_structural_damage(label, damage, client, world):
    """BR-34: структура копии ломается по-разному, ответ один."""
    as_admin(client)
    payload = payload_of(export_bytes(client))
    damage(payload)

    response = upload(client, json.dumps(payload).encode("utf-8"))
    assert response.status_code == 400, label
    assert "Файл не является резервной копией" in response.text, label
    assert staged_files() == [], label


def test_preview_rejects_oversized_file(client, world, monkeypatch):
    """FR-70: предел считается по ходу, файл не докачивается."""
    monkeypatch.setattr(config, "BACKUP_MAX_BYTES", 64)
    as_admin(client)

    response = upload(client, b"0" * 4096, name="big.json")
    assert response.status_code == 400
    assert "превышает допустимые" in response.text
    assert staged_files() == []


# --------------------------------------------------------------------------
# Подтверждение (BR-36)
# --------------------------------------------------------------------------


def test_restore_without_preview_is_refused(client, world, connection):
    """BR-36: прямой POST без загруженного файла ничего не меняет."""
    as_admin(client)
    before = course_titles(connection)

    response = client.post(RESTORE_URL)
    assert response.status_code == 302
    page = client.get(SCREEN_URL)
    assert "Файл резервной копии устарел. Загрузите его заново." in page.text
    assert course_titles(connection) == before
    assert journal_rows(connection, "backup.restored") == []


def test_open_screen_discards_pending_upload(client, world, connection):
    """Открытый экран гасит подтверждение: «Отменить» не оставляет файла."""
    as_admin(client)
    payload = payload_of(export_bytes(client))
    assert upload(client, json.dumps(payload).encode("utf-8")).status_code == 200
    assert len(staged_files()) == 1

    assert client.get(SCREEN_URL).status_code == 200
    assert staged_files() == []

    response = client.post(RESTORE_URL)
    assert response.status_code == 302
    page = client.get(SCREEN_URL)
    assert "Файл резервной копии устарел." in page.text
    assert journal_rows(connection, "backup.restored") == []


# --------------------------------------------------------------------------
# Восстановление (FR-71, FR-69, BR-37, AC-45)
# --------------------------------------------------------------------------


def test_restore_replaces_tables_files_and_ends_session(client, world, connection):
    """AC-45: полная замена, журнал, завершение сессии, чистые каталоги."""
    stored = put_attachment(connection, world, content=b"original")
    as_admin(client)
    payload = payload_of(export_bytes(client))

    # Копия сделана; после неё появляется лишний курс и чужой файл.
    connection.execute(
        "INSERT INTO courses (title, description, created_at) VALUES (?, ?, ?)",
        ("Новая", None, "2026-02-02T00:00:00Z"),
    )
    attachments.stored_path(stored).unlink()
    attachments.uploads_dir().mkdir(parents=True, exist_ok=True)
    (attachments.uploads_dir() / "stray.txt").write_bytes(b"stray")

    assert upload(client, json.dumps(payload).encode("utf-8")).status_code == 200
    response = client.post(RESTORE_URL)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")

    # Данные заменены составом копии.
    assert course_titles(connection) == ["Основной курс"]
    assert user_count(connection) == len(payload[backup.TABLES_KEY]["users"])

    # Журнал: экспорт и восстановление, автор — администратор.
    restored = journal_rows(connection, "backup.restored")
    assert len(restored) == 1
    assert restored[0]["actor_id"] == world["admin"]
    assert restored[0]["entity_type"] == "backup"
    details = json.loads(restored[0]["details_json"])
    assert details["records"] == records_total(payload)
    assert details["file_name"] == "copy.json"

    # Файлы: вложения копии на месте, посторонний файл убран.
    assert attachments.stored_path(stored).read_bytes() == b"original"
    assert not (attachments.uploads_dir() / "stray.txt").exists()

    # Сессия завершена: вход просят заново, сообщение §9.3 видно.
    page = client.get(SCREEN_URL)
    assert page.status_code == 302
    assert "/login" in page.headers["Location"]
    login_page = client.get("/login")
    assert "Резервная копия восстановлена. Войдите заново." in login_page.text

    # Ни staging, ни остатков подмены в каталоге вложений не осталось.
    assert staged_files() == []
    leftovers = [
        path.name
        for path in attachments.uploads_dir().iterdir()
        if path.name.startswith(".restore-")
    ]
    assert leftovers == []


def test_failed_restore_keeps_previous_data_and_files(client, world, connection):
    """BR-37: нарушение связей — откат, файлы и строки не тронуты."""
    stored = put_attachment(connection, world, content=b"original")
    as_admin(client)
    payload = payload_of(export_bytes(client))
    connection.execute(
        "INSERT INTO courses (title, description, created_at) VALUES (?, ?, ?)",
        ("Новая", None, "2026-02-02T00:00:00Z"),
    )
    (attachments.uploads_dir() / "stray.txt").write_bytes(b"stray")

    # Строка проходит валидацию (типы и состав верны), но ссылается на
    # несуществующее задание: отказ происходит уже в транзакции.
    payload[backup.TABLES_KEY]["submissions"].append(
        {
            "id": 999999,
            "assignment_id": 999999,
            "student_id": world["student"],
            "attempt_no": 1,
            "status": "submitted",
            "comment": "Ошибочная строка копии",
            "submitted_at": "2026-01-01T00:00:00Z",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )

    assert upload(client, json.dumps(payload).encode("utf-8")).status_code == 200
    response = client.post(RESTORE_URL)
    assert response.status_code == 302

    # Сессия не завершена: администратор остаётся на экране с ошибкой.
    page = client.get(SCREEN_URL)
    assert page.status_code == 200
    assert "Не удалось восстановить копию: данные не изменены." in page.text

    # Данные прежние: и курс, появившийся после экспорта, и файлы.
    assert "Новая" in course_titles(connection)
    assert attachments.stored_path(stored).read_bytes() == b"original"
    assert (attachments.uploads_dir() / "stray.txt").exists()
    assert journal_rows(connection, "backup.restored") == []
    assert staged_files() == []
