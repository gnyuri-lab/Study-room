"""Участники группы и кураторы (FR-19…FR-21, FR-09, FR-10).

Модуль закрывает три разных по смыслу вещи, которые в интерфейсе
выглядят как одна страница «Участники»:

* ручное добавление учётной записи и включение её в группу
  (``group_members`` для студента, ``curator_groups`` для куратора);
* правка и исключение участника;
* прикрепление и открепление куратора (один куратор — несколько групп,
  FR-21).

**Исключение участника не удаляет строку ``users``** (допущение В-01):
учётная запись получает ``is_active = 0``, теряет доступ к курсу, а
её прошлые работы и статусы сохраняются. Удалять строку нельзя —
на неё ссылаются сдачи, комментарии и загруженные файлы.

Следствие, о котором надо знать: e-mail исключённого участника остаётся
занятым в курсе, потому что ограничение уникальности действует на все
строки таблицы. Повторное добавление с тем же адресом будет отклонено с
текстом «Этот логин уже используется в системе.» — ровно тот же, что
и при повторном импорте (FR-30, AC-27).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import access, action_log, config, db, passwords
from app.services import ServiceError

#: Куда попадает участник по роли. Это единственное место, где роль
#: превращается в таблицу членства; импорт (Э-4) пользуется тем же.
MEMBERSHIP_TABLE_BY_ROLE = {
    "student": "group_members",
    "curator": "curator_groups",
}


def _connection() -> sqlite3.Connection:
    return db.get_db()


# --------------------------------------------------------------------------
# Чтение
# --------------------------------------------------------------------------


def list_participants(group_id: int) -> list[dict[str, Any]]:
    """Студенты и кураторы группы.

    Сортировка по ФИО — устойчивая к регистру и по e-mail, чтобы две
    учётные записи с одинаковым ФИО не менялись местами при загрузке.
    """
    rows = _connection().execute(
        """
        SELECT u.id, u.full_name, u.email, u.role, u.is_active,
               u.must_change_password
          FROM group_members gm
          JOIN users u ON u.id = gm.user_id
         WHERE gm.group_id = ?
        UNION ALL
        SELECT u.id, u.full_name, u.email, u.role, u.is_active,
               u.must_change_password
          FROM curator_groups cg
          JOIN users u ON u.id = cg.user_id
         WHERE cg.group_id = ?
         ORDER BY 2 COLLATE NOCASE, 3
        """,
        (group_id, group_id),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_member(user_id: int) -> sqlite3.Row | None:
    return _connection().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def is_participant(group_id: int, user_id: int) -> bool:
    """Состоит ли учётная запись в этой группе (любой ролью)."""
    connection = _connection()
    for table in MEMBERSHIP_TABLE_BY_ROLE.values():
        if (
            connection.execute(
                f"SELECT 1 FROM {table} WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            is not None
        ):
            return True
    return False


def list_course_curators(course_id: int) -> list[dict[str, Any]]:
    """Кураторы курса — источник списка «прикрепить к группе» (FR-21)."""
    rows = _connection().execute(
        """
        SELECT id, full_name, email
          FROM users
         WHERE course_id = ? AND role = 'curator' AND is_active = 1
         ORDER BY full_name COLLATE NOCASE, id
        """,
        (course_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Квалификация полей формы
# --------------------------------------------------------------------------


def validate_full_name(full_name: str) -> str:
    candidate = (full_name or "").strip()
    if not candidate:
        raise ServiceError(config.MESSAGES["full_name_required"])
    if len(candidate) > config.FULL_NAME_MAX_LENGTH:
        raise ServiceError(config.MESSAGES["full_name_too_long"])
    return candidate


def validate_role(role: str) -> str:
    if role not in MEMBERSHIP_TABLE_BY_ROLE:
        raise ServiceError(config.MESSAGES["role_invalid"])
    return role


def validate_email(email: str, *, course_id: int, exclude_user_id: int | None = None) -> str:
    """Логин участника: формат §7.3 и уникальность в пределах курса."""
    candidate = passwords.normalise_email(email)
    error = passwords.validate_email(candidate)
    if error is not None:
        raise ServiceError(error)
    excluded = () if exclude_user_id is None else (exclude_user_id,)
    if access.email_taken(candidate, course_id=course_id, exclude_user_ids=excluded):
        raise ServiceError(config.MESSAGES["email_taken"])
    return candidate


def validate_new_password(password: str, role: str) -> str:
    error = passwords.validate_password(password, role)
    if error is not None:
        raise ServiceError(error)
    return password


# --------------------------------------------------------------------------
# Добавление и правка
# --------------------------------------------------------------------------


def create_member(
    *,
    course_id: int,
    group_id: int,
    full_name: str,
    role: str,
    email: str,
    password: str,
    actor: sqlite3.Row,
    added_via: str = "manual",
) -> int:
    """Создать участника и включить его в группу (FR-19, AC-37).

    Порядок проверок совпадает с порядком полей формы: ФИО, роль,
    e-mail, пароль. Пароль хешируется **до** открытия транзакции —
    pbkdf2 дорог, а под ней была бы заблокирована база.

    ``added_via`` попадает в журнал и отвечает на вопрос «откуда взялась
    учётная запись»: ``manual`` для формы участников (AC-37) и
    ``import`` для строки файла (FR-28). Импорт Э-4 переиспользует эту
    функцию, а не пишет свой ``INSERT``, — иначе проверки полей и
    записи ``group.member_added`` разошлись бы с ручным добавлением.
    """
    clean_name = validate_full_name(full_name)
    clean_role = validate_role(role)
    clean_email = validate_email(email, course_id=course_id)
    validate_new_password(password, clean_role)
    encrypted, hashed = passwords.build_credentials(password, clean_role)

    table = MEMBERSHIP_TABLE_BY_ROLE[clean_role]
    with db.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (
                course_id, email, full_name, role, password_enc, password_hash,
                must_change_password, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)
            """,
            (
                course_id,
                clean_email,
                clean_name,
                clean_role,
                encrypted,
                hashed,
                action_log.utc_now_iso(),
            ),
        )
        user_id = cursor.lastrowid
        connection.execute(
            f"INSERT INTO {table} (group_id, user_id) VALUES (?, ?)",
            (group_id, user_id),
        )
        # Куратор, добавленный через форму участников, физически
        # прикреплён к группе, поэтому пишется curator.attached —
        # именно это событие и проверяет AC-37.
        action = "group.member_added" if clean_role == "student" else "curator.attached"
        action_log.log_action(
            action,
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="user",
            entity_id=user_id,
            course_id=course_id,
            group_id=group_id,
            details={"role": clean_role, "added_via": added_via},
            connection=connection,
        )
    return user_id


