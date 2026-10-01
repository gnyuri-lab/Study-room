"""Вход, смена учётных данных и пароля.

Покрывает FR-01…FR-12, BR-10…BR-12, BR-28…BR-33, AC-01…AC-04,
AC-19…AC-22, AC-28, AC-35. Экран выбора курса (AC-28) убран по решению
заказчика: вход делается в один шаг, а видимость считается по всем
записям человека (``access.identity_rows``).
"""

import pytest

from app import access
from app.blueprints import auth
from tests.conftest import add_user, insert_course


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    from app import passwords

    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(key_path, connection):
    """Курс, студент с обычным паролем и студент с временным."""
    course = insert_course(connection, title="Основной курс")
    student = add_user(
        connection,
        "student@example.ru",
        "Пароль123",
        course_id=course,
        full_name="Студент Студентов",
    )
    temporary = add_user(
        connection,
        "temp@example.ru",
        "Пароль123",
        course_id=course,
        full_name="Временный Временный",
        must_change_password=1,
    )
    admin = add_user(
        connection,
        "admin@example.ru",
        "Пароль1234",
        role="admin",
        full_name="Администратор Системы",
    )
    return {
        "course": course,
        "student": student,
        "temporary": temporary,
        "admin": admin,
    }


def _must_change(connection, user_id):
    return connection.execute(
        "SELECT must_change_password FROM users WHERE id = ?", (user_id,)
    ).fetchone()["must_change_password"]


def _hash_of(connection, user_id):
    return connection.execute(
        "SELECT password_hash FROM users WHERE id = ?", (user_id,)
    ).fetchone()["password_hash"]


def _login(client, email, password, **extra):
    return client.post(
        "/login", data={"email": email, "password": password, **extra}
    )


# --------------------------------------------------------------------------
# Барьер доступа (BR-31, BR-32)
# --------------------------------------------------------------------------


def test_guest_is_sent_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_page_is_public(client):
    assert client.get("/login").status_code == 200


def test_static_is_public(client):
    assert client.get("/static/css/app.css").status_code == 200


def test_guest_keeps_intended_address(client):
    response = client.get("/")
    assert response.headers["Location"] == "/login?next=/"


# --------------------------------------------------------------------------
# Вход (FR-01, AC-28)
# --------------------------------------------------------------------------


def test_successful_login(client, connection, world):
    response = _login(client, "student@example.ru", "Пароль123")
    assert response.status_code == 302
    assert client.get("/").status_code == 200


def test_email_is_not_case_sensitive(client, connection, world):
    assert _login(client, "STUDENT@EXAMPLE.RU", "Пароль123").status_code == 302


def test_email_is_trimmed(client, connection, world):
    assert _login(client, "  student@example.ru  ", "Пароль123").status_code == 302


def test_wrong_password_is_refused(client, connection, world):
    response = _login(client, "student@example.ru", "Неверный123")
    assert response.status_code == 401
    assert auth.INVALID_CREDENTIALS in response.get_data(as_text=True)
    assert client.get("/").status_code == 302


def test_unknown_email_gives_the_same_text(client, connection, world):
    response = _login(client, "nobody@example.ru", "Пароль123")
    assert response.status_code == 401
    assert auth.INVALID_CREDENTIALS in response.get_data(as_text=True)


def test_malformed_email_is_reported(client, connection, world):
    response = _login(client, "не-почта", "Пароль123")
    assert response.status_code == 401
    assert "корректный e-mail" in response.get_data(as_text=True)


def test_inactive_user_cannot_log_in(client, connection, world):
    connection.execute(
        "UPDATE users SET is_active = 0 WHERE id = ?", (world["student"],)
    )
    assert _login(client, "student@example.ru", "Пароль123").status_code == 401


def test_login_is_written_to_the_journal(client, connection, world):
    _login(client, "student@example.ru", "Пароль123")
    row = connection.execute(
        "SELECT * FROM action_log WHERE action = 'auth.login'"
    ).fetchone()
    assert row is not None
    assert row["actor_id"] == world["student"]
    assert row["entity_type"] == "user"
    # Логина и пароля в записи быть не должно.
    assert "student@example.ru" not in (row["details_json"] or "")
    assert "Пароль123" not in (row["details_json"] or "")


