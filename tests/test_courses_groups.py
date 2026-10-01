"""Курсы, группы, участники и кураторы.

Этап Э-3: FR-13…FR-23, BR-22…BR-27, AC-30, AC-31, AC-36, AC-37 плюс
проверки доступа по ролям (BR-33) и отсутствие паролей в журнале
(FR-64). Правила импорта (AC-24, AC-26) и архив заданий (AC-29)
относятся к Э-4 и Э-5 и проверяются в своих тестах.
"""

import json
import sqlite3

import pytest

from app import action_log, config, passwords
from tests.conftest import (
    add_curator_to_group,
    add_student_to_group,
    add_user,
    insert_course,
    insert_group,
)

ADMIN_LOGIN = ("admin@example.ru", "Пароль1234")


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(connection):
    """Курс, две группы, администратор, студент и куратор."""
    course = insert_course(connection, title="Основной курс")
    other = insert_course(connection, title="Другой курс")
    first_group = insert_group(connection, name="ФК-11", course_id=course)
    second_group = insert_group(connection, name="ФК-21", course_id=course)
    other_group = insert_group(connection, name="ФК-31", course_id=other)

    admin = add_user(
        connection,
        ADMIN_LOGIN[0],
        ADMIN_LOGIN[1],
        role="admin",
        full_name="Администратор Системы",
    )
    student = add_user(
        connection,
        "student@example.ru",
        "Пароль123",
        course_id=course,
        full_name="Студент Студентов",
    )
    add_student_to_group(connection, first_group, student)
    curator = add_user(
        connection,
        "curator@example.ru",
        "Пароль123",
        role="curator",
        course_id=course,
        full_name="Куратор Кураторов",
    )
    add_curator_to_group(connection, second_group, curator)
    return {
        "course": course,
        "other": other,
        "group": first_group,
        "second_group": second_group,
        "other_group": other_group,
        "admin": admin,
        "student": student,
        "curator": curator,
    }


@pytest.fixture
def empty_world(connection):
    """Только администратор: для проверки пустых состояний."""
    admin = add_user(
        connection,
        ADMIN_LOGIN[0],
        ADMIN_LOGIN[1],
        role="admin",
        full_name="Администратор Системы",
    )
    return {"admin": admin}


@pytest.fixture
def as_admin(client, world):
    response = client.post(
        "/login", data={"email": ADMIN_LOGIN[0], "password": ADMIN_LOGIN[1]}
    )
    assert response.status_code == 302
    return client


@pytest.fixture
def bare_admin(client, empty_world):
    response = client.post(
        "/login", data={"email": ADMIN_LOGIN[0], "password": ADMIN_LOGIN[1]}
    )
    assert response.status_code == 302
    return client


@pytest.fixture
def as_user(client, world):
    def _sign_in(email, password):
        return client.post("/login", data={"email": email, "password": password})

    return _sign_in


# --------------------------------------------------------------------------
# Помощники
# --------------------------------------------------------------------------


def _actions(connection, action=None):
    if action is None:
        rows = connection.execute("SELECT * FROM action_log ORDER BY id").fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM action_log WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [dict(row) for row in rows]


def _last(connection, action):
    entries = _actions(connection, action)
    assert entries, "В журнале нет записи {0}".format(action)
    return entries[-1]


def _row(connection, table, row_id):
    return connection.execute(
        "SELECT * FROM {0} WHERE id = ?".format(table), (row_id,)
    ).fetchone()


