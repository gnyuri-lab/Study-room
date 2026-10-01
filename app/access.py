"""Доступ: сессия, роли и область видимости (FR-07, FR-08, BR-28…BR-33).

Скрытие кнопки в шаблоне не считается разрешением: каждая проверка
здесь выполняется на сервере при каждом запросе (FR-07).

Два разных отказа, и это не одно и то же:

* **403** — роль не допущена к действию в принципе (BR-33).
  Текст из §9.3: «У вас нет прав на это действие.»
* **404** — объект чужой или его не существует. Ответы одинаковые,
  чтобы по коду нельзя было узнать, существует ли объект (FR-08).
  Текст: «Объект не найден или был удалён.»

Поэтому ``can_view_course`` возвращает ``bool``, а ``require_course``
сбрасывает обе ситуации в один 404.

**Одна учётная запись на человека.** E-mail уникален в пределах курса
(BR-06), поэтому один человек на двух курсах — это две строки ``users``.
Вход один и пароль один: после входа сеанс привязывается к одной строке,
но видимость считается по всем строкам человека (``identity_ids``).
Строки с разной ролью не склеиваются — у студента и куратора разные
права.
"""

from __future__ import annotations

import functools
import sqlite3
from typing import Any, Callable, Iterable

from flask import abort, g, session

SESSION_USER_KEY = "user_id"

ADMIN_ROLE = "admin"
CURATOR_ROLE = "curator"
STUDENT_ROLE = "student"


# --------------------------------------------------------------------------
# Загрузка текущего пользователя
# --------------------------------------------------------------------------


def _connection() -> sqlite3.Connection:
    from app import db

    return db.get_db()


def fetch_user(user_id: int) -> sqlite3.Row | None:
    """Вернуть активную учётную запись по ``id`` либо ``None``."""
    return _connection().execute(
        "SELECT * FROM users WHERE id = ? AND is_active = 1",
        (user_id,),
    ).fetchone()


def find_active_by_email(email: str) -> list[sqlite3.Row]:
    """Вернуть активные записи с таким e-mail, без учёта регистра (FR-01).

    Больше одной строки означает, что один человек заведён на двух
    курсах (BR-06). Вход не требует выбора курса: пароль проверяется по
    каждой строке, а видимость потом считается по всем сразу.
    """
    return _connection().execute(
        "SELECT * FROM users WHERE email = ? AND is_active = 1"
        " ORDER BY course_id IS NULL DESC, course_id, id",
        (email.strip(),),
    ).fetchall()


def identity_rows(user: sqlite3.Row | None = None) -> list[sqlite3.Row]:
    """Все учётные записи одного человека (тот же e-mail и роль).

    Человек заводится отдельной строкой ``users`` на каждый курс (его
    e-mail уникален только в пределах курса), но работает в системе как
    единое целое: после входа ему доступны все курсы, где он есть.

    Склеиваются строки с одинаковым e-mail **и одинаковой ролью**: права
    у студента и куратора разные, и подмешивать их нельзя.
    """
    if user is None:
        user = current_user()
    if user is None:
        return []
    return _connection().execute(
        "SELECT * FROM users"
        " WHERE email = ? AND role = ? AND is_active = 1"
        " ORDER BY course_id IS NULL DESC, course_id, id",
        (user["email"], user["role"]),
    ).fetchall()


def identity_ids(user: sqlite3.Row | None = None) -> list[int]:
    """Id всех учётных записей одного человека. См. :func:`identity_rows`."""
    return [int(row["id"]) for row in identity_rows(user)]


