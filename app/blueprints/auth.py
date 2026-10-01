"""Вход, выход и смена учётных данных.

Этап Э-2: FR-01…FR-12, BR-10…BR-12, BR-28…BR-33, AC-01…AC-04,
AC-19…AC-22, AC-28, AC-35.

Три решения, которые стоит знать при чтении кода.

**Пароль никогда не попадает в сеанс.** Сессия — подписанная cookie,
её содержимое читает браузер, поэтому в сеанс кладётся только id одной
строки ``users``, а сам пароль живёт только в момент проверки.

**Вход один шаг, курс не выбирается.** Один человек может быть заведён
на двух курсах отдельными строками (e-mail уникален в пределах курса).
Раньше это требовало экрана ``/select-course``: он убран по решению
заказчика — пароль проверяется по всем строкам с этим e-mail, а
видимость считается по ним всем сразу (``access.identity_rows``).

**Тексты ниже отсутствуют в §9.3.** ``INVALID_CREDENTIALS`` и
``CURRENT_PASSWORD_INVALID`` пришлось написать: §9.3 перечисляет
«ключевые» тексты и не содержит сообщения о неверных учётных данных.
Оба предложены в §9.3 отдельной правкой ТЗ.
"""

from __future__ import annotations

import sqlite3

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)

from app import access, action_log, config, db, passwords

bp = Blueprint("auth", __name__)

#: Неверный e-mail или пароль. Один текст на оба случая: различать их
#: нельзя, иначе форма входа превращается в способ проверить, какие
#: адреса заведены в системе.
INVALID_CREDENTIALS = "Неверный e-mail или пароль."

#: Текущий пароль при самостоятельной смене (FR-11).
CURRENT_PASSWORD_INVALID = "Текущий пароль указан неверно."

#: Флажок подтверждения на форме первого входа (FR-03, «и подтверждение»).
CONFIRMATION_REQUIRED = "Подтвердите смену логина и пароля."


def _safe_next(candidate: str | None) -> str:
    """Оставить только внутренний адрес, иначе — на кабинет.

    Без этой проверки ``?next=`` позволил бы увести пользователя после
    входа на внешний сайт.
    """
    if not candidate:
        return url_for("index")
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return url_for("index")


def _requested_next() -> str:
    return _safe_next(request.values.get("next"))


def _verify_password(user: sqlite3.Row, candidate: str) -> str | None:
    """Сверить пароль. ``None`` — совпал."""
    if not passwords.verify_password(candidate, user["password_hash"]):
        return INVALID_CREDENTIALS
    return None


def _match_any(candidates: list[sqlite3.Row], password: str) -> sqlite3.Row | None:
    """Найти запись человека, чей пароль подходит. Иначе ``None``.

    У человека на двух курсах две строки и могли завести разные пароли
    (файлы реестра собираются по курсам). Вход должен открываться по
    любому из них, а дальше строки всё равно живут под одним логином.
    """
    for row in candidates:
        if passwords.verify_password(password, row["password_hash"]):
            return row
    return None


def _complete_login(user: sqlite3.Row, next_url: str) -> "object":
    """Открыть сеанс, записать вход и уйти на ``next_url``."""
    action_log.log_authentication("auth.login", user)
    access.login_user(user)
    return redirect(next_url)


# --------------------------------------------------------------------------
# Вход и выход
# --------------------------------------------------------------------------


@bp.route("/login", methods=["GET", "POST"])
def login():
    if access.is_authenticated():
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template(
            "auth/login.html",
            email="",
            error=None,
            next_url=_requested_next(),
        )

    email = passwords.normalise_email(request.form.get("email", ""))
    password = request.form.get("password", "")
    next_url = _requested_next()

    error = passwords.validate_email(email)
    chosen: sqlite3.Row | None = None
    if error is None:
        candidates = access.find_active_by_email(email)
        chosen = _match_any(candidates, password)
        if chosen is None:
            error = INVALID_CREDENTIALS

    if error is not None:
        return (
            render_template(
                "auth/login.html",
                email=email,
                error=error,
                next_url=next_url,
            ),
            401,
        )
    return _complete_login(chosen, next_url)


