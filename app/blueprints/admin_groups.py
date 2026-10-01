"""Группы курса, участники и кураторы (FR-18…FR-23, BR-25…BR-27).

Чтение открыто администратору и куратору (``access.staff_required``):
куратор смотрит группы и участники своих курсов, ничего не меняя.
Любое изменение остаётся за ``access.admin_required`` (BR-33), а курс и
группу каждый маршрут проверяет через ``access.require_course`` /
``require_group`` (FR-08). Ответы 403 и 404 остаются разными: 403 —
роль не допущена, 404 — объект чужой или удалён.

Один шаблон ``group_members.html`` обслуживает и список участников, и
формы добавления и правки: отдельных страниц в карте файлов §13.1 для
них нет, а участников на экране всегда видно вместе с формой — так
видно, кого правим (checklist §4). Формы и кнопки правки в шаблоне
скрыты, если текущий пользователь не администратор: скрытие кнопки не
разрешение, маршрут всё равно закрыт на сервере.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app import access, config
from app.services import (
    ServiceError,
    groups as groups_service,
    members as members_service,
)

bp = Blueprint("admin_groups", __name__, url_prefix="/admin")


def _actor():
    return access.current_user()


def _render_members(group, *, error=None, form=None, revealed=None, new_password=None):
    """Общий контекст страницы «Участники».

    ``revealed`` и ``new_password`` живут только в ответе на POST и не
    попадают в сеанс: пароль нельзя класть в подписанную cookie и
    нельзя оставлять висеть в сессии после перезагрузки (Э-2).
    """
    return render_template(
        "admin/group_members.html",
        group=group,
        participants=members_service.list_participants(group["id"]),
        course_curators=members_service.list_course_curators(group["course_id"]),
        attached_curator_ids=_attached_curator_ids(group["id"]),
        error=error,
        form=form if form is not None else {},
        revealed=revealed or {},
        new_password=new_password,
    )


def _attached_curator_ids(group_id: int) -> set[int]:
    from app import db

    rows = db.get_db().execute(
        "SELECT user_id FROM curator_groups WHERE group_id = ?", (group_id,)
    ).fetchall()
    return {row["user_id"] for row in rows}


# --------------------------------------------------------------------------
# Группы курса (FR-18, FR-20, FR-22, FR-23)
# --------------------------------------------------------------------------


@bp.get("/courses/<int:course_id>/groups")
@access.staff_required
def groups_list(course_id: int):
    from app import db

    access.require_course(course_id)
    course = db.get_db().execute(
        "SELECT * FROM courses WHERE id = ?", (course_id,)
    ).fetchone()
    if course is None:
        access.abort_not_found()
    return render_template(
        "admin/groups_list.html",
        course=course,
        groups=groups_service.list_groups(course_id),
        error=None,
        form={"name": ""},
    )


@bp.route("/courses/<int:course_id>/groups/new", methods=["GET", "POST"])
@access.admin_required
def group_create(course_id: int):
    access.require_course(course_id)
    if request.method == "GET":
        return redirect(url_for("admin_groups.groups_list", course_id=course_id))

    try:
        groups_service.create_group(
            course_id=course_id,
            name=request.form.get("name", ""),
            actor=_actor(),
        )
    except ServiceError as failure:
        from app import db

        course = db.get_db().execute(
            "SELECT * FROM courses WHERE id = ?", (course_id,)
        ).fetchone()
        if course is None:
            access.abort_not_found()
        return (
            render_template(
                "admin/groups_list.html",
                course=course,
                groups=groups_service.list_groups(course_id),
                error=str(failure),
                form={"name": request.form.get("name", "")},
            ),
            400,
        )
    flash("Группа создана.", "info")
    return redirect(url_for("admin_groups.groups_list", course_id=course_id))


@bp.route("/groups/<int:group_id>/edit", methods=["GET", "POST"])
@access.admin_required
def group_edit(group_id: int):
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_course(group["course_id"])
    if group["archived_at"] is not None:
        access.abort_forbidden()

    if request.method == "GET":
        return render_template(
            "admin/group_form.html",
            mode="edit",
            group=group,
            form={"name": group["name"]},
            error=None,
        )

    try:
        groups_service.update_group(
            group_id=group_id,
            name=request.form.get("name", ""),
            actor=_actor(),
        )
    except ServiceError as failure:
        return (
            render_template(
                "admin/group_form.html",
                mode="edit",
                group=group,
                form={"name": request.form.get("name", "")},
                error=str(failure),
            ),
            400,
        )
    flash("Название группы сохранено.", "info")
    return redirect(url_for("admin_groups.groups_list", course_id=group["course_id"]))


@bp.post("/groups/<int:group_id>/delete")
@access.admin_required
def group_delete(group_id: int):
    """Удалить или архивировать группу (BR-25…BR-27).

    Отказ по BR-25 возвращается на страницу групп с текстом §9.3 и
    кодом 400: операция не выполнена, и администратор видит причину
    (AC-30).
    """
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_course(group["course_id"])
    try:
        outcome = groups_service.delete_or_archive_group(
            group_id=group_id, actor=_actor()
        )
    except ServiceError as failure:
        from app import db

        course = db.get_db().execute(
            "SELECT * FROM courses WHERE id = ?", (group["course_id"],)
        ).fetchone()
        return (
            render_template(
                "admin/groups_list.html",
                course=course,
                groups=groups_service.list_groups(group["course_id"]),
                error=str(failure),
                form={"name": ""},
            ),
            400,
        )
    if outcome == "archived":
        flash(
            "Группа переведена в архив: работы сохранены, группа "
            "исключена из активных списков.",
            "info",
        )
    else:
        flash("Группа удалена.", "info")
    return redirect(url_for("admin_groups.groups_list", course_id=group["course_id"]))


# --------------------------------------------------------------------------
# Участники и кураторы (FR-19…FR-21)
# --------------------------------------------------------------------------


@bp.get("/groups/<int:group_id>/members")
@access.staff_required
def group_members(group_id: int):
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    # Группа, а не только курс: куратор прикреплён к конкретным группам
    # и не должен читать чужие списки, даже в своём курсе.
    access.require_group(group_id)
    return _render_members(group)


@bp.post("/groups/<int:group_id>/members/new")
@access.admin_required
def member_create(group_id: int):
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_course(group["course_id"])
    if group["archived_at"] is not None:
        access.abort_forbidden()
    form = {
        "full_name": request.form.get("full_name", ""),
        "role": request.form.get("role", "student"),
        "email": request.form.get("email", ""),
        "password": request.form.get("password", ""),
    }
    try:
        members_service.create_member(
            course_id=group["course_id"],
            group_id=group_id,
            full_name=form["full_name"],
            role=form["role"],
            email=form["email"],
            password=form["password"],
            actor=_actor(),
        )
    except ServiceError as failure:
        return _render_members(group, error=str(failure), form=form), 400
    flash("Участник добавлен в группу.", "info")
    return redirect(url_for("admin_groups.group_members", group_id=group_id))


@bp.post("/groups/<int:group_id>/members/<int:user_id>/edit")
@access.admin_required
def member_edit(group_id: int, user_id: int):
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_course(group["course_id"])
    if not members_service.is_participant(group_id, user_id):
        access.abort_not_found()
    form = {
        "full_name": request.form.get("full_name", ""),
        "email": request.form.get("email", ""),
    }
    try:
        members_service.update_member(
            user_id=user_id,
            full_name=form["full_name"],
            email=form["email"],
            actor=_actor(),
        )
    except ServiceError as failure:
        return _render_members(group, error=str(failure), form=form), 400
    flash("Данные участника сохранены.", "info")
    return redirect(url_for("admin_groups.group_members", group_id=group_id))


@bp.post("/groups/<int:group_id>/members/<int:user_id>/remove")
@access.admin_required
def member_remove(group_id: int, user_id: int):
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_course(group["course_id"])
    try:
        members_service.remove_member(
            group_id=group_id, user_id=user_id, actor=_actor()
        )
    except ServiceError as failure:
        return _render_members(group, error=str(failure)), 400
    flash(
        "Участник исключён из группы. Учётная запись отключена, его "
        "прошлые работы сохранены.",
        "info",
    )
    return redirect(url_for("admin_groups.group_members", group_id=group_id))


# --------------------------------------------------------------------------
# Прикрепление и открепление куратора (FR-21, AC-37)
# --------------------------------------------------------------------------


@bp.post("/groups/<int:group_id>/curators/attach")
@access.admin_required
def curator_attach(group_id: int):
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_course(group["course_id"])
    if group["archived_at"] is not None:
        access.abort_forbidden()
    raw_user_id = request.form.get("user_id", "")
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return _render_members(
            group, error=config.MESSAGES["curator_required"]
        ), 400
    try:
        members_service.attach_curator(
            group_id=group_id, user_id=user_id, actor=_actor()
        )
    except ServiceError as failure:
        return _render_members(group, error=str(failure)), 400
    flash("Куратор прикреплён к группе.", "info")
    return redirect(url_for("admin_groups.group_members", group_id=group_id))


@bp.post("/groups/<int:group_id>/curators/<int:user_id>/detach")
@access.admin_required
def curator_detach(group_id: int, user_id: int):
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_course(group["course_id"])
    try:
        members_service.detach_curator(
            group_id=group_id, user_id=user_id, actor=_actor()
        )
    except ServiceError as failure:
        return _render_members(group, error=str(failure)), 400
    flash("Куратор откреплён от группы.", "info")
    return redirect(url_for("admin_groups.group_members", group_id=group_id))


# --------------------------------------------------------------------------
# Пароли участников глазами администратора (FR-09, FR-10, AC-19, AC-20)
# --------------------------------------------------------------------------


@bp.post("/groups/<int:group_id>/members/<int:user_id>/password/view")
@access.admin_required
def member_password_view(group_id: int, user_id: int):
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_course(group["course_id"])
    clear = members_service.reveal_member_password(
        group_id=group_id, user_id=user_id, actor=_actor()
    )
    return _render_members(group, revealed={user_id: clear})


@bp.post("/groups/<int:group_id>/members/<int:user_id>/password/reissue")
@access.admin_required
def member_password_reissue(group_id: int, user_id: int):
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_course(group["course_id"])
    clear = members_service.reissue_member_password(
        group_id=group_id, user_id=user_id, actor=_actor()
    )
    flash("Новый пароль выдан участнику.", "info")
    return _render_members(group, new_password=(user_id, clear))