def email_taken(
    email: str,
    *,
    course_id: int | None,
    exclude_user_ids: Iterable[int] | None = None,
) -> bool:
    """Занят ли логин.

    У администратора ``course_id`` равен NULL, поэтому его логин должен
    быть уникален во всём приложении, а у остальных — в пределах курса
    (Х-03, §7.3).

    ``exclude_user_ids`` — записи, которые не мешают (сам человек и все
    его строки на других курсах): один человек заводится на каждый курс
    отдельной строкой, и при смене логина они меняются разом.
    """
    conditions = ["email = ?", "course_id IS ?"]
    params: list[Any] = [email.strip(), course_id]
    excluded = [int(item) for item in (exclude_user_ids or ())]
    if excluded:
        marks = ",".join("?" for _ in excluded)
        conditions.append("users.id NOT IN ({0})".format(marks))
        params.extend(excluded)
    query = "SELECT 1 FROM users WHERE {} LIMIT 1".format(" AND ".join(conditions))
    return _connection().execute(query, params).fetchone() is not None


def current_user() -> sqlite3.Row | None:
    """Текущий пользователь из сессии либо ``None``.

    Учётная запись перечитывается из базы на каждый запрос: если её
    отключили (допущение В-01), сессия перестаёт давать доступ сразу,
    без ожидания выхода.
    """
    if "current_user" in g:
        return g.current_user
    user_id = session.get(SESSION_USER_KEY)
    user = None
    if user_id is not None:
        try:
            user = fetch_user(int(user_id))
        except (TypeError, ValueError):
            user = None
    g.current_user = user
    return user


def is_authenticated() -> bool:
    return current_user() is not None


def role() -> str | None:
    user = current_user()
    return user["role"] if user is not None else None


def has_role(*roles: str) -> bool:
    current = role()
    return current is not None and current in roles


def is_admin() -> bool:
    return has_role(ADMIN_ROLE)


def must_change_password() -> bool:
    """Требуется обязательная смена логина и пароля (FR-02)."""
    user = current_user()
    return user is not None and bool(user["must_change_password"])


def login_user(user: sqlite3.Row) -> None:
    """Открыть сеанс. ``session.clear()`` убирает возможный ``next``."""
    session.clear()
    session[SESSION_USER_KEY] = int(user["id"])
    session.permanent = True
    g.pop("current_user", None)


def logout_user() -> None:
    """Завершить сеанс (FR-06)."""
    session.clear()
    g.pop("current_user", None)


# --------------------------------------------------------------------------
# Область видимости (BR-28, BR-29)
# --------------------------------------------------------------------------


def visible_group_ids() -> set[int]:
    """Группы, доступные текущему пользователю.

    Архивные группы скрыты от куратора и студента: по FR-23 они
    исключены из активных списков, а работы в них сохранены. Администратор
    архив видит — он им и управляет.

    Учитываются все учётные записи человека (``identity_ids``): студент
    видит группы всех курсов, где он заведён, а не только того курса,
    чья строка попала в сеанс.
    """
    user = current_user()
    if user is None:
        return set()
    if user["role"] == ADMIN_ROLE:
        rows = _connection().execute("SELECT id FROM groups").fetchall()
        return {row["id"] for row in rows}

    ids = identity_ids(user)
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    if user["role"] == CURATOR_ROLE:
        rows = _connection().execute(
            """
            SELECT g.id
              FROM groups g
              JOIN curator_groups cg ON cg.group_id = g.id
             WHERE cg.user_id IN ({0}) AND g.archived_at IS NULL
            """.format(marks),
            ids,
        ).fetchall()
    else:
        rows = _connection().execute(
            """
            SELECT g.id
              FROM groups g
              JOIN group_members gm ON gm.group_id = g.id
             WHERE gm.user_id IN ({0}) AND g.archived_at IS NULL
            """.format(marks),
            ids,
        ).fetchall()
    return {row["id"] for row in rows}


def visible_course_ids() -> set[int]:
    """Курсы, доступные текущему пользователю."""
    user = current_user()
    if user is None:
        return set()
    if user["role"] == ADMIN_ROLE:
        rows = _connection().execute("SELECT id FROM courses").fetchall()
        return {row["id"] for row in rows}

    ids = identity_ids(user)
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    if user["role"] == CURATOR_ROLE:
        rows = _connection().execute(
            """
            SELECT DISTINCT g.course_id AS id
              FROM groups g
              JOIN curator_groups cg ON cg.group_id = g.id
             WHERE cg.user_id IN ({0}) AND g.archived_at IS NULL
            """.format(marks),
            ids,
        ).fetchall()
    else:
        rows = _connection().execute(
            "SELECT DISTINCT course_id AS id FROM users"
            " WHERE id IN ({0}) AND course_id IS NOT NULL".format(marks),
            ids,
        ).fetchall()
    return {row["id"] for row in rows if row["id"] is not None}


