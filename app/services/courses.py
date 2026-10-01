"""Курсы: создание, правка, перечень и каскадное удаление (FR-13…FR-17).

Удаление курса — единственное место в приложении, где нужно удалять
связанные строки вручную. ``ON DELETE CASCADE`` в схеме намеренно
нет (см. комментарий в ``migrations/001_schema.sql``): база не даёт
удалить родителя раньше потомков, поэтому неверный порядок приводит
к ошибке, а не к тихой потере данных. Порядок задан здесь и покрыт
тестами.

**Описание курса не ограничено по длине.** §7.4 не задаёт верхней
границы, ограничение в схеме тоже отсутствует. Константа
``COURSE_DESCRIPTION_MAX_LENGTH`` в ``app/config.py`` этому требованию
противоречит и на этом этапе не применяется; вопрос вынесен в отчёт
этапа, а не решён молча.

Имя файла при удалении курса получается из ``stored_name`` и не
участвует в построении пути (NFR-06, FR-54): на диске удаляются
только файлы, имена которых лежат в базе, и только внутри
``config.UPLOADS_DIR``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app import access, action_log, config, db
from app.services import ServiceError

#: Таблицы, из которых нужно собрать имена файлов перед удалением.
FILE_TABLES = ("assignment_files", "submission_files", "submission_comment_files")

#: Порядок каскадного удаления курса. Список читается сверху вниз и
#: проверяется тестом ``test_course_delete_order_is_respected``: каждый
#: потомок удаляется раньше родителя, иначе сработает внешний ключ.
CASCADE_ORDER = (
    # Файлы ссылаются на комментарий (comment_id, миграция 003), поэтому
    # сначала вложения, потом сами комментарии, потом сдача.
    "submission_comment_files",
    "submission_comments",
    "submission_files",
    "submissions",
    "assignment_files",
    "assignments",
    "curator_groups",
    "group_members",
    "groups",
    "import_errors",
    "import_batches",
    "users",
    "courses",
)


def _connection() -> sqlite3.Connection:
    return db.get_db()


# --------------------------------------------------------------------------
# Чтение
# --------------------------------------------------------------------------


def list_courses() -> list[dict[str, Any]]:
    """Все курсы с числом групп и участников (FR-15).

    Сортировка по названию — это единственное упорядочивание в
    приложении: §4.2 запрещает отдельные средства сортировки, а
    предсказуемый порядок строк нужен, чтобы список не «прыгал».
    """
    rows = _connection().execute(
        """
        SELECT c.id, c.title, c.description, c.created_at,
               (SELECT COUNT(*) FROM groups g
                 WHERE g.course_id = c.id) AS groups_count,
               (SELECT COUNT(*) FROM users u
                 WHERE u.course_id = c.id) AS members_count
          FROM courses c
         ORDER BY c.title, c.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_course(course_id: int) -> sqlite3.Row | None:
    return _connection().execute(
        "SELECT * FROM courses WHERE id = ?", (course_id,)
    ).fetchone()


def deletion_preview(course_id: int) -> dict[str, int]:
    """Число заданий, групп, участников и работ курса (FR-16).

    Показывается до подтверждения удаления, поэтому считается здесь, а
    не в шаблоне: числа обязаны совпадать с тем, что удалится.
    """
    connection = _connection()
    assignments = connection.execute(
        "SELECT COUNT(*) FROM assignments WHERE course_id = ?", (course_id,)
    ).fetchone()[0]
    groups = connection.execute(
        "SELECT COUNT(*) FROM groups WHERE course_id = ?", (course_id,)
    ).fetchone()[0]
    members = connection.execute(
        "SELECT COUNT(*) FROM users WHERE course_id = ?", (course_id,)
    ).fetchone()[0]
    submissions = connection.execute(
        """
        SELECT COUNT(*)
          FROM submissions s
          JOIN assignments a ON a.id = s.assignment_id
         WHERE a.course_id = ?
        """,
        (course_id,),
    ).fetchone()[0]
    return {
        "assignments": assignments,
        "groups": groups,
        "members": members,
        "submissions": submissions,
    }