def _count(connection, table):
    return connection.execute("SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0]


def _seed_submission_tree(connection, course_id, student_id):
    """Задание, сдача, комментарий и файлы — для проверки каскада.

    Схема привязывает файл к комментарию (§7.10, миграция 003): строка
    лежит в ``submission_comment_files`` и ссылается на владельца через
    ``comment_id``, поэтому комментарий вставляется первым, а файл —
    после него.
    """
    assignment = connection.execute(
        "INSERT INTO assignments (course_id, title, order_index, created_at)"
        " VALUES (?, 'ДЗ 1', 1, '2026-01-01T00:00:00Z')",
        (course_id,),
    ).lastrowid
    connection.execute(
        "INSERT INTO assignment_files (assignment_id, original_name, stored_name,"
        " mime, size_bytes, uploaded_by, created_at)"
        " VALUES (?, 'ДЗ1.xlsx', 'stored-assignment', NULL, 10, ?, ?)",
        (assignment, student_id, "2026-01-01T00:00:00Z"),
    )
    submission = connection.execute(
        "INSERT INTO submissions (assignment_id, student_id, attempt_no, status,"
        " comment, submitted_at, created_at)"
        " VALUES (?, ?, 1, 'submitted', 'Работа', ?, ?)",
        (assignment, student_id, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ).lastrowid
    connection.execute(
        "INSERT INTO submission_files (submission_id, original_name, stored_name,"
        " mime, size_bytes, uploaded_by, created_at)"
        " VALUES (?, 'work.xlsx', 'stored-submission', NULL, 10, ?, ?)",
        (submission, student_id, "2026-01-01T00:00:00Z"),
    )
    comment = connection.execute(
        "INSERT INTO submission_comments (submission_id, author_id, body,"
        " created_at) VALUES (?, ?, 'Комментарий', ?)",
        (submission, student_id, "2026-01-01T00:00:00Z"),
    ).lastrowid
    connection.execute(
        "INSERT INTO submission_comment_files (submission_id, comment_id,"
        " original_name, stored_name, mime, size_bytes, uploaded_by, created_at)"
        " VALUES (?, ?, 'note.txt', 'stored-comment', NULL, 10, ?, ?)",
        (submission, comment, student_id, "2026-01-01T00:00:00Z"),
    )
    return assignment, submission


def _seed_submission(connection, course_id, student_id, comment="Готово"):
    assignment = connection.execute(
        "INSERT INTO assignments (course_id, title, order_index, created_at)"
        " VALUES (?, 'ДЗ 1', 1, '2026-01-01T00:00:00Z')",
        (course_id,),
    ).lastrowid
    submission = connection.execute(
        "INSERT INTO submissions (assignment_id, student_id, attempt_no, status,"
        " comment, submitted_at, created_at)"
        " VALUES (?, ?, 1, 'accepted', ?, ?, ?)",
        (assignment, student_id, comment, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ).lastrowid
    return assignment, submission


# --------------------------------------------------------------------------
# Доступ по ролям (BR-33, FR-07)
# --------------------------------------------------------------------------

# Маршруты, доступные по GET. Отдельно от POST-маршрутов: неверный
# метод даёт 405, а не 403, и подменять одно другим в проверке нельзя.
ADMIN_GET_ROUTES = (
    "/admin/courses",
    "/admin/courses/new",
    "/admin/courses/1/edit",
    "/admin/courses/1/delete",
    "/admin/courses/1/groups",
    "/admin/groups/1/edit",
    "/admin/groups/1/members",
)

# Из админских GET куратору открыто только чтение: группы, участники и
# задания своего курса. Кнопки правки в шаблонах ему не рисуются, а сами
# маршруты изменения остаются в ADMIN_POST_ROUTES и там закрыты.
CURATOR_READ_ROUTES = (
    "/admin/courses/1/groups",
    "/admin/groups/1/members",
)

# Маршруты, изменяющие состояние: только POST.
ADMIN_POST_ROUTES = (
    "/admin/groups/1/delete",
    "/admin/groups/1/members/new",
    "/admin/groups/1/members/2/edit",
    "/admin/groups/1/members/2/remove",
    "/admin/groups/1/members/2/password/view",
    "/admin/groups/1/members/2/password/reissue",
    "/admin/groups/1/curators/attach",
    "/admin/groups/1/curators/2/detach",
)


@pytest.mark.parametrize(
    "route",
    [route for route in ADMIN_GET_ROUTES if route not in CURATOR_READ_ROUTES],
)
def test_curator_is_forbidden_on_get(client, world, as_user, route):
    as_user("curator@example.ru", "Пароль123")
    assert client.get(route).status_code == 403


def test_curator_may_read_groups_and_members(client, world, as_user):
    as_user("curator@example.ru", "Пароль123")

    courses = client.get("/admin/courses/{0}/groups".format(world["course"]))
    assert courses.status_code == 200
    page = courses.get_data(as_text=True)
    assert "ФК-21" in page
    assert "Создать группу" not in page
    assert "Удалить курс" not in page

    members = client.get("/admin/groups/{0}/members".format(world["second_group"]))
    assert members.status_code == 200
    page = members.get_data(as_text=True)
    assert "Добавить участника" not in page
    assert "Показать пароль" not in page
    assert "Изменить данные участника" not in page


def test_curator_reads_foreign_group_as_missing(client, world, as_user):
    # Чужая группа — 404, а не 403: по коду нельзя узнать, существует
    # она вообще (FR-08).
    as_user("curator@example.ru", "Пароль123")
    response = client.get("/admin/groups/{0}/members".format(world["group"]))
    assert response.status_code == 404


def test_curator_reads_assignments_without_editing(client, world, as_user):
    """Куратор видит задания своего курса, но не кнопки правки."""
    as_user("curator@example.ru", "Пароль123")
    response = client.get(
        "/admin/courses/{0}/assignments".format(world["course"])
    )
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Добавить задание" not in page
    assert "О курсе" not in page
    assert "Группы и участники" in page


def test_student_still_forbidden_on_assignment_list(client, world, as_user):
    as_user("student@example.ru", "Пароль123")
    response = client.get(
        "/admin/courses/{0}/assignments".format(world["course"])
    )
    assert response.status_code == 403


def test_curator_cabinet_links_to_groups_and_assignments(client, world, as_user):
    """В кабинете куратора есть кнопки курсов, групп и заданий."""
    as_user("curator@example.ru", "Пароль123")
    text = client.get("/").get_data(as_text=True)
    assert "/admin/courses/{0}/groups".format(world["course"]) in text
    assert "/admin/courses/{0}/assignments".format(world["course"]) in text
    assert "/admin/groups/{0}/members".format(world["second_group"]) in text
    # Возврат в кабинет есть на рабочих экранах, но не на самом кабинете.
    assert "header-home" not in text


@pytest.mark.parametrize("route", ADMIN_GET_ROUTES)
def test_student_is_forbidden_on_get(client, world, as_user, route):
    as_user("student@example.ru", "Пароль123")
    assert client.get(route).status_code == 403


@pytest.mark.parametrize("route", ADMIN_POST_ROUTES)
def test_curator_is_forbidden_on_post(client, world, as_user, route):
    as_user("curator@example.ru", "Пароль123")
    assert client.post(route).status_code == 403


@pytest.mark.parametrize("route", ADMIN_POST_ROUTES)
def test_student_is_forbidden_on_post(client, world, as_user, route):
    as_user("student@example.ru", "Пароль123")
    assert client.post(route).status_code == 403


def test_post_only_routes_reject_get(as_admin, world):
    assert as_admin.get("/admin/groups/{0}/delete".format(world["group"])).status_code == 405
    assert (
        as_admin.get(
            "/admin/groups/{0}/members/{1}/remove".format(
                world["group"], world["student"]
            )
        ).status_code
        == 405
    )


def test_guest_is_redirected_to_login(client, world):
    assert client.get("/admin/courses").status_code == 302


def test_admin_sees_courses_of_any_course(as_admin, world):
    assert (
        as_admin.get("/admin/courses/{0}/groups".format(world["other"])).status_code
        == 200
    )


def test_missing_course_is_not_found(as_admin, world):
    assert as_admin.get("/admin/courses/9999/groups").status_code == 404


def test_missing_group_is_not_found(as_admin, world):
    assert as_admin.get("/admin/groups/9999/members").status_code == 404


# --------------------------------------------------------------------------
# Курсы: создание, список, правка (FR-13, FR-14, FR-15, AC-36)
# --------------------------------------------------------------------------


def test_course_is_created_and_logged(as_admin, connection, world):
    response = as_admin.post(
        "/admin/courses/new",
        data={"title": "Новый курс", "description": "Описание курса"},
    )
    assert response.status_code == 302
    course = connection.execute(
        "SELECT * FROM courses WHERE title = 'Новый курс'"
    ).fetchone()
    assert course is not None
    assert course["description"] == "Описание курса"
    entry = _last(connection, "course.created")
    assert entry["entity_type"] == "course"
    assert entry["entity_id"] == course["id"]
    assert entry["course_id"] == course["id"]
    assert entry["actor_id"] == world["admin"]


def test_course_description_is_optional(as_admin, connection, world):
    as_admin.post("/admin/courses/new", data={"title": "Без описания", "description": ""})
    course = connection.execute(
        "SELECT * FROM courses WHERE title = 'Без описания'"
    ).fetchone()
    assert course["description"] is None


def test_course_without_title_is_rejected(as_admin, connection, world):
    before = _count(connection, "courses")
    response = as_admin.post(
        "/admin/courses/new", data={"title": "   ", "description": ""}
    )
    assert response.status_code == 400
    assert config.MESSAGES["course_title_required"] in response.text
    assert _count(connection, "courses") == before


def test_course_title_longer_than_200_is_rejected(as_admin, connection, world):
    response = as_admin.post(
        "/admin/courses/new", data={"title": "Я" * 201, "description": ""}
    )
    assert response.status_code == 400
    assert config.MESSAGES["course_title_too_long"] in response.text
    assert _count(connection, "courses") == 2


def test_course_edits_are_saved_and_logged(as_admin, connection, world):
    course_id = world["course"]
    response = as_admin.post(
        "/admin/courses/{0}/edit".format(course_id),
        data={"title": "Переименованный", "description": "Новое описание"},
    )
    assert response.status_code == 302
    course = _row(connection, "courses", course_id)
    assert course["title"] == "Переименованный"
    assert course["description"] == "Новое описание"
    assert _last(connection, "course.updated")["entity_id"] == course_id


def test_course_edit_rejects_wrong_title(as_admin, connection, world):
    course_id = world["course"]
    response = as_admin.post(
        "/admin/courses/{0}/edit".format(course_id),
        data={"title": "", "description": "Описание"},
    )
    assert response.status_code == 400
    assert _row(connection, "courses", course_id)["title"] == "Основной курс"


def test_course_form_is_prefilled(as_admin, world):
    response = as_admin.get("/admin/courses/{0}/edit".format(world["course"]))
    assert response.status_code == 200
    assert "Основной курс" in response.text


def test_courses_list_shows_all_courses(as_admin, world):
    response = as_admin.get("/admin/courses")
    assert response.status_code == 200
    assert "Основной курс" in response.text
    assert "Другой курс" in response.text


def test_courses_list_empty_state(bare_admin):
    response = bare_admin.get("/admin/courses")
    assert config.EMPTY_STATES["admin_courses"] in response.text


# --------------------------------------------------------------------------
# Удаление курса (FR-16, FR-17, BR-22, AC-31)
# --------------------------------------------------------------------------


def test_course_delete_shows_consequences_and_asks_title(as_admin, world):
    response = as_admin.get("/admin/courses/{0}/delete".format(world["course"]))
    assert response.status_code == 200
    assert "Введите название курса для подтверждения" in response.text
    assert "заданий" in response.text
    assert "участников" in response.text
    assert "работ" in response.text


def test_course_delete_requires_exact_title(as_admin, connection, world):
    course_id = world["course"]
    response = as_admin.post(
        "/admin/courses/{0}/delete".format(course_id),
        data={"confirmation": "Основной"},
    )
    assert response.status_code == 400
    assert config.MESSAGES["course_confirm_mismatch"] in response.text
    assert _row(connection, "courses", course_id) is not None


def test_course_delete_without_confirmation_deletes_nothing(as_admin, connection, world):
    course_id = world["course"]
    response = as_admin.post(
        "/admin/courses/{0}/delete".format(course_id), data={"confirmation": ""}
    )
    assert response.status_code == 400
    assert _row(connection, "courses", course_id) is not None
    assert _row(connection, "groups", world["group"]) is not None


def test_course_delete_removes_whole_tree_and_keeps_log(as_admin, connection, world):
    course_id = world["course"]
    _seed_submission_tree(connection, course_id, world["student"])
    connection.execute(
        "INSERT INTO import_batches (actor_id, course_id, file_name, total_rows,"
        " valid_rows, created_at)"
        " VALUES (?, ?, 'Список.xlsx', 6, 4, '2026-01-01T00:00:00Z')",
        (world["admin"], course_id),
    )

    response = as_admin.post(
        "/admin/courses/{0}/delete".format(course_id),
        data={"confirmation": "Основной курс"},
    )
    assert response.status_code == 302

    assert _row(connection, "courses", course_id) is None
    assert _row(connection, "groups", world["group"]) is None
    assert _row(connection, "users", world["student"]) is None
    for table in (
        "assignments",
        "assignment_files",
        "submissions",
        "submission_files",
        "submission_comments",
        "submission_comment_files",
        "import_batches",
    ):
        assert _count(connection, table) == 0, table

    # Журнал сохраняется (FR-17, BR-22), автор удалённого курса обезличен.
    entry = _last(connection, "course.deleted")
    assert entry["entity_id"] == course_id
    details = json.loads(entry["details_json"])
    assert details["title"] == "Основной курс"
    assert details["members"] == 2
    # Чужой курс не тронут.
    assert _row(connection, "courses", world["other"]) is not None
    assert _row(connection, "users", world["curator"]) is None


def test_course_delete_anonymises_actor_of_removed_users(as_admin, connection, world):
    course_id = world["course"]
    connection.execute(
        "INSERT INTO action_log (actor_id, actor_role, action, entity_type,"
        " entity_id, course_id, created_at)"
        " VALUES (?, 'student', 'submission.created', 'submission', 1, ?, ?)",
        (world["student"], course_id, "2026-01-01T00:00:00Z"),
    )
    as_admin.post(
        "/admin/courses/{0}/delete".format(course_id),
        data={"confirmation": "Основной курс"},
    )
    row = connection.execute(
        "SELECT actor_id, actor_role FROM action_log WHERE action ="
        " 'submission.created'"
    ).fetchone()
    assert row["actor_id"] is None
    assert row["actor_role"] == "student"


def test_course_delete_keeps_own_log_entry_readable(as_admin, connection, world):
    course_id = world["course"]
    as_admin.post(
        "/admin/courses/{0}/delete".format(course_id),
        data={"confirmation": "Основной курс"},
    )
    entry = _last(connection, "course.deleted")
    # Администратор вне курса, поэтому его actor_id не обезличивается:
    # иначе запись «кто удалил» стала бы бесполезной (FR-12, BR-22).
    assert entry["actor_id"] == world["admin"]
    assert entry["actor_role"] == "admin"


def test_course_delete_removes_stored_files(as_admin, connection, world, workspace):
    uploads = workspace / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    for name in ("stored-assignment", "stored-submission", "stored-comment"):
        (uploads / name).write_bytes(b"x")
    _seed_submission_tree(connection, world["course"], world["student"])

    as_admin.post(
        "/admin/courses/{0}/delete".format(world["course"]),
        data={"confirmation": "Основной курс"},
    )
    assert sorted(path.name for path in uploads.iterdir()) == []


def test_deleted_course_leaves_no_login_for_its_users(
    as_admin, client, connection, world
):
    as_admin.post(
        "/admin/courses/{0}/delete".format(world["course"]),
        data={"confirmation": "Основной курс"},
    )
    client.post("/logout")
    # Учётная запись куратора удалена вместе с курсом, поэтому вход
    # невозможен, а не просто запрещён.
    response = client.post(
        "/login", data={"email": "curator@example.ru", "password": "Пароль123"}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------
# Группы (FR-18, FR-20, FR-22, FR-23, BR-25…BR-27, AC-30)
# --------------------------------------------------------------------------


def test_group_is_created_and_logged(as_admin, connection, world):
    response = as_admin.post(
        "/admin/courses/{0}/groups/new".format(world["course"]),
        data={"name": "Новая группа"},
    )
    assert response.status_code == 302
    group = connection.execute(
        "SELECT * FROM groups WHERE name = 'Новая группа'"
    ).fetchone()
    assert group["course_id"] == world["course"]
    assert group["archived_at"] is None
    entry = _last(connection, "group.created")
    assert entry["entity_id"] == group["id"]
    assert entry["group_id"] == group["id"]


def test_group_name_is_unique_inside_course(as_admin, connection, world):
    response = as_admin.post(
        "/admin/courses/{0}/groups/new".format(world["course"]),
        data={"name": "ФК-11"},
    )
    assert response.status_code == 400
    assert config.MESSAGES["group_name_taken"] in response.text
    assert _count(connection, "groups") == 3


def test_same_group_name_allowed_in_another_course(as_admin, connection, world):
    response = as_admin.post(
        "/admin/courses/{0}/groups/new".format(world["other"]),
        data={"name": "ФК-11"},
    )
    assert response.status_code == 302
    assert _count(connection, "groups") == 4


def test_group_without_name_is_rejected(as_admin, connection, world):
    response = as_admin.post(
        "/admin/courses/{0}/groups/new".format(world["course"]), data={"name": ""}
    )
    assert response.status_code == 400
    assert config.MESSAGES["group_name_required"] in response.text
    assert _count(connection, "groups") == 3


def test_group_is_renamed(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/edit".format(world["group"]), data={"name": "ФК-12"}
    )
    assert response.status_code == 302
    assert _row(connection, "groups", world["group"])["name"] == "ФК-12"
    assert _last(connection, "group.updated")["entity_id"] == world["group"]


def test_group_rename_rejects_duplicate(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/edit".format(world["group"]), data={"name": "ФК-21"}
    )
    assert response.status_code == 400
    assert _row(connection, "groups", world["group"])["name"] == "ФК-11"


def test_group_delete_creates_nothing_on_get(as_admin, connection, world):
    assert as_admin.get("/admin/groups/{0}/delete".format(world["group"])).status_code == 405


def test_nonempty_group_cannot_be_deleted(as_admin, connection, world):
    response = as_admin.post("/admin/groups/{0}/delete".format(world["group"]))
    assert response.status_code == 400
    assert config.MESSAGES["group_not_empty"].format(count=1) in response.text
    assert _row(connection, "groups", world["group"]) is not None
    assert _actions(connection, "group.archived") == []


def test_curator_also_blocks_group_deletion(as_admin, connection, world):
    group_id = world["second_group"]
    response = as_admin.post("/admin/groups/{0}/delete".format(group_id))
    assert response.status_code == 400
    assert config.MESSAGES["group_not_empty"].format(count=1) in response.text
    assert _row(connection, "groups", group_id) is not None


def test_group_with_student_and_curator_reports_total(as_admin, connection, world):
    add_curator_to_group(connection, world["group"], world["curator"])
    response = as_admin.post("/admin/groups/{0}/delete".format(world["group"]))
    assert config.MESSAGES["group_not_empty"].format(count=2) in response.text


def test_empty_group_is_physically_deleted(as_admin, connection, world):
    """BR-26: группа без участников и сдач удаляется физически."""
    empty = insert_group(connection, name="Пустая", course_id=world["course"])
    response = as_admin.post("/admin/groups/{0}/delete".format(empty))
    assert response.status_code == 302
    assert _row(connection, "groups", empty) is None
    entry = _last(connection, "group.updated")
    assert entry["entity_id"] == empty
    assert json.loads(entry["details_json"])["deleted"] is True


def test_group_with_submissions_is_blocked_while_member_present(
    as_admin, connection, world
):
    """FR-22, BR-25, Р-02: сдачи нельзя уничтожить удалением группы.

    Пока в группе есть студент с работами, удаление запрещено — это
    и есть то состояние, ради которого FR-23 требует архив.
    """
    empty = insert_group(connection, name="С работами", course_id=world["course"])
    other_student = add_user(
        connection,
        "second@example.ru",
        "Пароль123",
        course_id=world["course"],
        full_name="Второй Второй",
    )
    add_student_to_group(connection, empty, other_student)
    _assignment, submission = _seed_submission(
        connection, world["course"], other_student
    )

    response = as_admin.post("/admin/groups/{0}/delete".format(empty))
    assert response.status_code == 400
    assert config.MESSAGES["group_not_empty"].format(count=1) in response.text
    assert _row(connection, "groups", empty)["archived_at"] is None
    assert _row(connection, "submissions", submission) is not None


def test_group_becomes_deletable_and_keeps_works_after_member_excluded(
    as_admin, connection, world
):
    """Обходной путь: исключили студента — группа удаляется, работы нет.

    Работы принадлежат студенту и заданию, а не группе, поэтому
    физическое удаление пустой группы их не затрагивает (В-01).
    """
    empty = insert_group(connection, name="С работами", course_id=world["course"])
    other_student = add_user(
        connection,
        "second@example.ru",
        "Пароль123",
        course_id=world["course"],
        full_name="Второй Второй",
    )
    add_student_to_group(connection, empty, other_student)
    _assignment, submission = _seed_submission(
        connection, world["course"], other_student
    )

    as_admin.post(
        "/admin/groups/{0}/members/{1}/remove".format(empty, other_student)
    )
    assert _row(connection, "users", other_student)["is_active"] == 0

    response = as_admin.post("/admin/groups/{0}/delete".format(empty))
    assert response.status_code == 302
    assert _row(connection, "groups", empty) is None
    assert _row(connection, "submissions", submission) is not None
    assert _actions(connection, "group.archived") == []


def test_archived_group_is_hidden_from_student(client, connection, world, as_user):
    connection.execute(
        "UPDATE groups SET archived_at = '2026-02-01T00:00:00Z' WHERE id = ?",
        (world["group"],),
    )
    as_user("student@example.ru", "Пароль123")
    cabinet = client.get("/")
    assert cabinet.status_code == 200
    assert "ФК-11" not in cabinet.text
    assert "Основной курс" in cabinet.text


def test_archived_group_is_hidden_from_curator(client, connection, world, as_user):
    connection.execute(
        "UPDATE groups SET archived_at = '2026-02-01T00:00:00Z' WHERE id = ?",
        (world["second_group"],),
    )
    as_user("curator@example.ru", "Пароль123")
    cabinet = client.get("/")
    assert cabinet.status_code == 200
    assert "ФК-21" not in cabinet.text


def test_archived_group_still_visible_to_admin(as_admin, connection, world):
    connection.execute(
        "UPDATE groups SET archived_at = '2026-02-01T00:00:00Z' WHERE id = ?",
        (world["second_group"],),
    )
    response = as_admin.get("/admin/courses/{0}/groups".format(world["course"]))
    assert "В архиве" in response.text
    assert "ФК-21" in response.text


def test_archived_group_accepts_no_new_members(as_admin, connection, world):
    connection.execute(
        "UPDATE groups SET archived_at = '2026-02-01T00:00:00Z' WHERE id = ?",
        (world["second_group"],),
    )
    response = as_admin.post(
        "/admin/groups/{0}/members/new".format(world["second_group"]),
        data={
            "full_name": "Новый Новый",
            "role": "student",
            "email": "new@example.ru",
            "password": "Пароль123",
        },
    )
    assert response.status_code == 403


def test_archived_group_accepts_no_curator(as_admin, connection, world):
    connection.execute(
        "UPDATE groups SET archived_at = '2026-02-01T00:00:00Z' WHERE id = ?",
        (world["group"],),
    )
    response = as_admin.post(
        "/admin/groups/{0}/curators/attach".format(world["group"]),
        data={"user_id": str(world["curator"])},
    )
    assert response.status_code == 403


def test_archived_group_cannot_be_renamed(as_admin, connection, world):
    connection.execute(
        "UPDATE groups SET archived_at = '2026-02-01T00:00:00Z' WHERE id = ?",
        (world["group"],),
    )
    response = as_admin.get("/admin/groups/{0}/edit".format(world["group"]))
    assert response.status_code == 403


def test_groups_list_shows_counts(as_admin, world):
    response = as_admin.get("/admin/courses/{0}/groups".format(world["course"]))
    assert response.status_code == 200
    assert "студентов — 1" in response.text
    assert "кураторов — 1" in response.text


def test_groups_list_empty_state(as_admin, connection, world):
    fresh = insert_course(connection, title="Пустой курс")
    response = as_admin.get("/admin/courses/{0}/groups".format(fresh))
    assert config.EMPTY_STATES["course_groups"] in response.text


# --------------------------------------------------------------------------
# Участники (FR-19, FR-20, AC-37)
# --------------------------------------------------------------------------

MEMBER_FORM = {
    "full_name": "Новый Новый",
    "role": "student",
    "email": "new@example.ru",
    "password": "Пароль123",
}


def test_member_is_added_with_password_and_log(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]), data=MEMBER_FORM
    )
    assert response.status_code == 302
    member = connection.execute(
        "SELECT * FROM users WHERE email = ?", (MEMBER_FORM["email"],)
    ).fetchone()
    assert member["full_name"] == "Новый Новый"
    assert member["role"] == "student"
    assert member["course_id"] == world["course"]
    assert member["is_active"] == 1
    assert member["must_change_password"] == 0
    assert passwords.verify_password(
        MEMBER_FORM["password"], member["password_hash"]
    )
    assert connection.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (world["group"], member["id"]),
    ).fetchone() is not None
    entry = _last(connection, "group.member_added")
    assert entry["entity_id"] == member["id"]
    assert entry["group_id"] == world["group"]
    assert "парол" not in (entry["details_json"] or "").lower()


def test_member_password_is_encrypted_and_revealed(as_admin, connection, world):
    as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]), data=MEMBER_FORM
    )
    member = connection.execute(
        "SELECT * FROM users WHERE email = ?", (MEMBER_FORM["email"],)
    ).fetchone()
    assert passwords.decrypt_password(member["password_enc"]) == MEMBER_FORM["password"]
    response = as_admin.post(
        "/admin/groups/{0}/members/{1}/password/view".format(
            world["group"], member["id"]
        )
    )
    assert response.status_code == 200
    assert MEMBER_FORM["password"] in response.text
    assert _last(connection, "pwd.viewed_by_admin")["entity_id"] == member["id"]