@bp.route("/logout", methods=["POST"])
def logout():
    user = access.current_user()
    if user is not None:
        action_log.log_authentication("auth.logout", user)
    access.logout_user()
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------
# Смена логина и пароля при первом входе (FR-02…FR-04, AC-01…AC-03)
# --------------------------------------------------------------------------


@bp.route("/change-credentials", methods=["GET", "POST"])
def change_credentials():
    user = access.current_user()
    if user is None:
        return redirect(url_for("auth.login"))
    if not user["must_change_password"]:
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template(
            "auth/change_credentials.html",
            mode="credentials",
            user=user,
            error=None,
            form={},
        )

    form = {
        "new_email": passwords.normalise_email(
            request.form.get("new_email", "")
        ),
        "new_password": request.form.get("new_password", ""),
        "new_password_confirm": request.form.get("new_password_confirm", ""),
    }
    acknowledged = request.form.get("confirm_change") == "on"

    error = passwords.validate_email(form["new_email"])
    # Строки человека на других курсах меняются разом: логин у него один.
    siblings = access.identity_rows(user)
    sibling_ids = [int(row["id"]) for row in siblings]
    if error is None:
        error = passwords.validate_password_confirmation(
            form["new_password"], form["new_password_confirm"], user["role"]
        )
    if error is None and not acknowledged:
        error = CONFIRMATION_REQUIRED
    if error is None:
        for row in siblings:
            if access.email_taken(
                form["new_email"],
                course_id=row["course_id"],
                exclude_user_ids=sibling_ids,
            ):
                error = config.MESSAGES["email_taken"]
                break

    if error is not None:
        return (
            render_template(
                "auth/change_credentials.html",
                mode="credentials",
                user=user,
                error=error,
                form=form,
            ),
            400,
        )

    # Хеш считаем до открытия транзакции: он дорогой, а база под ним
    # была бы заблокирована.
    encrypted, hashed = passwords.build_credentials(
        form["new_password"], user["role"]
    )
    marks = ",".join("?" for _ in sibling_ids)
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE users
               SET email = ?, password_enc = ?, password_hash = ?,
                   must_change_password = 0
             WHERE id IN ({0})
            """.format(marks),
            [form["new_email"], encrypted, hashed, *sibling_ids],
        )
        action_log.log_action(
            "auth.credentials_changed",
            actor_id=user["id"],
            actor_role=user["role"],
            entity_type="user",
            entity_id=user["id"],
            course_id=user["course_id"],
            connection=connection,
        )
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Самостоятельная смена пароля (FR-11, FR-12, AC-21)
# --------------------------------------------------------------------------


@bp.route("/change-password", methods=["GET", "POST"])
def change_password():
    user = access.current_user()
    if user is None:
        return redirect(url_for("auth.login"))
    if user["must_change_password"]:
        return redirect(url_for("auth.change_credentials"))

    if request.method == "GET":
        return render_template(
            "auth/change_credentials.html",
            mode="password",
            user=user,
            error=None,
            form={},
        )

    current_password = request.form.get("current_password", "")
    form = {
        "new_password": request.form.get("new_password", ""),
        "new_password_confirm": request.form.get("new_password_confirm", ""),
    }

    error = _verify_password(user, current_password)
    if error is not None:
        error = CURRENT_PASSWORD_INVALID
    else:
        error = passwords.validate_password_confirmation(
            form["new_password"], form["new_password_confirm"], user["role"]
        )

    if error is not None:
        return (
            render_template(
                "auth/change_credentials.html",
                mode="password",
                user=user,
                error=error,
                form=form,
            ),
            400,
        )

    encrypted, hashed = passwords.build_credentials(
        form["new_password"], user["role"]
    )
    # Пароль у человека один, поэтому меняются все его строки.
    sibling_ids = access.identity_ids(user)
    marks = ",".join("?" for _ in sibling_ids)
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE users
               SET password_enc = ?, password_hash = ?
             WHERE id IN ({0})
            """.format(marks),
            [encrypted, hashed, *sibling_ids],
        )
        action_log.log_action(
            "pwd.changed_by_user",
            actor_id=user["id"],
            actor_role=user["role"],
            entity_type="user",
            entity_id=user["id"],
            course_id=user["course_id"],
            connection=connection,
        )
    return redirect(url_for("index"))
