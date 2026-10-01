"""Сдача работы: попытки, лимит, файлы и доступ
(FR-38…FR-44, FR-49…FR-54, BR-13…BR-16, AC-07…AC-10).

Проверяются решения этапа, а не «страница открылась»:

* одна отправка — одна строка ``submissions`` и один номер попытки,
  файлы лежат под сгенерированным именем (FR-38, FR-54);
* порядок отказов: лимит важнее статуса, и после трёх попыток студент
  читает про лимит, даже когда работа «на проверке» (BR-16, AC-10);
* из «на проверке» и из принятого статуса пересдачи нет (BR-14, BR-15);
* право на работу и на её файлы: автор, куратор своей группы,
  администратор; остальным — 404 или 403 (FR-49, FR-53, BR-29).
"""

from __future__ import annotations

import io

import pytest

from app import attachments, config, passwords
from tests.conftest import (
    add_curator_to_group,
    add_student_to_group,
    add_user,
    insert_course,
    insert_group,
)

ADMIN_LOGIN = ("admin@example.ru", "Пароль1234")
CURATOR_LOGIN = ("curator@example.ru", "Пароль123")
STUDENT_LOGIN = ("student@example.ru", "Пароль123")
OTHER_LOGIN = ("other@example.ru", "Пароль123")
FOREIGN_LOGIN = ("foreign@example.ru", "Пароль123")


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(connection, key_path):
    """Два курса: в каждом группа, студент и куратор, плюс администратор."""
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

    other_course = insert_course(connection, title="Другой курс")
    other_group = insert_group(connection, name="ФК-9", course_id=other_course)
    foreign_curator = add_user(
        connection,
        FOREIGN_LOGIN[0],
        FOREIGN_LOGIN[1],
        role="curator",
        course_id=other_course,
        full_name="Куратор Чужой",
    )
    add_curator_to_group(connection, other_group, foreign_curator)
    other_student = add_user(
        connection,
        OTHER_LOGIN[0],
        OTHER_LOGIN[1],
        course_id=other_course,
        full_name="Студент Чужой",
    )
    add_student_to_group(connection, other_group, other_student)

    return {
        "course": course,
        "group": group,
        "admin": admin,
        "curator": curator,
        "student": student,
        "other_course": other_course,
        "other_group": other_group,
        "other_student": other_student,
        "foreign_curator": foreign_curator,
    }


def login(client, credentials):
    """Войти под указанной учётной записью (см. test_assignments)."""
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


def as_other(client):
    return login(client, OTHER_LOGIN)


def as_foreign_curator(client):
    return login(client, FOREIGN_LOGIN)


def insert_assignment(
    conn,
    course_id=1,
    title="Домашняя работа 1",
    due_at=None,
    order_index=1,
    archived_at=None,
    created_at="2026-01-01T00:00:00Z",
):
    cursor = conn.execute(
        """
        INSERT INTO assignments (
            course_id, title, description, due_at, order_index,
            archived_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (course_id, title, "Решить задачи", due_at, order_index,
         archived_at, created_at),
    )
    return cursor.lastrowid


def insert_submission(
    conn,
    assignment_id,
    student_id,
    attempt_no=1,
    status="submitted",
    comment="Отчёты во вложении",
    submitted_at="2026-09-01T10:00:00Z",
):
    cursor = conn.execute(
        """
        INSERT INTO submissions (
            assignment_id, student_id, attempt_no, status, comment,
            submitted_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            assignment_id,
            student_id,
            attempt_no,
            status,
            comment,
            submitted_at,
            submitted_at,
        ),
    )
    return cursor.lastrowid


def work_file(name="отчёт.txt", content="текст работы\n"):
    return (io.BytesIO(content.encode("utf-8")), name)