def test_password_of_another_group_is_not_found(as_admin, world):
    response = as_admin.post(
        "/admin/groups/{0}/members/{1}/password/view".format(
            world["second_group"], world["student"]
        )
    )
    assert response.status_code == 404


def test_password_view_is_not_available_to_curator(client, world, as_user):
    as_user("curator@example.ru", "Пароль123")
    response = client.post(
        "/admin/groups/{0}/members/{1}/password/view".format(
            world["group"], world["student"]
        )
    )
    assert response.status_code == 403


def test_password_reissue_breaks_old_password(as_admin, connection, world):
    as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]), data=MEMBER_FORM
    )
    member = connection.execute(
        "SELECT * FROM users WHERE email = ?", (MEMBER_FORM["email"],)
    ).fetchone()
    old_hash = member["password_hash"]
    response = as_admin.post(
        "/admin/groups/{0}/members/{1}/password/reissue".format(
            world["group"], member["id"]
        )
    )
    assert response.status_code == 200
    fresh = _row(connection, "users", member["id"])
    assert fresh["password_hash"] != old_hash
    assert not passwords.verify_password(MEMBER_FORM["password"], fresh["password_hash"])
    assert fresh["must_change_password"] == 0
    assert _last(connection, "pwd.reissued_by_admin")["entity_id"] == member["id"]


