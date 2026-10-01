import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import config, db, security  # noqa: E402


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Изолированный каталог вместо data/ и var/ рабочего проекта."""
    data_dir = tmp_path / "data"
    var_dir = tmp_path / "var"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "VAR_DIR", var_dir)
    monkeypatch.setattr(config, "UPLOADS_DIR", data_dir / "uploads")
    monkeypatch.setattr(config, "DB_PATH", data_dir / "study_room.db")
    monkeypatch.setattr(config, "SECRET_KEY_PATH", var_dir / "secret.key")
    return tmp_path


@pytest.fixture
def key_path(workspace):
    path, _created = security.create_key()
    return path


@pytest.fixture
def connection(workspace):
    path, _applied = db.init_database()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


@pytest.fixture
def app(workspace, key_path):
    from app import create_app

    # key_path создаёт ключ по config.SECRET_KEY_PATH, который workspace
    # подменяет на tmp: create_app читает ключ подписи сессии оттуда же,
    # и тесты не зависят от var/ рабочего проекта.
    application = create_app()
    application.config.update(
        TESTING=True,
        DB_PATH=config.DB_PATH,
        MIGRATIONS_DIR=config.MIGRATIONS_DIR,
        SECRET_KEY_PATH=config.SECRET_KEY_PATH,
    )
    with application.app_context():
        db.init_database()
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def as_app(app):
    """Контекст приложения и запроса: нужны и `db.get_db()`, и `session`."""
    with app.test_request_context():
        yield


def insert_course(conn, title="Курс", created_at="2026-01-01T00:00:00Z"):
    cursor = conn.execute(
        "INSERT INTO courses (title, description, created_at) VALUES (?, ?, ?)",
        (title, None, created_at),
    )
    return cursor.lastrowid


def insert_user(
    conn,
    email,
    role="student",
    course_id=1,
    full_name="Имя Фамилия",
    password_enc=b"enc",
    password_hash="hash",
    must_change_password=0,
    is_active=1,
    created_at="2026-01-01T00:00:00Z",
):
    if role == "admin":
        course_id = None
    cursor = conn.execute(
        """
        INSERT INTO users (
            course_id, email, full_name, role, password_enc, password_hash,
            must_change_password, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            course_id,
            email,
            full_name,
            role,
            password_enc,
            password_hash,
            must_change_password,
            is_active,
            created_at,
        ),
    )
    return cursor.lastrowid


@pytest.fixture
def course(connection):
    """Один курс с id = 1, чтобы про FK было на что сослаться."""
    return insert_course(connection)


def insert_group(
    conn,
    name="Группа 1",
    course_id=1,
    archived_at=None,
    created_at="2026-01-01T00:00:00Z",
):
    cursor = conn.execute(
        """
        INSERT INTO groups (course_id, name, archived_at, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (course_id, name, archived_at, created_at),
    )
    return cursor.lastrowid


def add_student_to_group(conn, group_id, user_id):
    conn.execute(
        "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
        (group_id, user_id),
    )


def add_curator_to_group(conn, group_id, user_id):
    conn.execute(
        "INSERT INTO curator_groups (group_id, user_id) VALUES (?, ?)",
        (group_id, user_id),
    )


def add_user(
    conn,
    email,
    password,
    role="student",
    course_id=1,
    full_name="Имя Фамилия",
    must_change_password=0,
    is_active=1,
    created_at="2026-01-01T00:00:00Z",
):
    """Учётная запись с настоящими полями пароля.

    Тестам входа нужен реальный хеш и реальное шифрование: подставной
    `b"enc"` из `insert_user` не переживёт проверку пароля.
    """
    from app import passwords

    encrypted, hashed = passwords.build_credentials(password, role)
    if role == "admin":
        course_id = None
    cursor = conn.execute(
        """
        INSERT INTO users (
            course_id, email, full_name, role, password_enc, password_hash,
            must_change_password, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            course_id,
            email,
            full_name,
            role,
            encrypted,
            hashed,
            must_change_password,
            is_active,
            created_at,
        ),
    )
    return cursor.lastrowid
