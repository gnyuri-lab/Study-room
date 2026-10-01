"""Статусы работы: проверка, автостатус и отзыв
(FR-45…FR-48, BR-17…BR-21, AC-11…AC-15).

Проверяются переходы, а не кнопки:

* «сдано» после дедлайна становится «сдано после дедлайна» — это
  считает система, у куратора нет такой кнопки (FR-47, BR-18);
* без дедлайна опоздание невозможно (BR-19);
* куратор меняет только работу «на проверке», причём «на доработку» —
  только с комментарием (BR-20, AC-12);
* терминальный статус меняет только администратор и только в доработку,
  отдельным действием журнала (BR-21, AC-15);
* чужая работа — 404, чужая роль — 403 (FR-49, BR-29, BR-33).
"""

from __future__ import annotations

import pytest

from app import config, passwords
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
    FOREIGN_LOGIN,
    STUDENT_LOGIN,
    insert_assignment,
    insert_submission,
    rows,
    work_file,
)

OTHER_LOGIN = ("other@example.ru", "Пароль123")


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(connection, key_path):
    """Курс с группой, студентом и куратором плюс администратор."""
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

    other_group = insert_group(connection, name="ФК-9", course_id=course)
    foreign_curator = add_user(
        connection,
        FOREIGN_LOGIN[0],
        FOREIGN_LOGIN[1],
        role="curator",
        course_id=course,
        full_name="Куратор Чужой",
    )
    add_curator_to_group(connection, other_group, foreign_curator)
    other_student = add_user(
        connection,
        OTHER_LOGIN[0],
        OTHER_LOGIN[1],
        course_id=course,
        full_name="Студент Чужой",
    )
    add_student_to_group(connection, other_group, other_student)

    return {
        "course": course,
        "group": group,
        "admin": admin,
        "curator": curator,
        "student": student,
        "other_group": other_group,
        "other_student": other_student,
        "foreign_curator": foreign_curator,
    }


def login(client, credentials):
    client.post("/logout")
    response = client.post(
        "/login",
        data={"email": credentials[0], "password": credentials[1]},
        follow_redirects=True,
    )
    assert "Выйти" in response.text
    return response


def as_admin(client):
    return login(client, ADMIN_LOGIN)


def as_curator(client):
    return login(client, CURATOR_LOGIN)


def as_student(client):
    return login(client, STUDENT_LOGIN)


def as_foreign_curator(client):
    return login(client, FOREIGN_LOGIN)