def test_reissued_password_works_on_login(client, as_admin, connection, world):
    as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]), data=MEMBER_FORM
    )
    member = connection.execute(
        "SELECT * FROM users WHERE email = ?", (MEMBER_FORM["email"],)
    ).fetchone()
    as_admin.post(
        "/admin/groups/{0}/members/{1}/password/reissue".format(
            world["group"], member["id"]
        )
    )
    fresh = _row(connection, "users", member["id"])
    new_password = passwords.reveal_password(fresh)
    client.post("/logout")
    response = client.post(
        "/login", data={"email": MEMBER_FORM["email"], "password": new_password}
    )
    assert response.status_code == 302


def test_curator_role_in_member_form_attaches_curator(as_admin, connection, world):
    form = dict(MEMBER_FORM, role="curator", email="curator2@example.ru")
    response = as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]), data=form
    )
    assert response.status_code == 302
    curator = connection.execute(
        "SELECT * FROM users WHERE email = ?", (form["email"],)
    ).fetchone()
    assert curator["role"] == "curator"
    assert connection.execute(
        "SELECT 1 FROM curator_groups WHERE group_id = ? AND user_id = ?",
        (world["group"], curator["id"]),
    ).fetchone() is not None
    assert _last(connection, "curator.attached")["entity_id"] == curator["id"]