def confirm_text(preview: dict[str, int]) -> str:
    """Текст подтверждения из §9.3 с подставленными числами."""
    return (
        "Удаление необратимо. Будут удалены: заданий — {assignments}, "
        "групп — {groups}, участников — {members}, работ — {submissions}. "
        "Введите название курса для подтверждения."
    ).format(**preview)


# --------------------------------------------------------------------------
# Квалификация
# --------------------------------------------------------------------------


def validate_title(title: str) -> str:
    """Название курса: не пусто и не длиннее 200 символов (§7.4)."""
    candidate = (title or "").strip()
    if not candidate:
        raise ServiceError(config.MESSAGES["course_title_required"])
    if len(candidate) > config.COURSE_TITLE_MAX_LENGTH:
        raise ServiceError(config.MESSAGES["course_title_too_long"])
    return candidate


def validate_description(description: str | None) -> str | None:
    """Описание необязательно (§7.4); пустая строка хранится как NULL."""
    if description is None:
        return None
    candidate = description.strip()
    return candidate or None


# --------------------------------------------------------------------------
# Создание и правка
# --------------------------------------------------------------------------


def create_course(*, title: str, description: str | None, actor: sqlite3.Row) -> int:
    """Создать курс и записать ``course.created`` (FR-13, AC-36)."""
    clean_title = validate_title(title)
    clean_description = validate_description(description)
    with db.transaction() as connection:
        cursor = connection.execute(
            "INSERT INTO courses (title, description, created_at)"
            " VALUES (?, ?, ?)",
            (clean_title, clean_description, action_log.utc_now_iso()),
        )
        course_id = cursor.lastrowid
        action_log.log_action(
            "course.created",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="course",
            entity_id=course_id,
            course_id=course_id,
            connection=connection,
        )
    return course_id


def update_course(
    *, course_id: int, title: str, description: str | None, actor: sqlite3.Row
) -> None:
    """Сохранить новые название и описание (FR-14, AC-36)."""
    course = fetch_course(course_id)
    if course is None:
        access.abort_not_found()
    clean_title = validate_title(title)
    clean_description = validate_description(description)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE courses SET title = ?, description = ? WHERE id = ?",
            (clean_title, clean_description, course_id),
        )
        action_log.log_action(
            "course.updated",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="course",
            entity_id=course_id,
            course_id=course_id,
            connection=connection,
        )


# --------------------------------------------------------------------------
# Удаление (FR-16, FR-17, BR-22)
# --------------------------------------------------------------------------


def _stored_names(connection: sqlite3.Connection, course_id: int) -> list[str]:
    """Имена файлов курса для удаления с диска (FR-17, NFR-06)."""
    names: list[str] = []
    assignment_ids = [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM assignments WHERE course_id = ?", (course_id,)
        ).fetchall()
    ]
    if assignment_ids:
        placeholders = ", ".join("?" * len(assignment_ids))
        for row in connection.execute(
            "SELECT stored_name FROM assignment_files"
            f" WHERE assignment_id IN ({placeholders})",
            assignment_ids,
        ).fetchall():
            names.append(row["stored_name"])
    submissions = [
        row["id"]
        for row in connection.execute(
            """
            SELECT s.id
              FROM submissions s
              JOIN assignments a ON a.id = s.assignment_id
             WHERE a.course_id = ?
            """,
            (course_id,),
        ).fetchall()
    ]
    if submissions:
        placeholders = ", ".join("?" * len(submissions))
        for table in ("submission_files", "submission_comment_files"):
            for row in connection.execute(
                f"SELECT stored_name FROM {table}"
                f" WHERE submission_id IN ({placeholders})",
                submissions,
            ).fetchall():
                names.append(row["stored_name"])
    return names


def _delete_stored_files(names: list[str]) -> None:
    """Удалить файлы курса с диска.

    Вызывается после коммита: сначала запись в базе, потом файл. Если
    файл уже отсутствует, это не ошибка — база была источником истины.
    """
    uploads = Path(config.UPLOADS_DIR)
    for name in names:
        target = uploads / name
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # Каталог недоступен: запись останется, удаление курса уже
            # состоялось, и об этом лучше не сообщать как об ошибке 500.
            continue


