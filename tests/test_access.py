"""Сессия, роли и область видимости (BR-28, BR-29, BR-30, BR-31, BR-33, FR-08)."""

import pytest
from flask import g
from werkzeug.exceptions import Forbidden, NotFound

from app import access
from tests.conftest import (
    add_curator_to_group,
    add_student_to_group,
    add_user,
    insert_course,
    insert_group,
)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    from app import passwords

    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(connection):
    """Два курса, три группы и по одной учётной записи каждой роли."""
    first_course = insert_course(connection, title="Первый курс")
    second_course = insert_course(connection, title="Второй курс")

    first_group = insert_group(connection, name="ФК-11", course_id=first_course)
    second_group = insert_group(connection, name="ФК-21", course_id=second_course)
    third_group = insert_group(connection, name="ФК-31", course_id=second_course)

    student = add_user(
        connection,
        "student@example.ru",
        "Пароль123",
        course_id=first_course,
        full_name="Студент Студентов",
    )
    add_student_to_group(connection, first_group, student)

    curator = add_user(
        connection,
        "curator@example.ru",
        "Пароль123",
        role="curator",
        course_id=second_course,
        full_name="Куратор Кураторов",
    )
    add_curator_to_group(connection, second_group, curator)
    add_curator_to_group(connection, third_group, curator)

    admin = add_user(
        connection,
        "admin@example.ru",
        "Пароль123",
        role="admin",
        full_name="Администратор Системы",
    )
    return {
        "first_course": first_course,
        "second_course": second_course,
        "first_group": first_group,
        "second_group": second_group,
        "third_group": third_group,
        "student": student,
        "curator": curator,
        "admin": admin,
    }


def _user(connection, user_id):
    return connection.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def _sign_in(connection, world, role):
    access.login_user(_user(connection, world[role]))


# --------------------------------------------------------------------------
# Текущий пользователь
# --------------------------------------------------------------------------


def test_no_session_means_no_user(as_app):
    assert access.current_user() is None
    assert access.is_authenticated() is False
    assert access.role() is None
    assert access.is_admin() is False


def test_login_user_puts_id_into_session(as_app, connection, world):
    _sign_in(connection, world, "student")
    assert access.is_authenticated()
    assert access.role() == "student"
    assert access.must_change_password() is False
    access.logout_user()
    assert access.current_user() is None


def test_inactive_user_loses_access_immediately(as_app, connection, world):
    _sign_in(connection, world, "student")
    assert access.is_authenticated()
    connection.execute(
        "UPDATE users SET is_active = 0 WHERE id = ?", (world["student"],)
    )
    g.pop("current_user", None)
    assert access.current_user() is None


def test_fetch_user_skips_inactive(as_app, connection, world):
    connection.execute(
        "UPDATE users SET is_active = 0 WHERE id = ?", (world["student"],)
    )
    assert access.fetch_user(world["student"]) is None


def test_find_active_by_email_ignores_case(as_app, connection, world):
    found = access.find_active_by_email("STUDENT@EXAMPLE.RU")
    assert [row["id"] for row in found] == [world["student"]]


def test_find_active_by_email_returns_all_courses(as_app, connection):
    first_course = insert_course(connection, title="Первый курс")
    second_course = insert_course(connection, title="Второй курс")
    add_user(connection, "both@example.ru", "Пароль123", course_id=first_course)
    add_user(connection, "both@example.ru", "Пароль123", course_id=second_course)
    assert len(access.find_active_by_email("both@example.ru")) == 2


# --------------------------------------------------------------------------
# Уникальность логина (BR-06, Х-03)
# --------------------------------------------------------------------------


def test_email_taken_in_same_course(as_app, connection, world):
    assert access.email_taken(
        "student@example.ru", course_id=world["first_course"]
    )


def test_email_is_case_insensitive(as_app, connection, world):
    assert access.email_taken(
        "STUDENT@EXAMPLE.RU", course_id=world["first_course"]
    )


def test_same_email_on_another_course_is_free(as_app, connection, world):
    assert not access.email_taken(
        "student@example.ru", course_id=world["second_course"]
    )


def test_excluding_self_frees_own_email(as_app, connection, world):
    assert not access.email_taken(
        "student@example.ru",
        course_id=world["first_course"],
        exclude_user_ids=(world["student"],),
    )


def test_admin_email_must_be_free_everywhere(as_app, connection, world):
    # Администратор вне курса: его логин не должен совпадать с чужим.
    assert not access.email_taken(
        "admin@example.ru", course_id=None, exclude_user_ids=(world["admin"],)
    )
    add_user(connection, "unique@example.ru", "Пароль123", role="admin")
    assert access.email_taken("unique@example.ru", course_id=None)


