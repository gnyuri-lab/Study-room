"""Журнал действий (FR-12, FR-61…FR-64).

Отдельно проверяется то, что журнал не может стать утечкой: пароль
нельзя положить в details, а IP-адрес в схеме нет вообще.
"""

import json

import pytest

from app import action_log, config


@pytest.fixture
def log_row(connection, course):
    return action_log.log_action(
        "course.created",
        actor_id=None,
        actor_role="admin",
        entity_type="course",
        entity_id=course,
        course_id=course,
        connection=connection,
    )


def _read(connection, row_id):
    return connection.execute(
        "SELECT * FROM action_log WHERE id = ?", (row_id,)
    ).fetchone()


# --------------------------------------------------------------------------
# Запись и возврат
# --------------------------------------------------------------------------


def test_log_action_returns_id_and_writes_row(connection, course):
    row_id = action_log.log_action(
        "course.created",
        actor_id=None,
        actor_role="admin",
        entity_type="course",
        entity_id=course,
        course_id=course,
        connection=connection,
    )
    row = _read(connection, row_id)
    assert row["action"] == "course.created"
    assert row["entity_type"] == "course"
    assert row["entity_id"] == course
    assert row["course_id"] == course
    assert row["created_at"].endswith("Z")
    assert row["details_json"] is None


def test_status_change_is_recorded_with_both_statuses(connection, course):
    row_id = action_log.log_action(
        "submission.status_changed",
        actor_id=None,
        actor_role="curator",
        entity_type="submission",
        from_status="submitted",
        to_status="revision",
        connection=connection,
    )
    row = _read(connection, row_id)
    assert row["from_status"] == "submitted"
    assert row["to_status"] == "revision"


def test_details_are_stored_as_json(connection, course):
    row_id = action_log.log_action(
        "group.created",
        actor_id=None,
        actor_role="admin",
        entity_type="group",
        details={"название": "ФК-11", "студентов": 4},
        connection=connection,
    )
    assert json.loads(_read(connection, row_id)["details_json"]) == {
        "название": "ФК-11",
        "студентов": 4,
    }


def test_actor_may_be_null(connection, course):
    # Запись об удалении курса остаётся, а автор обезличивается.
    row_id = action_log.log_action(
        "course.deleted",
        actor_id=None,
        actor_role="admin",
        entity_type="course",
        connection=connection,
    )
    assert _read(connection, row_id)["actor_id"] is None


# --------------------------------------------------------------------------
# Справочник §7.12
# --------------------------------------------------------------------------


def test_unknown_action_is_rejected(connection):
    with pytest.raises(action_log.UnknownJournalValueError):
        action_log.log_action(
            "что-то.новое",
            actor_id=None,
            actor_role="admin",
            entity_type="course",
            connection=connection,
        )


def test_unknown_entity_type_is_rejected(connection):
    with pytest.raises(action_log.UnknownJournalValueError):
        action_log.log_action(
            "course.created",
            actor_id=None,
            actor_role="admin",
            entity_type="galaxy",
            connection=connection,
        )


def test_unknown_status_is_rejected(connection):
    with pytest.raises(action_log.UnknownJournalValueError):
        action_log.log_action(
            "submission.status_changed",
            actor_id=None,
            actor_role="admin",
            entity_type="submission",
            to_status="finished",
            connection=connection,
        )


def test_all_statuses_from_the_spec_are_accepted(connection):
    for status in config.STATUS_ORDER:
        action_log.log_action(
            "submission.status_changed",
            actor_id=None,
            actor_role="admin",
            entity_type="submission",
            to_status=status,
            connection=connection,
        )
    assert connection.execute(
        "SELECT COUNT(*) FROM action_log"
    ).fetchone()[0] == len(config.STATUS_ORDER)


# --------------------------------------------------------------------------
# Пароли и секреты в details (FR-64, О-11)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "details",
    [
        {"password": "Пароль123"},
        {"new_password": "Пароль123"},
        {"пароль": "Пароль123"},
        {"user": {"password": "Пароль123"}},
        {"items": [{"pwd": "Пароль123"}]},
        {"secret": "abc"},
        {"ключ": "abc"},
    ],
)
def test_secret_like_keys_are_rejected(connection, course, details):
    with pytest.raises(action_log.UnsafeDetailsError):
        action_log.log_action(
            "course.created",
            actor_id=None,
            actor_role="admin",
            entity_type="course",
            connection=connection,
            details=details,
    )
    assert connection.execute("SELECT COUNT(*) FROM action_log").fetchone()[0] == 0


def test_harmless_details_pass(connection, course):
    row_id = action_log.log_action(
        "course.created",
        actor_id=None,
        actor_role="admin",
        entity_type="course",
        connection=connection,
        details={"попыток": 2, "групп": ["ФК-11"]},
    )
    assert _read(connection, row_id)["details_json"] is not None


def test_assert_details_safe_accepts_empty():
    action_log.assert_details_safe(None)
    action_log.assert_details_safe({})


# --------------------------------------------------------------------------
# Схема: чего в журнале быть не должно
# --------------------------------------------------------------------------


def test_no_ip_columns(connection):
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(action_log)").fetchall()
    }
    assert "ip" not in columns
    assert "ip_address" not in columns
    assert not [name for name in columns if "ip" in name.lower()]


def test_no_foreign_keys_so_actor_can_be_cleared(key_path, connection, course):
    from tests.conftest import add_user

    user_id = add_user(
        connection, "a@b.ru", "Пароль123", course_id=course
    )
    row_id = action_log.log_action(
        "auth.login",
        actor_id=user_id,
        actor_role="student",
        entity_type="user",
        entity_id=user_id,
        connection=connection,
    )
    connection.execute("UPDATE action_log SET actor_id = NULL WHERE id = ?", (row_id,))
    assert _read(connection, row_id)["actor_id"] is None


def test_utc_now_iso_is_utc():
    stamp = action_log.utc_now_iso()
    assert stamp.endswith("Z")
    assert "T" in stamp
    assert len(stamp) == 20
