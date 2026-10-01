"""Группы курса: создание, правка, архивирование и удаление
(FR-18, FR-22, FR-23, BR-25…BR-27).

Три исхода удаления, и порядок проверок задан правилами буквы:

1. есть хотя бы один участник — операция **заблокирована** (BR-25,
   BR-27). Это правило сильнее остальных: пустая по сдачам группа с
   участниками всё равно не удаляется.
2. участников нет, но есть сдачи — **мягкое удаление в архив**
   (FR-23): работы студентов не должны пропасть.
3. участников и сдач нет — **физическое удаление** (BR-26).

**Исход 2 недостижим при текущей схеме — это расхождение ТЗ, а не
ошибка расчёта.** Архивирование требует нуля участников, а сдачи
считаются по текущим строкам ``group_members`` (§7.6: у таблицы
членства ровно два поля, истории в ней нет, а ``submissions`` не
содержит ``group_id``). Состояния «ноль участников и ненулевые сдачи»
не бывает: BR-25 закрывает группу, пока в ней есть участник.

Что это значит на практике: сдачи не могут быть уничтожены удалением
группы. Пока в группе есть студент с работами, BR-25 запрещает удаление;
после исключения студента (FR-20) его работы остаются в базе, потому
что принадлежат студенту и заданию, а не группе, и группа удаляется
физически без потери данных. Ветка архива оставлена в коде намеренно:
если ТЗ получит способ связывать сдачи с группой, править придётся
только ``submission_count``, а не всю функцию. Вопрос вынесен в отчёт
этапа (README.md, «Отчёт по этапу Э-3») и ждёт решения заказчика.

В справочнике §7.12 нет кода ``group.deleted``: для физического
удаления используется ``group.updated`` с признаком ``deleted`` в
``details_json``. Своего кода ТЗ не заводит, чтобы не менять
``CHECK`` в уже применённой миграции; расхождение вынесено в отчёт
этапа (README.md, «Отчёт по этапу Э-3»).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import access, action_log, config, db
from app.services import ServiceError

#: Что группа считается непустой: и студенты, и кураторы (FR-22, BR-25).
MEMBERSHIP_TABLES = ("group_members", "curator_groups")


def _connection() -> sqlite3.Connection:
    return db.get_db()


# --------------------------------------------------------------------------
# Чтение
# --------------------------------------------------------------------------


def list_groups(course_id: int, *, include_archived: bool = True) -> list[dict[str, Any]]:
    """Группы курса вместе с числом участников.

    ``include_archived=False`` даёт активные группы — тот список, из
    которого группа выбирается для работы (FR-23: архивная группа
    исключена из активных списков).
    """
    condition = "" if include_archived else "AND g.archived_at IS NULL"
    rows = _connection().execute(
        f"""
        SELECT g.id, g.name, g.archived_at, g.created_at,
               (SELECT COUNT(*) FROM group_members gm
                 WHERE gm.group_id = g.id) AS students_count,
               (SELECT COUNT(*) FROM curator_groups cg
                 WHERE cg.group_id = g.id) AS curators_count
          FROM groups g
         WHERE g.course_id = ? {condition}
         ORDER BY g.name, g.id
        """,
        (course_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["participants_count"] = item["students_count"] + item["curators_count"]
        result.append(item)
    return result


def fetch_group(group_id: int) -> sqlite3.Row | None:
    return _connection().execute(
        """
        SELECT g.*, c.title AS course_title
          FROM groups g
          JOIN courses c ON c.id = g.course_id
         WHERE g.id = ?
        """,
        (group_id,),
    ).fetchone()


def participant_count(group_id: int) -> int:
    """Все участники группы: студенты и кураторы вместе (AC-30)."""
    connection = _connection()
    total = 0
    for table in MEMBERSHIP_TABLES:
        total += connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE group_id = ?", (group_id,)
        ).fetchone()[0]
    return total


def submission_count(group_id: int) -> int:
    """Сколько работ сдано участниками группы (FR-23, BR-26).

    Задания принадлежат курсу, а не группе, поэтому «сдачи по группе»
    — это сдачи студентов, состоящих в ней.
    """
    return _connection().execute(
        """
        SELECT COUNT(*)
          FROM submissions s
         WHERE s.student_id IN (
            SELECT user_id FROM group_members WHERE group_id = ?
         )
        """,
        (group_id,),
    ).fetchone()[0]


# --------------------------------------------------------------------------
# Квалификация
# --------------------------------------------------------------------------


def validate_name(name: str, *, course_id: int, exclude_group_id: int | None = None) -> str:
    """Название группы: не пусто, ≤ 100 символов, уникально в курсе (FR-18)."""
    candidate = (name or "").strip()
    if not candidate:
        raise ServiceError(config.MESSAGES["group_name_required"])
    if len(candidate) > config.GROUP_NAME_MAX_LENGTH:
        raise ServiceError(config.MESSAGES["group_name_too_long"])
    condition = "SELECT 1 FROM groups WHERE course_id = ? AND name = ?"
    params: list[Any] = [course_id, candidate]
    if exclude_group_id is not None:
        condition += " AND id != ?"
        params.append(exclude_group_id)
    if _connection().execute(condition + " LIMIT 1", params).fetchone() is not None:
        raise ServiceError(config.MESSAGES["group_name_taken"])
    return candidate


# --------------------------------------------------------------------------
# Создание и правка
# --------------------------------------------------------------------------


def find_by_name(course_id: int, name: str) -> sqlite3.Row | None:
    """Активная группа курса с таким именем — или ``None``.

    Импорт (Э-4) переиспользует существующую группу вместо попытки
    создать вторую с тем же именем: ``validate_name`` отверг бы такую
    попытку с «Группа с таким названием уже есть в курсе», хотя
    администратор ничего не делал неправильно — он просто принёс файл
    с уже существующей группой. Архивные группы не подходят: в них
    нельзя принимать участников (FR-23), и такое имя отсекается при
    квалификации строки.
    """
    return _connection().execute(
        "SELECT * FROM groups"
        " WHERE course_id = ? AND name = ? AND archived_at IS NULL",
        (course_id, (name or "").strip()),
    ).fetchone()


def create_group(
    *, course_id: int, name: str, actor: sqlite3.Row
) -> int:
    """Создать группу курса и записать ``group.created`` (FR-18)."""
    course = _connection().execute(
        "SELECT 1 FROM courses WHERE id = ?", (course_id,)
    ).fetchone()
    if course is None:
        access.abort_not_found()
    clean_name = validate_name(name, course_id=course_id)
    with db.transaction() as connection:
        cursor = connection.execute(
            "INSERT INTO groups (course_id, name, archived_at, created_at)"
            " VALUES (?, ?, NULL, ?)",
            (course_id, clean_name, action_log.utc_now_iso()),
        )
        group_id = cursor.lastrowid
        action_log.log_action(
            "group.created",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="group",
            entity_id=group_id,
            course_id=course_id,
            group_id=group_id,
            connection=connection,
        )
    return group_id


def update_group(*, group_id: int, name: str, actor: sqlite3.Row) -> None:
    """Переименовать группу (FR-20)."""
    group = fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    clean_name = validate_name(
        name, course_id=group["course_id"], exclude_group_id=group_id
    )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE groups SET name = ? WHERE id = ?", (clean_name, group_id)
        )
        action_log.log_action(
            "group.updated",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="group",
            entity_id=group_id,
            course_id=group["course_id"],
            group_id=group_id,
            details={"name": clean_name},
            connection=connection,
        )


# --------------------------------------------------------------------------
# Удаление или архивирование (FR-22, FR-23, BR-25…BR-27)
# --------------------------------------------------------------------------


def delete_or_archive_group(
    *, group_id: int, actor: sqlite3.Row
) -> str:
    """Удалить или архивировать группу. Возвращает ``deleted``/``archived``.

    Блокировка по BR-25 проверяется **до** всего остального: даже группа
    без сдач, но с участниками, удалить нельзя. Текст отказа — из §9.3
    с фактическим числом участников (AC-30).
    """
    group = fetch_group(group_id)
    if group is None:
        access.abort_not_found()

    participants = participant_count(group_id)
    if participants:
        raise ServiceError(
            config.MESSAGES["group_not_empty"].format(count=participants)
        )

    submissions = submission_count(group_id)
    archived = submissions > 0
    with db.transaction() as connection:
        if archived:
            connection.execute(
                "UPDATE groups SET archived_at = ? WHERE id = ?",
                (action_log.utc_now_iso(), group_id),
            )
            action = "group.archived"
        else:
            for table in MEMBERSHIP_TABLES:
                connection.execute(
                    f"DELETE FROM {table} WHERE group_id = ?", (group_id,)
                )
            connection.execute("DELETE FROM groups WHERE id = ?", (group_id,))
            action = "group.updated"
        action_log.log_action(
            action,
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="group",
            entity_id=group_id,
            course_id=group["course_id"],
            group_id=group_id,
            details={"name": group["name"], "deleted": not archived},
            connection=connection,
        )
    return "archived" if archived else "deleted"