def delete_course(
    *, course_id: int, confirmation: str, actor: sqlite3.Row
) -> dict[str, int]:
    """Удалить курс физически вместе со всеми связанными данными.

    Требует ввода названия курса: без совпадения ничего не удаляется
    (FR-16, BR-22). Записи журнала сохраняются, а автор записей
    удалённых пользователей обезличивается (``actor_id`` становится
    NULL) — в самой таблице внешних ключей нет (Д-11).
    """
    course = fetch_course(course_id)
    if course is None:
        access.abort_not_found()
    if (confirmation or "").strip() != course["title"]:
        raise ServiceError(config.MESSAGES["course_confirm_mismatch"])

    with db.transaction() as connection:
        preview = deletion_preview(course_id)
        stored_names = _stored_names(connection, course_id)
        author_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM users WHERE course_id = ?", (course_id,)
            ).fetchall()
        ]

        # Запись об удалении пишется первой: после удаления строк курса
        # в журнале не осталось бы ни автора, ни ссылки на объект.
        action_log.log_action(
            "course.deleted",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="course",
            entity_id=course_id,
            course_id=course_id,
            details={
                "title": course["title"],
                "assignments": preview["assignments"],
                "groups": preview["groups"],
                "members": preview["members"],
                "submissions": preview["submissions"],
            },
            connection=connection,
        )

        if author_ids:
            placeholders = ", ".join("?" * len(author_ids))
            connection.execute(
                "UPDATE action_log SET actor_id = NULL"
                f" WHERE actor_id IN ({placeholders})",
                author_ids,
            )

        for table in CASCADE_ORDER:
            if table == "submission_comments":
                connection.execute(
                    """
                    DELETE FROM submission_comments
                     WHERE submission_id IN (
                        SELECT s.id
                          FROM submissions s
                          JOIN assignments a ON a.id = s.assignment_id
                         WHERE a.course_id = ?
                     )
                    """,
                    (course_id,),
                )
            elif table == "submission_comment_files":
                connection.execute(
                    """
                    DELETE FROM submission_comment_files
                     WHERE submission_id IN (
                        SELECT s.id
                          FROM submissions s
                          JOIN assignments a ON a.id = s.assignment_id
                         WHERE a.course_id = ?
                     )
                    """,
                    (course_id,),
                )
            elif table == "submission_files":
                connection.execute(
                    """
                    DELETE FROM submission_files
                     WHERE submission_id IN (
                        SELECT s.id
                          FROM submissions s
                          JOIN assignments a ON a.id = s.assignment_id
                         WHERE a.course_id = ?
                     )
                    """,
                    (course_id,),
                )
            elif table == "submissions":
                connection.execute(
                    """
                    DELETE FROM submissions
                     WHERE assignment_id IN (
                        SELECT id FROM assignments WHERE course_id = ?
                     )
                    """,
                    (course_id,),
                )
            elif table == "assignment_files":
                connection.execute(
                    "DELETE FROM assignment_files WHERE assignment_id IN"
                    " (SELECT id FROM assignments WHERE course_id = ?)",
                    (course_id,),
                )
            elif table == "assignments":
                connection.execute(
                    "DELETE FROM assignments WHERE course_id = ?", (course_id,)
                )
            elif table in ("curator_groups", "group_members"):
                connection.execute(
                    f"DELETE FROM {table} WHERE group_id IN"
                    " (SELECT id FROM groups WHERE course_id = ?)",
                    (course_id,),
                )
            elif table == "groups":
                connection.execute(
                    "DELETE FROM groups WHERE course_id = ?", (course_id,)
                )
            elif table == "import_errors":
                connection.execute(
                    "DELETE FROM import_errors WHERE batch_id IN"
                    " (SELECT id FROM import_batches WHERE course_id = ?)",
                    (course_id,),
                )
            elif table == "import_batches":
                connection.execute(
                    "DELETE FROM import_batches WHERE course_id = ?", (course_id,)
                )
            elif table == "users":
                connection.execute(
                    "DELETE FROM users WHERE course_id = ?", (course_id,)
                )
            elif table == "courses":
                connection.execute("DELETE FROM courses WHERE id = ?", (course_id,))

    _delete_stored_files(stored_names)
    return preview