def set_status(client, submission_id, requested="accepted", comment="Хорошо.",
               files=None):
    data = {"requested": requested, "comment": comment}
    if files is not None:
        data["files"] = files
    return client.post(
        "/curator/submissions/{0}/status".format(submission_id),
        data=data,
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def revert(client, submission_id, comment="Верните вторую часть."):
    return client.post(
        "/curator/submissions/{0}/revert".format(submission_id),
        data={"comment": comment},
        follow_redirects=True,
    )


def status_of(connection, submission_id):
    row = connection.execute(
        "SELECT status FROM submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    return row["status"]


# --------------------------------------------------------------------------
# Автостатус «сдано» (BR-17, BR-18, BR-19, AC-11, FR-47)
# --------------------------------------------------------------------------


def test_accept_before_deadline(client, connection, world):
    """Сдано вовремя остаётся «сдано» (BR-17, AC-12)."""
    assignment = insert_assignment(
        connection, course_id=world["course"], due_at="2026-10-01T23:59:00Z"
    )
    submission = insert_submission(
        connection, assignment, world["student"],
        submitted_at="2026-09-01T10:00:00Z",
    )
    as_curator(client)

    response = set_status(client, submission)

    assert "Статус работы: Сдано." in response.text
    assert status_of(connection, submission) == "accepted"
    entry = [
        item
        for item in rows(connection, "action_log")
        if item["action"] == "submission.status_changed"
    ]
    assert len(entry) == 1
    assert entry[0]["from_status"] == "submitted"
    assert entry[0]["to_status"] == "accepted"


def test_accept_after_deadline(client, connection, world):
    """Сдано позже дедлайна — «сдано после дедлайна» (BR-18, AC-11)."""
    assignment = insert_assignment(
        connection, course_id=world["course"], due_at="2026-08-31T23:59:00Z"
    )
    submission = insert_submission(
        connection, assignment, world["student"],
        submitted_at="2026-09-01T10:00:00Z",
    )
    as_curator(client)

    response = set_status(client, submission)

    assert "Статус работы: Сдано после дедлайна." in response.text
    assert status_of(connection, submission) == "accepted_late"


def test_accept_without_deadline(client, connection, world):
    """Без дедлайна опоздать нельзя: всегда «сдано» (BR-19, AC-13)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(
        connection, assignment, world["student"],
        submitted_at="2099-01-01T00:00:00Z",
    )
    as_curator(client)

    set_status(client, submission)

    assert status_of(connection, submission) == "accepted"


# --------------------------------------------------------------------------
# Ревизия и ограничения куратора (BR-20, AC-12, AC-13, AC-14)
# --------------------------------------------------------------------------


def test_revision_requires_comment(client, connection, world):
    """«На доработку» без комментария не сохраняется (FR-45)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_curator(client)

    response = set_status(client, submission, requested="revision", comment=" ")

    assert config.MESSAGES["status_comment_required"] in response.text
    assert status_of(connection, submission) == "submitted"


def test_revision_writes_comment_and_informs_student(client, connection, world):
    """Куратор ставит доработку, комментарий виден студенту (FR-45, FR-46)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_curator(client)

    response = set_status(
        client, submission, requested="revision",
        comment="Добавьте расчётную часть.",
    )

    assert "Статус работы: На доработку." in response.text
    assert status_of(connection, submission) == "revision"
    comments = rows(connection, "submission_comments")
    assert len(comments) == 1
    assert comments[0]["body"] == "Добавьте расчётную часть."

    as_student(client)
    page = client.get(
        "/student/courses/{0}/assignments/{1}".format(
            world["course"], assignment
        )
    )
    assert "Добавьте расчётную часть." in page.text


def test_status_change_only_from_on_check(client, connection, world):
    """Из «на доработку» статус не меняется (BR-20)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(
        connection, assignment, world["student"], status="revision"
    )
    as_curator(client)

    response = set_status(client, submission, requested="accepted")

    assert config.MESSAGES["status_not_on_review"].format(
        status="На доработку"
    ) in response.text
    assert status_of(connection, submission) == "revision"


def test_terminal_status_shows_message(client, connection, world):
    """AC-14: принятую работу не меняет ни куратор, — показан текст §9.3."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(
        connection, assignment, world["student"], status="accepted"
    )
    as_curator(client)

    response = set_status(client, submission, requested="revision")

    assert config.MESSAGES["status_terminal"] in response.text
    assert status_of(connection, submission) == "accepted"


def test_terminal_card_explains_without_form(client, connection, world):
    """Карточка принятой работы объясняет отказ вместо кнопки (AC-14)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(
        connection, assignment, world["student"], status="accepted"
    )
    as_curator(client)

    page = client.get("/curator/submissions/{0}".format(submission))

    assert page.status_code == 200
    assert config.MESSAGES["status_terminal"] in page.text
    assert "Сохранить статус" not in page.text


def test_submission_card_marks_deadline_red(client, connection, world):
    """Карточка работы куратора: подпись «Дедлайн» и дата красные,
    «не задан» приглушено (замечание приёмки 01.10.2026, FR-45)."""
    from app.services.assignments import format_due

    stored = "2026-10-15T21:59:00Z"
    assignment = insert_assignment(
        connection, course_id=world["course"], due_at=stored
    )
    submission = insert_submission(connection, assignment, world["student"])
    as_curator(client)

    page = client.get("/curator/submissions/{0}".format(submission))
    text = page.get_data(as_text=True)
    assert '<span class="deadline">Дедлайн:' in text
    # Точный вид даты зависит от пояса машины — сравниваем с тем, что
    # даёт тот же форматтер, что и сервис.
    assert format_due(stored) in text
    assert "не задан" not in text

    blank = insert_assignment(
        connection, course_id=world["course"], order_index=2
    )
    other = insert_submission(connection, blank, world["student"])
    page = client.get("/curator/submissions/{0}".format(other))
    assert '<span class="muted">не задан</span>' in page.get_data(as_text=True)


def test_unknown_status_choice(client, connection, world):
    """Нераспознанный выбор — отказ, а не случайный статус (BR-20)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_curator(client)

    response = set_status(client, submission, requested="deleted")

    assert config.MESSAGES["status_value_invalid"] in response.text
    assert status_of(connection, submission) == "submitted"


# --------------------------------------------------------------------------
# Отзыв проверки (FR-48, BR-21, AC-15)
# --------------------------------------------------------------------------


def test_revert_only_terminal_status(client, connection, world):
    """В доработку возвращают только принятую работу (BR-21)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_admin(client)

    response = revert(client, submission)

    assert config.MESSAGES["review_revert_only_terminal"] in response.text
    assert status_of(connection, submission) == "submitted"


def test_revert_only_admin(client, connection, world):
    """Куратор не отзывает принятие: у него 403 (BR-21, BR-33)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(
        connection, assignment, world["student"], status="accepted"
    )
    as_curator(client)

    response = revert(client, submission)

    assert response.status_code == 403
    assert status_of(connection, submission) == "accepted"


def test_admin_revert_writes_journal(client, connection, world):
    """Администратор возвращает работу в доработку (AC-15, сценарий А-2)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(
        connection, assignment, world["student"], status="accepted_late"
    )
    as_admin(client)

    response = revert(client, submission)

    assert "Работа возвращена на доработку." in response.text
    assert status_of(connection, submission) == "revision"
    reverted = [
        item
        for item in rows(connection, "action_log")
        if item["action"] == "submission.review_reverted"
    ]
    assert len(reverted) == 1
    assert reverted[0]["from_status"] == "accepted_late"
    assert reverted[0]["to_status"] == "revision"


def test_student_cannot_change_status(client, connection, world):
    """Студенту маршрут проверки не открыт (FR-45, BR-33)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_student(client)

    response = set_status(client, submission)

    assert response.status_code == 403
    assert status_of(connection, submission) == "submitted"


# --------------------------------------------------------------------------
# Доступ к чужой работе (FR-49, BR-29)
# --------------------------------------------------------------------------


def test_foreign_curator_gets_404(client, connection, world):
    """Куратор чужой группы не видит ни карточку, ни смену статуса."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_foreign_curator(client)

    page = client.get("/curator/submissions/{0}".format(submission))
    response = set_status(client, submission)

    assert page.status_code == 404
    assert response.status_code == 404
    assert status_of(connection, submission) == "submitted"

    # При этом своя группа тому же куратору открыта — отказ связан
    # с группой, а не с ролью (BR-29).
    own = insert_submission(
        connection, assignment, world["other_student"]
    )
    assert client.get(
        "/curator/submissions/{0}".format(own)
    ).status_code == 200


def test_own_group_card_is_open(client, connection, world):
    """Куратор группы открывает работу своего студента (BR-29)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_curator(client)

    page = client.get("/curator/submissions/{0}".format(submission))

    assert page.status_code == 200
    assert "Студент Первый" in page.text


# --------------------------------------------------------------------------
# Переписка и файлы комментария (FR-46, FR-50)
# --------------------------------------------------------------------------


def test_staff_comment_with_file(client, connection, world):
    """Файл к комментарию: строка, вложение и скачивание (FR-46)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_curator(client)

    response = client.post(
        "/curator/submissions/{0}/comments".format(submission),
        data={
            "body": "Проверил, всё верно.",
            "files": [work_file("замечания.txt")],
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert "Комментарий добавлен" in response.text
    comments = rows(connection, "submission_comments")
    assert len(comments) == 1
    files = rows(connection, "submission_comment_files")
    assert len(files) == 1
    assert files[0]["comment_id"] == comments[0]["id"]

    file_url = "/files/comment/{0}".format(files[0]["id"])
    as_student(client)
    assert client.get(file_url).status_code == 200

    login(client, OTHER_LOGIN)
    other_student = rows(
        connection, "users", "full_name = ?", ("Студент Чужой",)
    )[0]
    assert other_student["course_id"] == world["course"]
    assert client.get(file_url).status_code == 403


def test_staff_comment_requires_body_with_file(client, connection, world):
    """Файл без текста не принимается: тело комментария обязательно."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_curator(client)

    response = client.post(
        "/curator/submissions/{0}/comments".format(submission),
        data={"body": " ", "files": [work_file("замечания.txt")]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert config.MESSAGES["comment_body_required"] in response.text
    assert rows(connection, "submission_comments") == []


# --------------------------------------------------------------------------
# Список работ группы (FR-45, FR-49)
# --------------------------------------------------------------------------


def test_group_works_page_shows_statuses(client, connection, world):
    """Страница группы показывает студентов и статусы их работ (AC-18)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    insert_submission(connection, assignment, world["student"])
    as_curator(client)

    page = client.get(
        "/curator/groups/{0}/students".format(world["group"])
    )

    assert page.status_code == 200
    assert "Студент Первый" in page.text
    assert "На проверке" in page.text
    assert "Открыть работу" in page.text


def test_group_works_page_hidden_from_foreign_curator(client, connection, world):
    """Чужая группа закрыта и на списке работ (BR-29)."""
    as_foreign_curator(client)

    page = client.get(
        "/curator/groups/{0}/students".format(world["group"])
    )

    assert page.status_code == 404


def test_admin_sees_group_works(client, connection, world):
    """Администратор работает с теми же экранами, что и куратор (FR-45)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    insert_submission(connection, assignment, world["student"])
    as_admin(client)

    page = client.get(
        "/curator/groups/{0}/students".format(world["group"])
    )

    assert page.status_code == 200
    assert "Студент Первый" in page.text