@pytest.mark.parametrize(
    "overrides,message_key",
    [
        ({"full_name": "  "}, "full_name_required"),
        ({"full_name": "Я" * 201}, "full_name_too_long"),
        ({"role": "преподаватель"}, "role_invalid"),
        ({"email": "нет-atsign.ru"}, "email_invalid"),
        ({"email": "student@example.ru"}, "email_taken"),
        ({"password": "1234"}, "user_password_short"),
        ({"password": ""}, "user_password_short"),
    ],
)
def test_member_form_rejects_bad_input(as_admin, connection, world, overrides, message_key):
    before = _count(connection, "users")
    form = dict(MEMBER_FORM, **overrides)
    response = as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]), data=form
    )
    assert response.status_code == 400
    assert config.MESSAGES[message_key] in response.text
    assert _count(connection, "users") == before


def test_password_of_exactly_five_characters_is_accepted(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]),
        data=dict(MEMBER_FORM, password="12345"),
    )
    assert response.status_code == 302
    assert _count(connection, "users") == 4


def test_member_email_is_unique_in_course(as_admin, connection, world):
    """Один e-mail на двух курсах допустим (BR-06, §4.5).

    Здесь проверяется обратное: тот же адрес в том же курсе занят.
    """
    response = as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]),
        data=dict(MEMBER_FORM, email="curator@example.ru"),
    )
    assert response.status_code == 400
    assert config.MESSAGES["email_taken"] in response.text