def can_view_course(course_id: int | None) -> bool:
    if course_id is None:
        return False
    return int(course_id) in visible_course_ids()


def can_view_group(group_id: int | None) -> bool:
    if group_id is None:
        return False
    return int(group_id) in visible_group_ids()


def require_course(course_id: int | None) -> None:
    """Отказать в доступе к чужому или несуществующему курсу (FR-08)."""
    if not can_view_course(course_id):
        abort_not_found()


def require_group(group_id: int | None) -> None:
    if not can_view_group(group_id):
        abort_not_found()


# --------------------------------------------------------------------------
# Отказы
# --------------------------------------------------------------------------


def abort_forbidden() -> None:
    """Отказ по роли: действие недоступно этой роли в принципе (BR-33)."""
    abort(403)


def abort_not_found() -> None:
    """Объект не найден или не виден: ответы неразличимы (FR-08)."""
    abort(404)


# --------------------------------------------------------------------------
# Декораторы
# --------------------------------------------------------------------------


def login_required(view: Callable) -> Callable:
    """Требовать входа; иначе — на экран входа (BR-31)."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            from flask import redirect, request, url_for

            return redirect(
                url_for("auth.login", next=request.full_path)
            )
        return view(*args, **kwargs)

    return wrapper


def admin_required(view: Callable) -> Callable:
    """Требовать роль администратора, иначе 403 (BR-33)."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            from flask import redirect, request, url_for

            return redirect(url_for("auth.login", next=request.full_path))
        if not is_admin():
            abort_forbidden()
        return view(*args, **kwargs)

    return wrapper


def staff_required(view: Callable) -> Callable:
    """Допустить администратора и куратора, иначе 403.

    Куратор только смотрит: группы, участников и задания своих курсов.
    Любое изменение остаётся за ``admin_required`` — кнопки правки в
    шаблонах для него не рисуются, но маршрут закрыт и на сервере.
    """

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            from flask import redirect, request, url_for

            return redirect(url_for("auth.login", next=request.full_path))
        if not has_role(ADMIN_ROLE, CURATOR_ROLE):
            abort_forbidden()
        return view(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------
# Выдача данных учётных записей (BR-30, FR-09)
# --------------------------------------------------------------------------

#: Поля, которые нельзя отдавать куратору и студенту ни при каких условиях.
SECRET_FIELDS = ("email", "password")


def serialize_user(user: sqlite3.Row, viewer_role: str | None) -> dict[str, Any]:
    """Собрать данные учётной записи для ответа.

    Администратор получает e-mail и текущий пароль (FR-09). Куратор и
    студент не получают этих полей вообще — не замаскированными, а
    отсутствующими в словаре (BR-30, AC-22).
    """
    payload: dict[str, Any] = {
        "id": user["id"],
        "full_name": user["full_name"],
        "role": user["role"],
        "course_id": user["course_id"],
        "must_change_password": bool(user["must_change_password"]),
        "is_active": bool(user["is_active"]),
    }
    if viewer_role == ADMIN_ROLE:
        payload["email"] = user["email"]
        from app import passwords

        payload["password"] = passwords.reveal_password(user)
    return payload


def owns(user: sqlite3.Row) -> bool:
    """Является ли текущий пользователь этой учётной записью."""
    current = current_user()
    return current is not None and current["id"] == user["id"]


def self_or_admin(user: sqlite3.Row) -> bool:
    """Текущий пользователь — сам владелец записи или администратор."""
    return owns(user) or is_admin()


def require_self_or_admin(user: sqlite3.Row) -> None:
    if not self_or_admin(user):
        abort_not_found()