def test_failed_login_is_not_written(client, connection, world):
    _login(client, "student@example.ru", "Неверный123")
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM action_log WHERE action = 'auth.login'"
        ).fetchone()[0]
        == 0
    )


def test_next_is_respected(client, connection, world):
    response = _login(client, "student@example.ru", "Пароль123", next="/change-password")
    assert response.headers["Location"] == "/change-password"


def test_external_next_is_ignored(client, connection, world):
    response = _login(
        client, "student@example.ru", "Пароль123", next="https://example.evil/"
    )
    assert response.headers["Location"] == "/"


def test_double_slash_next_is_ignored(client, connection, world):
    response = _login(client, "student@example.ru", "Пароль123", next="//example.evil/")
    assert response.headers["Location"] == "/"


# --------------------------------------------------------------------------
# Один человек на двух курсах (FR-05, BR-06): вход в один шаг
# --------------------------------------------------------------------------


@pytest.fixture
def two_courses(key_path, connection):
    first = insert_course(connection, title="Первый курс")
    second = insert_course(connection, title="Второй курс")
    first_user = add_user(
        connection, "both@example.ru", "Пароль123", course_id=first
    )
    second_user = add_user(
        connection, "both@example.ru", "Пароль123", course_id=second
    )
    return {
        "first": first,
        "second": second,
        "first_user": first_user,
        "second_user": second_user,
    }


def test_one_login_opens_both_courses(client, connection, two_courses):
    response = _login(client, "both@example.ru", "Пароль123")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    page = client.get("/").get_data(as_text=True)
    assert "Первый курс" in page
    assert "Второй курс" in page


def test_course_choice_screen_is_gone(client, connection, two_courses):
    # Экран выбора курса убран: вход больше не делят на шаги, а курс
    # определяется по тому, где человек заведён.
    _login(client, "both@example.ru", "Пароль123")
    assert client.get("/select-course").status_code == 404
    assert client.post("/select-course", data={}).status_code == 404


def test_password_is_not_stored_in_the_session(client, connection, two_courses):
    _login(client, "both@example.ru", "Пароль123")
    with client.session_transaction() as session_data:
        dumped = repr(dict(session_data))
        # В сеансе лежит только id строки: пароль в cookie попадать не должен.
        assert "Пароль123" not in dumped
        assert "pending_email" not in session_data


def test_password_matching_only_one_row_also_works(client, connection, two_courses):
    """Строки на курсах заводились отдельными файлами.

    Пароли могли разойтись, поэтому вход открывается по любому из них.
    """
    from app import passwords

    encrypted, hashed = passwords.build_credentials("ВторойПароль", "student")
    connection.execute(
        "UPDATE users SET password_enc = ?, password_hash = ? WHERE id = ?",
        (encrypted, hashed, two_courses["second_user"]),
    )
    response = _login(client, "both@example.ru", "ВторойПароль")
    assert response.status_code == 302
    with client.session_transaction() as session_data:
        assert session_data[access.SESSION_USER_KEY] == two_courses["second_user"]


def test_unknown_password_is_rejected_for_every_row(client, connection, two_courses):
    response = _login(client, "both@example.ru", "Неверный123")
    assert response.status_code == 401
    assert auth.INVALID_CREDENTIALS in response.get_data(as_text=True)


def test_password_change_reaches_every_row(client, connection, two_courses):
    _login(client, "both@example.ru", "Пароль123")
    response = client.post(
        "/change-password",
        data={
            "current_password": "Пароль123",
            "new_password": "НовыйПароль1",
            "new_password_confirm": "НовыйПароль1",
        },
    )
    assert response.status_code == 302
    hashes = {
        row["password_hash"]
        for row in connection.execute(
            "SELECT password_hash FROM users WHERE email = ?", ("both@example.ru",)
        )
    }
    assert len(hashes) == 1