def test_same_email_in_another_course_is_allowed(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/members/new".format(world["other_group"]),
        data=dict(MEMBER_FORM, email="student@example.ru", full_name="Тёзка Тёзкин"),
    )
    assert response.status_code == 302
    twin = connection.execute(
        "SELECT * FROM users WHERE email = ? AND course_id = ?",
        ("student@example.ru", world["other"]),
    ).fetchone()
    assert twin is not None
    assert twin["full_name"] == "Тёзка Тёзкин"


def test_member_fields_are_edited(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/members/{1}/edit".format(
            world["group"], world["student"]
        ),
        data={"full_name": "Переименованный", "email": "renamed@example.ru"},
    )
    assert response.status_code == 302
    member = _row(connection, "users", world["student"])
    assert member["full_name"] == "Переименованный"
    assert member["email"] == "renamed@example.ru"
    assert _last(connection, "group.member_updated")["entity_id"] == world["student"]


def test_member_edit_keeps_own_email_without_error(as_admin, connection, world):
    """Повторное сохранение формы не считается конфликтом с самим собой."""
    response = as_admin.post(
        "/admin/groups/{0}/members/{1}/edit".format(
            world["group"], world["student"]
        ),
        data={"full_name": "Студент Студентов", "email": "student@example.ru"},
    )
    assert response.status_code == 302
    assert _row(connection, "users", world["student"])["email"] == "student@example.ru"