def update_member(
    *, user_id: int, full_name: str, email: str, actor: sqlite3.Row
) -> None:
    """Сохранить новые ФИО и e-mail (FR-20).

    Почему так мало полей: пароль здесь не меняется, у него отдельная
    операция с записью в журнал (``pwd.reissued_by_admin``, AC-20), а
    роль определяет таблицу членства, и её смена означала бы перевод
    участника между таблицами — это уже не «изменение записи».
    """
    member = fetch_member(user_id)
    if member is None:
        access.abort_not_found()
    clean_name = validate_full_name(full_name)
    clean_email = validate_email(
        email, course_id=member["course_id"], exclude_user_id=user_id
    )
    changed = {}
    if clean_name != member["full_name"]:
        changed["full_name"] = clean_name
    if clean_email != member["email"]:
        changed["email"] = clean_email
    if not changed:
        return
    with db.transaction() as connection:
        connection.execute(
            "UPDATE users SET full_name = ?, email = ? WHERE id = ?",
            (clean_name, clean_email, user_id),
        )
        action_log.log_action(
            "group.member_updated",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="user",
            entity_id=user_id,
            course_id=member["course_id"],
            details={"fields": sorted(changed)},
            connection=connection,
        )


# --------------------------------------------------------------------------
# Исключение участника (FR-20, В-01)
# --------------------------------------------------------------------------