def test_credentials_change_reaches_every_row(client, connection, two_courses):
    connection.execute(
        "UPDATE users SET must_change_password = 1 WHERE email = ?",
        ("both@example.ru",),
    )
    _login(client, "both@example.ru", "Пароль123")
    response = client.post(
        "/change-credentials",
        data={
            "new_email": "common@example.ru",
            "new_password": "НовыйПароль1",
            "new_password_confirm": "НовыйПароль1",
            "confirm_change": "on",
        },
    )
    assert response.status_code == 302
    emails = {
        row["email"] for row in connection.execute("SELECT email FROM users")
    }
    assert emails == {"common@example.ru"}
    pending = connection.execute(
        "SELECT COUNT(*) FROM users WHERE must_change_password = 1"
    ).fetchone()[0]
    assert pending == 0


def test_siblings_cannot_block_the_new_login(client, connection, two_courses):
    """Свой же старый логин на другом курсе не мешает смене."""
    connection.execute(
        "UPDATE users SET must_change_password = 1 WHERE email = ?",
        ("both@example.ru",),
    )
    _login(client, "both@example.ru", "Пароль123")
    response = client.post(
        "/change-credentials",
        data={
            "new_email": "both@example.ru",
            "new_password": "НовыйПароль1",
            "new_password_confirm": "НовыйПароль1",
            "confirm_change": "on",
        },
    )
    assert response.status_code == 302
    assert auth.INVALID_CREDENTIALS not in response.get_data(as_text=True)


# --------------------------------------------------------------------------
# Первый вход: обязательная смена (FR-02, FR-03, AC-01, AC-02, AC-03)
# --------------------------------------------------------------------------