def test_member_edit_rejects_invalid_email(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/members/{1}/edit".format(
            world["group"], world["student"]
        ),
        data={"full_name": "Переименованный", "email": "broken"},
    )
    assert response.status_code == 400
    member = _row(connection, "users", world["student"])
    assert member["email"] == "student@example.ru"
    assert member["full_name"] == "Студент Студентов"


def test_member_edit_rejects_foreign_email(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/members/{1}/edit".format(
            world["group"], world["student"]
        ),
        data={"full_name": "Студент", "email": "curator@example.ru"},
    )
    assert response.status_code == 400
    assert _row(connection, "users", world["student"])["email"] == "student@example.ru"


def test_member_removed_from_group_deactivates_account(as_admin, connection, world):
    _assignment, submission = _seed_submission(
        connection, world["course"], world["student"]
    )
    response = as_admin.post(
        "/admin/groups/{0}/members/{1}/remove".format(
            world["group"], world["student"]
        )
    )
    assert response.status_code == 302
    assert connection.execute(
        "SELECT 1 FROM group_members WHERE user_id = ?", (world["student"],)
    ).fetchone() is None
    member = _row(connection, "users", world["student"])
    assert member["is_active"] == 0
    # Прошлые работы сохраняются (допущение В-01).
    assert _row(connection, "submissions", submission) is not None
    entry = _last(connection, "group.member_removed")
    assert entry["entity_id"] == world["student"]
    assert json.loads(entry["details_json"])["deactivated"] is True


def test_removed_member_email_stays_taken(as_admin, connection, world):
    as_admin.post(
        "/admin/groups/{0}/members/{1}/remove".format(
            world["group"], world["student"]
        )
    )
    response = as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]),
        data=dict(MEMBER_FORM, email="student@example.ru"),
    )
    assert response.status_code == 400
    assert config.MESSAGES["email_taken"] in response.text


def test_member_kept_active_in_another_group(as_admin, connection, world):
    """Исключение из одной группы не гасит учётную запись (В-01)."""
    third = insert_group(connection, name="ФК-12", course_id=world["course"])
    add_student_to_group(connection, third, world["student"])
    as_admin.post(
        "/admin/groups/{0}/members/{1}/remove".format(
            world["group"], world["student"]
        )
    )
    assert _row(connection, "users", world["student"])["is_active"] == 1
    assert connection.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (third, world["student"]),
    ).fetchone() is not None


def test_removed_member_loses_login_access(client, as_admin, connection, world):
    as_admin.post(
        "/admin/groups/{0}/members/{1}/remove".format(
            world["group"], world["student"]
        )
    )
    client.post("/logout")
    response = client.post(
        "/login", data={"email": "student@example.ru", "password": "Пароль123"}
    )
    assert response.status_code == 401


def test_group_becomes_deletable_after_removing_members(as_admin, connection, world):
    group_id = world["group"]
    as_admin.post(
        "/admin/groups/{0}/members/{1}/remove".format(group_id, world["student"])
    )
    response = as_admin.post("/admin/groups/{0}/delete".format(group_id))
    assert response.status_code == 302
    assert _row(connection, "groups", group_id) is None


def test_curator_is_detached_instead_of_removed(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/curators/{1}/detach".format(
            world["second_group"], world["curator"]
        )
    )
    assert response.status_code == 302
    assert _row(connection, "users", world["curator"])["is_active"] == 1


def test_foreign_member_is_not_found(as_admin, world):
    response = as_admin.post(
        "/admin/groups/{0}/members/{1}/edit".format(
            world["group"], world["curator"]
        ),
        data={"full_name": "Кража", "email": "krazha@example.ru"},
    )
    assert response.status_code == 404


def test_members_page_lists_students_and_curators(as_admin, connection, world):
    add_curator_to_group(connection, world["group"], world["curator"])
    response = as_admin.get("/admin/groups/{0}/members".format(world["group"]))
    assert response.status_code == 200
    assert "Студент Студентов" in response.text
    assert "Куратор Кураторов" in response.text
    assert "Администратор" in response.text