# --------------------------------------------------------------------------
# Область видимости (BR-28, BR-29)
# --------------------------------------------------------------------------


def test_admin_sees_all_courses_and_groups(as_app, connection, world):
    _sign_in(connection, world, "admin")
    assert access.visible_course_ids() == {
        world["first_course"],
        world["second_course"],
    }
    assert access.visible_group_ids() == {
        world["first_group"],
        world["second_group"],
        world["third_group"],
    }


def test_curator_sees_only_own_groups(as_app, connection, world):
    _sign_in(connection, world, "curator")
    assert access.visible_group_ids() == {
        world["second_group"],
        world["third_group"],
    }
    assert access.visible_course_ids() == {world["second_course"]}


def test_student_sees_own_course_and_group(as_app, connection, world):
    _sign_in(connection, world, "student")
    assert access.visible_group_ids() == {world["first_group"]}
    assert access.visible_course_ids() == {world["first_course"]}


def test_student_cannot_open_foreign_group(as_app, connection, world):
    _sign_in(connection, world, "student")
    assert access.can_view_group(world["second_group"]) is False
    with pytest.raises(NotFound):
        access.require_group(world["second_group"])


def test_curator_cannot_open_foreign_course(as_app, connection, world):
    _sign_in(connection, world, "curator")
    assert access.can_view_course(world["first_course"]) is False
    with pytest.raises(NotFound):
        access.require_course(world["first_course"])


def test_missing_course_is_not_found_not_forbidden(as_app, connection, world):
    _sign_in(connection, world, "student")
    with pytest.raises(NotFound):
        access.require_course(9999)


def test_forbidden_is_a_different_error(as_app, connection, world):
    _sign_in(connection, world, "student")
    with pytest.raises(Forbidden):
        access.abort_forbidden()


# --------------------------------------------------------------------------
# Владение записью
# --------------------------------------------------------------------------


def test_owns_and_self_or_admin(as_app, connection, world):
    _sign_in(connection, world, "student")
    student = _user(connection, world["student"])
    other = _user(connection, world["curator"])
    assert access.owns(student)
    assert not access.owns(other)
    assert access.self_or_admin(student)
    with pytest.raises(NotFound):
        access.require_self_or_admin(other)


def test_admin_may_open_any_record(as_app, connection, world):
    _sign_in(connection, world, "admin")
    access.require_self_or_admin(_user(connection, world["student"]))


# --------------------------------------------------------------------------
# Выдача полей учётной записи (BR-30, AC-22)
# --------------------------------------------------------------------------


def test_admin_sees_email_and_password(as_app, connection, world):
    _sign_in(connection, world, "admin")
    payload = access.serialize_user(_user(connection, world["student"]), "admin")
    assert payload["email"] == "student@example.ru"
    assert payload["password"] == "Пароль123"


@pytest.mark.parametrize("viewer_role", ["curator", "student", None])
def test_other_roles_never_see_email_or_password(
    as_app, connection, world, viewer_role
):
    _sign_in(connection, world, "student")
    payload = access.serialize_user(
        _user(connection, world["student"]), viewer_role
    )
    assert "email" not in payload
    assert "password" not in payload
    assert access.SECRET_FIELDS == ("email", "password")
    assert "Пароль123" not in str(payload)


# --------------------------------------------------------------------------
# Декораторы (BR-31, BR-33)
# --------------------------------------------------------------------------


def test_login_required_sends_guest_to_login(app, client):
    @app.get("/__guard")
    @access.login_required
    def guarded():
        return "ok"

    response = client.get("/__guard")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert "__guard" in response.headers["Location"]


def test_admin_required_forbids_non_admin(app, client, connection, world):
    @app.get("/__admin")
    @access.admin_required
    def admin_only():
        return "ok"

    with client.session_transaction() as session:
        session[access.SESSION_USER_KEY] = world["student"]
    response = client.get("/__admin")
    assert response.status_code == 403
    assert "У вас нет прав на это действие." in response.text


def test_admin_required_allows_admin(app, client, connection, world):
    @app.get("/__admin")
    @access.admin_required
    def admin_only():
        return "ok"

    with client.session_transaction() as session:
        session[access.SESSION_USER_KEY] = world["admin"]
    assert client.get("/__admin").status_code == 200


def test_admin_required_sends_guest_to_login(app, client):
    @app.get("/__admin")
    @access.admin_required
    def admin_only():
        return "ok"

    response = client.get("/__admin")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