def test_temporary_password_forces_change(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    response = client.get("/")
    assert response.status_code == 302
    assert "/change-credentials" in response.headers["Location"]


def test_change_credentials_page_shows_temporary_login(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    page = client.get("/change-credentials").get_data(as_text=True)
    assert "temp@example.ru" in page
    assert "Подтверждаю смену логина и пароля" in page


def test_confirmation_checkbox_is_required(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    response = client.post(
        "/change-credentials",
        data={
            "new_email": "new@example.ru",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "НовыйПароль123",
        },
    )
    assert response.status_code == 400
    assert auth.CONFIRMATION_REQUIRED in response.get_data(as_text=True)
    assert _must_change(connection, world["temporary"]) == 1


def test_password_must_be_confirmed(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    response = client.post(
        "/change-credentials",
        data={
            "new_email": "new@example.ru",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "ДругойПароль123",
            "confirm_change": "on",
        },
    )
    assert response.status_code == 400
    assert "Пароли не совпадают." in response.get_data(as_text=True)


def test_taken_login_is_refused(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    response = client.post(
        "/change-credentials",
        data={
            "new_email": "STUDENT@example.ru",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "НовыйПароль123",
            "confirm_change": "on",
        },
    )
    assert response.status_code == 400
    assert "уже используется" in response.get_data(as_text=True)


def test_admin_password_minimum_is_eight(client, connection, world):
    _login(client, "admin@example.ru", "Пароль1234")
    connection.execute(
        "UPDATE users SET must_change_password = 1 WHERE id = ?", (world["admin"],)
    )
    response = client.post(
        "/change-credentials",
        data={
            "new_email": "newadmin@example.ru",
            "new_password": "1234567",
            "new_password_confirm": "1234567",
            "confirm_change": "on",
        },
    )
    assert response.status_code == 400
    assert "8 символов" in response.get_data(as_text=True)


def test_successful_credentials_change(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    response = client.post(
        "/change-credentials",
        data={
            "new_email": "new@example.ru",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "НовыйПароль123",
            "confirm_change": "on",
        },
    )
    assert response.status_code == 302
    assert _must_change(connection, world["temporary"]) == 0
    assert client.get("/").status_code == 200


def test_change_is_written_to_the_journal(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    client.post(
        "/change-credentials",
        data={
            "new_email": "new@example.ru",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "НовыйПароль123",
            "confirm_change": "on",
        },
    )
    row = connection.execute(
        "SELECT * FROM action_log WHERE action = 'auth.credentials_changed'"
    ).fetchone()
    assert row is not None
    assert row["actor_id"] == world["temporary"]
    assert "НовыйПароль123" not in (row["details_json"] or "")


def test_new_login_works_and_old_does_not(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    client.post(
        "/change-credentials",
        data={
            "new_email": "new@example.ru",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "НовыйПароль123",
            "confirm_change": "on",
        },
    )
    client.post("/logout")
    assert _login(client, "temp@example.ru", "Пароль123").status_code == 401
    assert _login(client, "new@example.ru", "НовыйПароль123").status_code == 302


def test_change_credentials_is_closed_for_normal_user(client, connection, world):
    _login(client, "student@example.ru", "Пароль123")
    assert client.get("/change-credentials").status_code == 302
    response = client.post(
        "/change-credentials",
        data={
            "new_email": "other@example.ru",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "НовыйПароль123",
            "confirm_change": "on",
        },
    )
    assert response.status_code == 302


def test_logout_works_even_before_credentials_change(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    response = client.post("/logout")
    assert response.status_code == 302
    row = connection.execute(
        "SELECT * FROM action_log WHERE action = 'auth.logout'"
    ).fetchone()
    assert row is not None
    assert row["actor_id"] == world["temporary"]


# --------------------------------------------------------------------------
# Самостоятельная смена пароля (FR-11, FR-12, AC-21)
# --------------------------------------------------------------------------


def test_change_password_page_is_available(client, connection, world):
    _login(client, "student@example.ru", "Пароль123")
    assert client.get("/change-password").status_code == 200


def test_change_password_wrong_current(client, connection, world):
    _login(client, "student@example.ru", "Пароль123")
    before = _hash_of(connection, world["student"])
    response = client.post(
        "/change-password",
        data={
            "current_password": "Неверный123",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "НовыйПароль123",
        },
    )
    assert response.status_code == 400
    assert auth.CURRENT_PASSWORD_INVALID in response.get_data(as_text=True)
    assert _hash_of(connection, world["student"]) == before


def test_successful_password_change(client, connection, world):
    _login(client, "student@example.ru", "Пароль123")
    before = _hash_of(connection, world["student"])
    response = client.post(
        "/change-password",
        data={
            "current_password": "Пароль123",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "НовыйПароль123",
        },
    )
    assert response.status_code == 302
    assert _hash_of(connection, world["student"]) != before
    client.post("/logout")
    assert _login(client, "student@example.ru", "Пароль123").status_code == 401
    assert _login(client, "student@example.ru", "НовыйПароль123").status_code == 302


def test_password_change_is_written_to_the_journal(client, connection, world):
    _login(client, "student@example.ru", "Пароль123")
    client.post(
        "/change-password",
        data={
            "current_password": "Пароль123",
            "new_password": "НовыйПароль123",
            "new_password_confirm": "НовыйПароль123",
        },
    )
    row = connection.execute(
        "SELECT * FROM action_log WHERE action = 'pwd.changed_by_user'"
    ).fetchone()
    assert row is not None
    assert row["actor_id"] == world["student"]
    assert "НовыйПароль123" not in (row["details_json"] or "")


def test_change_password_waits_for_credentials_change(client, connection, world):
    _login(client, "temp@example.ru", "Пароль123")
    response = client.get("/change-password")
    assert response.status_code == 302
    assert "/change-credentials" in response.headers["Location"]


def test_short_new_password_is_refused(client, connection, world):
    _login(client, "student@example.ru", "Пароль123")
    response = client.post(
        "/change-password",
        data={
            "current_password": "Пароль123",
            "new_password": "1234",
            "new_password_confirm": "1234",
        },
    )
    assert response.status_code == 400
    assert "5 символов" in response.get_data(as_text=True)


# --------------------------------------------------------------------------
# Выход (FR-06)
# --------------------------------------------------------------------------


def test_logout_clears_the_session(client, connection, world):
    _login(client, "student@example.ru", "Пароль123")
    assert client.post("/logout").status_code == 302
    with client.session_transaction() as session_data:
        assert access.SESSION_USER_KEY not in session_data
    assert client.get("/").status_code == 302


def test_logout_is_post_only(client, connection, world):
    _login(client, "student@example.ru", "Пароль123")
    assert client.get("/logout").status_code == 405


def test_guest_logout_is_safe(client, connection, world):
    assert client.post("/logout").status_code == 302