def submit(
    client,
    world,
    assignment_id,
    comment="Отчёты во вложении",
    files=None,
):
    data = {"comment": comment}
    if files is not None:
        data["files"] = files
    return client.post(
        "/student/courses/{0}/assignments/{1}/submit".format(
            world["course"], assignment_id
        ),
        data=data,
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def rows(connection, table, where="", args=()):
    query = "SELECT * FROM {0}".format(table)
    if where:
        query += " WHERE " + where
    return [dict(row) for row in connection.execute(query, args).fetchall()]


def log_actions(connection):
    return [
        entry["action"]
        for entry in rows(connection, "action_log")
    ]


def limit_message(used=3, limit=3):
    return config.MESSAGES["submission_limit_reached"].format(
        used=used, limit=limit
    )


# --------------------------------------------------------------------------
# Запись попытки (FR-38, FR-39, AC-07)
# --------------------------------------------------------------------------


def test_submit_writes_attempt_and_stores_file(client, connection, world):
    """Отправка создаёт строку и кладёт файл под своим именем (AC-07)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    as_student(client)

    response = submit(
        client, world, assignment, files=[work_file("отчёт.txt")]
    )
    assert "Работа отправлена на проверку" in response.text

    saved = rows(connection, "submissions")
    assert len(saved) == 1
    assert saved[0]["attempt_no"] == 1
    assert saved[0]["status"] == "submitted"
    assert saved[0]["comment"] == "Отчёты во вложении"

    files = rows(connection, "submission_files")
    assert len(files) == 1
    assert files[0]["original_name"] == "отчёт.txt"
    # Хранение под сгенерированным именем (FR-54, NFR-06): исходное имя
    # остаётся только в базе для показа, путь по нему не строится.
    assert files[0]["stored_name"] != "отчёт.txt"
    assert "submission.created" in log_actions(connection)


def test_submit_requires_comment(client, connection, world):
    """Пустой комментарий — отказ, ни строки, ни файла (FR-38)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    as_student(client)

    response = submit(client, world, assignment, comment="   ")

    assert config.MESSAGES["submission_comment_required"] in response.text
    assert rows(connection, "submissions") == []
    assert rows(connection, "submission_files") == []


def test_comment_length_is_limited(client, connection, world):
    """Комментарий длиннее 4000 символов не сохраняется (§7.9)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    as_student(client)

    response = submit(client, world, assignment, comment="ы" * 4001)

    assert config.MESSAGES["submission_comment_too_long"] in response.text
    assert rows(connection, "submissions") == []


def test_submit_rejects_sixth_file(client, connection, world):
    """Шестой файл отклоняется целиком (FR-52, §9.3)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    as_student(client)

    files = [work_file("файл-{0}.txt".format(i)) for i in range(6)]
    response = submit(client, world, assignment, files=files)

    assert config.MESSAGES["files_limit_submission"] in response.text
    assert rows(connection, "submissions") == []
    assert rows(connection, "submission_files") == []


def test_resubmission_after_revision(client, connection, world):
    """Из «на доработку» сдача открыта: попытка номер два (BR-13)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    insert_submission(connection, assignment, world["student"], status="revision")
    as_student(client)

    response = submit(client, world, assignment)

    assert "попытка 2 из 3" in response.text
    saved = rows(connection, "submissions")
    assert [item["attempt_no"] for item in saved] == [1, 2]


def test_submit_blocked_on_check(client, connection, world):
    """Из «на проверке» пересдачи нет (BR-14, AC-08)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    insert_submission(connection, assignment, world["student"])
    as_student(client)

    response = submit(client, world, assignment)

    assert config.MESSAGES["submission_on_check"] in response.text
    assert len(rows(connection, "submissions")) == 1


def test_submit_blocked_when_accepted(client, connection, world):
    """Принятую работу пересдать нельзя (BR-15)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    insert_submission(connection, assignment, world["student"], status="accepted")
    as_student(client)

    response = submit(client, world, assignment)

    assert config.MESSAGES["submission_accepted"] in response.text
    assert len(rows(connection, "submissions")) == 1


def test_attempt_limit_wins_over_status(client, connection, world):
    """AC-10: после трёх попыток отказ про лимит, даже когда работа
    «на проверке» — BR-16 проверяется раньше BR-14."""
    assignment = insert_assignment(connection, course_id=world["course"])
    for attempt in (1, 2, 3):
        insert_submission(
            connection,
            assignment,
            world["student"],
            attempt_no=attempt,
            status="submitted" if attempt == 3 else "revision",
        )
    as_student(client)

    response = submit(client, world, assignment)

    assert limit_message(3, 3) in response.text
    assert config.MESSAGES["submission_on_check"] not in response.text
    assert len(rows(connection, "submissions")) == 3


def test_archived_assignment_is_closed(client, connection, world):
    """Архивное задание не показывается и не принимает работы (FR-23)."""
    assignment = insert_assignment(
        connection,
        course_id=world["course"],
        archived_at="2026-09-01T00:00:00Z",
    )
    as_student(client)

    page = client.get(
        "/student/courses/{0}/assignments/{1}".format(
            world["course"], assignment
        )
    )
    response = submit(client, world, assignment)

    assert page.status_code == 404
    assert response.status_code == 404
    assert rows(connection, "submissions") == []


# --------------------------------------------------------------------------
# История и переписка (FR-44, FR-50)
# --------------------------------------------------------------------------


def test_card_shows_history_and_thread(client, connection, world):
    """Карточка показывает обе попытки, общую переписку и форму (AC-09)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    first = insert_submission(
        connection, assignment, world["student"], status="revision"
    )
    insert_submission(connection, assignment, world["student"], attempt_no=2)
    connection.execute(
        """
        INSERT INTO submission_comments (submission_id, author_id, body, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (first, world["curator"], "Добавьте вторую часть.", "2026-09-02T09:00:00Z"),
    )
    as_student(client)

    response = client.get(
        "/student/courses/{0}/assignments/{1}".format(
            world["course"], assignment
        )
    )

    assert response.status_code == 200
    assert "Попытка 1" in response.text
    assert "Попытка 2" in response.text
    # Комментарий куратора к первой попытке остаётся в переписке,
    # хотя сдача уже вторая (FR-50, AC-09).
    assert "Добавьте вторую часть." in response.text
    assert "Добавить комментарий" in response.text


def test_student_comments_own_work(client, connection, world):
    """Студент пишет в переписку своей работы (FR-50)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_student(client)

    response = client.post(
        "/student/submissions/{0}/comments".format(submission),
        data={"body": "Поправил в файле."},
        follow_redirects=True,
    )

    assert "Комментарий добавлен" in response.text
    saved = rows(connection, "submission_comments")
    assert len(saved) == 1
    assert saved[0]["body"] == "Поправил в файле."


def test_student_comment_requires_body(client, connection, world):
    """Пустой комментарий не записывается (FR-50)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_student(client)

    response = client.post(
        "/student/submissions/{0}/comments".format(submission),
        data={"body": " "},
        follow_redirects=True,
    )

    assert config.MESSAGES["comment_body_required"] in response.text
    assert rows(connection, "submission_comments") == []


def test_student_comment_files_limit(client, connection, world):
    """К одному комментарию — не больше пяти файлов (§7.10, FR-46)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_student(client)
    limit = config.MAX_FILES_PER_COMMENT

    response = client.post(
        "/student/submissions/{0}/comments".format(submission),
        data={
            "body": "Лишнее вложение.",
            "files": [work_file("f{0}.txt".format(i)) for i in range(limit + 1)],
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert config.MESSAGES["files_limit_comment"] in response.text
    assert rows(connection, "submission_comments") == []


def test_student_comment_several_files(client, connection, world):
    """Несколько файлов одного комментария привязаны к нему (FR-46)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    as_student(client)

    response = client.post(
        "/student/submissions/{0}/comments".format(submission),
        data={
            "body": "Две вложения.",
            "files": [work_file("a.txt"), work_file("b.txt")],
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert "Комментарий добавлен" in response.text
    comments = rows(connection, "submission_comments")
    assert len(comments) == 1
    files = rows(connection, "submission_comment_files")
    assert len(files) == 2
    assert {item["comment_id"] for item in files} == {comments[0]["id"]}


def test_student_cannot_comment_foreign_work(client, connection, world):
    """Чужая работа не видна даже по прямому номеру (FR-49)."""
    assignment = insert_assignment(connection, course_id=world["other_course"])
    submission = insert_submission(
        connection, assignment, world["other_student"]
    )
    as_student(client)

    response = client.post(
        "/student/submissions/{0}/comments".format(submission),
        data={"body": "Прочитаю чужое"},
        follow_redirects=True,
    )

    assert response.status_code == 404
    assert rows(connection, "submission_comments") == []


# --------------------------------------------------------------------------
# Курсы студента (AC-04, BR-28)
# --------------------------------------------------------------------------


def test_my_courses_page_counts_work(client, connection, world):
    """«Мои курсы» считает прогресс по последней попытке (AC-04).

    Первая сдача осталась «на доработке», но её уже закрыла вторая:
    счётчик показывает положение работы сейчас, а не историю.
    """
    assignment = insert_assignment(connection, course_id=world["course"])
    insert_submission(
        connection, assignment, world["student"], status="revision"
    )
    insert_submission(connection, assignment, world["student"], attempt_no=2)
    as_student(client)

    response = client.get("/student/courses")

    assert response.status_code == 200
    assert "Основной курс" in response.text
    assert "Заданий: 1" in response.text
    assert "на проверке: 1" in response.text
    assert "на доработке: 0" in response.text
    assert "принято: 0" in response.text


# --------------------------------------------------------------------------
# Право на работу и файлы (FR-49, FR-53, BR-29)
# --------------------------------------------------------------------------


def test_download_rights_follow_the_work(client, connection, world):
    """Файл работы скачивает автор, куратор группы и администратор;
    чужим — отказ (FR-53, BR-29)."""
    assignment = insert_assignment(connection, course_id=world["course"])
    submission = insert_submission(connection, assignment, world["student"])
    connection.execute(
        """
        INSERT INTO submission_files (
            submission_id, original_name, stored_name, mime,
            size_bytes, uploaded_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            submission,
            "отчёт.txt",
            "gen-1.txt",
            "text/plain",
            11,
            world["student"],
            "2026-09-01T10:00:00Z",
        ),
    )
    file_id = rows(connection, "submission_files")[0]["id"]
    url = "/files/submission/{0}".format(file_id)
    # Файл должен лежать на диске: маршрут отвечает 404 и на потерянный
    # файл (NFR-06), поэтому кладём настоящее содержимое.
    attachments.uploads_dir().mkdir(parents=True, exist_ok=True)
    attachments.stored_path("gen-1.txt").write_bytes(b"hello world")

    as_student(client)
    assert client.get(url).status_code == 200

    as_curator(client)
    assert client.get(url).status_code == 200

    as_admin(client)
    assert client.get(url).status_code == 200

    as_foreign_curator(client)
    assert client.get(url).status_code == 403

    as_other(client)
    assert client.get(url).status_code == 403
    assert "file.downloaded" in log_actions(connection)