def remove_member(*, group_id: int, user_id: int, actor: sqlite3.Row) -> None:
    """Исключить участника из группы.

    Строка членства удаляется всегда, а учётная запись деактивируется
    только если других групп в этом курсе не осталось: иначе участник
    потерял бы доступ к группе, в которой ещё состоит (В-01).
    """
    if not is_participant(group_id, user_id):
        access.abort_not_found()
    member = fetch_member(user_id)
    if member is None:
        access.abort_not_found()
    with db.transaction() as connection:
        for table in MEMBERSHIP_TABLE_BY_ROLE.values():
            connection.execute(
                f"DELETE FROM {table} WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
        remaining = 0
        for table in MEMBERSHIP_TABLE_BY_ROLE.values():
            remaining += connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        if remaining == 0:
            connection.execute(
                "UPDATE users SET is_active = 0 WHERE id = ?", (user_id,)
            )
        action_log.log_action(
            "group.member_removed",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="user",
            entity_id=user_id,
            course_id=member["course_id"],
            group_id=group_id,
            details={"role": member["role"], "deactivated": remaining == 0},
            connection=connection,
        )


# --------------------------------------------------------------------------
# Кураторы (FR-21, AC-37)
# --------------------------------------------------------------------------


def attach_curator(*, group_id: int, user_id: int, actor: sqlite3.Row) -> None:
    """Прикрепить куратора к группе. Повторное — отказ с объяснением."""
    curator = fetch_member(user_id)
    if curator is None or curator["role"] != "curator":
        access.abort_not_found()
    if not curator["is_active"]:
        raise ServiceError(config.MESSAGES["curator_inactive"])
    if is_participant(group_id, user_id):
        raise ServiceError(config.MESSAGES["curator_already_attached"])
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO curator_groups (group_id, user_id) VALUES (?, ?)",
            (group_id, user_id),
        )
        action_log.log_action(
            "curator.attached",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="user",
            entity_id=user_id,
            course_id=curator["course_id"],
            group_id=group_id,
            connection=connection,
        )


def detach_curator(*, group_id: int, user_id: int, actor: sqlite3.Row) -> None:
    """Открепить куратора от группы (FR-21, AC-37)."""
    curator = fetch_member(user_id)
    if curator is None or curator["role"] != "curator":
        access.abort_not_found()
    if not is_participant(group_id, user_id):
        access.abort_not_found()
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM curator_groups WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        action_log.log_action(
            "curator.detached",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="user",
            entity_id=user_id,
            course_id=curator["course_id"],
            group_id=group_id,
            connection=connection,
        )


# --------------------------------------------------------------------------
# Пароли администратора (FR-09, FR-10, AC-19, AC-20)
# --------------------------------------------------------------------------


def reveal_member_password(*, group_id: int, user_id: int, actor: sqlite3.Row) -> str:
    """Показать текущий пароль участника и записать просмотр (FR-09).

    Право на вызов проверяет слой доступа: маршрут доступен только
    администратору, поэтому пароль не может уйти куратору или студенту
    (BR-30, AC-22). Сам пароль в журнал не попадает — туда пишется
    только сам факт просмотра.
    """
    member = fetch_member(user_id)
    if member is None or not is_participant(group_id, user_id):
        access.abort_not_found()
    clear = passwords.reveal_password(member)
    with db.transaction() as connection:
        action_log.log_action(
            "pwd.viewed_by_admin",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="user",
            entity_id=user_id,
            course_id=member["course_id"],
            group_id=group_id,
            connection=connection,
        )
    return clear


def reissue_member_password(
    *, group_id: int, user_id: int, actor: sqlite3.Row
) -> str:
    """Назначить участнику новый пароль и вернуть его открытым текстом.

    Признак ``must_change_password`` снимается: по AC-20 пользователь
    входит по новому паролю сразу.
    """
    member = fetch_member(user_id)
    if member is None or not is_participant(group_id, user_id):
        access.abort_not_found()
    with db.transaction() as connection:
        new_password = passwords.reissue_password(user_id, connection=connection)
        action_log.log_action(
            "pwd.reissued_by_admin",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="user",
            entity_id=user_id,
            course_id=member["course_id"],
            group_id=group_id,
            connection=connection,
        )
    return new_password