def test_members_empty_state(as_admin, connection, world):
    empty = insert_group(connection, name="Пустая", course_id=world["course"])
    response = as_admin.get("/admin/groups/{0}/members".format(empty))
    assert config.EMPTY_STATES["group_members"] in response.text


# --------------------------------------------------------------------------
# Кураторы (FR-21, AC-37)
# --------------------------------------------------------------------------


def test_curator_is_attached_and_sees_group(as_admin, client, connection, world, as_user):
    response = as_admin.post(
        "/admin/groups/{0}/curators/attach".format(world["group"]),
        data={"user_id": str(world["curator"])},
    )
    assert response.status_code == 302
    assert connection.execute(
        "SELECT 1 FROM curator_groups WHERE group_id = ? AND user_id = ?",
        (world["group"], world["curator"]),
    ).fetchone() is not None
    entry = _last(connection, "curator.attached")
    assert entry["entity_id"] == world["curator"]
    assert entry["group_id"] == world["group"]

    client.post("/logout")
    as_user("curator@example.ru", "Пароль123")
    assert "ФК-11" in client.get("/").text


def test_curator_attach_twice_is_rejected(as_admin, connection, world):
    response = as_admin.post(
        "/admin/groups/{0}/curators/attach".format(world["second_group"]),
        data={"user_id": str(world["curator"])},
    )
    assert response.status_code == 400
    assert config.MESSAGES["curator_already_attached"] in response.text


def test_curator_may_be_attached_to_several_groups(as_admin, connection, world):
    for group_id in (world["group"], world["second_group"]):
        as_admin.post(
            "/admin/groups/{0}/curators/attach".format(group_id),
            data={"user_id": str(world["curator"])},
        )
    rows = connection.execute(
        "SELECT group_id FROM curator_groups WHERE user_id = ?", (world["curator"],)
    ).fetchall()
    assert {row["group_id"] for row in rows} == {
        world["group"],
        world["second_group"],
    }


def test_attach_requires_curator_role(as_admin, world):
    response = as_admin.post(
        "/admin/groups/{0}/curators/attach".format(world["group"]),
        data={"user_id": str(world["student"])},
    )
    assert response.status_code == 404


def test_attach_without_choice_is_rejected(as_admin, world):
    response = as_admin.post(
        "/admin/groups/{0}/curators/attach".format(world["group"]), data={"user_id": ""}
    )
    assert response.status_code == 400
    assert config.MESSAGES["curator_required"] in response.text


def test_attach_unknown_user_is_not_found(as_admin, world):
    response = as_admin.post(
        "/admin/groups/{0}/curators/attach".format(world["group"]),
        data={"user_id": "9999"},
    )
    assert response.status_code == 404


def test_curator_detached_stops_seeing_group(as_admin, client, connection, world, as_user):
    response = as_admin.post(
        "/admin/groups/{0}/curators/{1}/detach".format(
            world["second_group"], world["curator"]
        )
    )
    assert response.status_code == 302
    assert connection.execute(
        "SELECT 1 FROM curator_groups WHERE group_id = ?", (world["second_group"],)
    ).fetchone() is None
    entry = _last(connection, "curator.detached")
    assert entry["entity_id"] == world["curator"]
    assert entry["group_id"] == world["second_group"]

    client.post("/logout")
    as_user("curator@example.ru", "Пароль123")
    assert "ФК-21" not in client.get("/").text


def test_detach_unknown_curator_is_not_found(as_admin, world):
    response = as_admin.post(
        "/admin/groups/{0}/curators/{1}/detach".format(world["group"], world["student"])
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Пароли в журнале (FR-64, AC-22)
# --------------------------------------------------------------------------


def test_no_plain_password_reaches_the_journal(as_admin, connection, world):
    as_admin.post(
        "/admin/groups/{0}/members/new".format(world["group"]), data=MEMBER_FORM
    )
    member = connection.execute(
        "SELECT id FROM users WHERE email = ?", (MEMBER_FORM["email"],)
    ).fetchone()
    as_admin.post(
        "/admin/groups/{0}/members/{1}/password/view".format(
            world["group"], member["id"]
        )
    )
    as_admin.post(
        "/admin/groups/{0}/members/{1}/password/reissue".format(
            world["group"], member["id"]
        )
    )
    for entry in _actions(connection):
        payload = (entry["details_json"] or "").lower()
        assert MEMBER_FORM["password"] not in payload
        assert "парол" not in payload
        assert "password" not in payload


def test_journal_rejects_password_details():
    with pytest.raises(action_log.UnsafeDetailsError):
        action_log.log_action(
            "group.member_added",
            actor_id=1,
            actor_role="admin",
            entity_type="user",
            details={"password": "Пароль123"},
            connection=sqlite3.connect(":memory:"),
        )


def test_journal_rejects_unknown_action():
    with pytest.raises(action_log.UnknownJournalValueError):
        action_log.log_action(
            "group.obliterated",
            actor_id=1,
            actor_role="admin",
            entity_type="group",
            connection=sqlite3.connect(":memory:"),
        )


# --------------------------------------------------------------------------
# Кабинет администратора (checklist §1, §3)
# --------------------------------------------------------------------------


def test_admin_cabinet_has_course_buttons(as_admin):
    response = as_admin.get("/")
    assert response.status_code == 200
    assert "Создать курс" in response.text
    assert "Редактировать курс / группы студентов" in response.text


def test_course_edit_page_offers_group_and_delete(as_admin, world):
    response = as_admin.get("/admin/courses/{0}/edit".format(world["course"]))
    assert "Группы и участники" in response.text
    assert "Удалить курс" in response.text
