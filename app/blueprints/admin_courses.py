"""Курсы: создание, просмотр, правка и удаление (FR-13…FR-17, BR-22)
плюс дашборд администратора (FR-55, FR-57…FR-60, этап Э-7).

Только администратор. Проверка роли стоит на каждом маршруте через
``access.admin_required`` (BR-33) — скрытие кнопки в шаблоне не считается
разрешением (FR-07).

Удаление курса идёт через GET+POST, а не через одну кнопку: перед
удалением нужно показать перечень последствий и поле подтверждения
(§9.1, «Подтверждение действия»), а этот перечень считается на сервере.
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

from app import access
from app.services import ServiceError, courses as courses_service
from app.services import dashboard as dashboard_service

bp = Blueprint("admin_courses", __name__, url_prefix="/admin")


def _actor():
    return access.current_user()


@bp.get("/dashboard")
@access.admin_required
def dashboard():
    """Сводные показатели, диаграмма и разрезы по всем курсам
    (FR-55, FR-57, FR-60, AC-41).

    Фильтры читаются из строки запроса и применяются одновременно
    (FR-58): маршрут их не валидирует, это работа сервиса — он же
    решает, попадает ли значение в область видимости.
    """
    return render_template(
        "admin/dashboard.html",
        dashboard=dashboard_service.build(_actor(), request.args),
    )


@bp.get("/courses")
@access.admin_required
def courses_list():
    return render_template(
        "admin/courses_list.html",
        courses=courses_service.list_courses(),
    )


@bp.route("/courses/new", methods=["GET", "POST"])
@access.admin_required
def course_create():
    form = {"title": "", "description": ""}
    if request.method == "GET":
        return render_template(
            "admin/course_form.html", mode="create", form=form, error=None
        )
    form = {
        "title": request.form.get("title", ""),
        "description": request.form.get("description", ""),
    }
    try:
        course_id = courses_service.create_course(
            title=form["title"],
            description=form["description"],
            actor=_actor(),
        )
    except ServiceError as failure:
        return (
            render_template(
                "admin/course_form.html",
                mode="create",
                form=form,
                error=str(failure),
            ),
            400,
        )
    flash("Курс создан.", "info")
    return redirect(url_for("admin_courses.course_edit", course_id=course_id))


@bp.route("/courses/<int:course_id>/edit", methods=["GET", "POST"])
@access.admin_required
def course_edit(course_id: int):
    course = courses_service.fetch_course(course_id)
    if course is None:
        access.abort_not_found()

    if request.method == "GET":
        return render_template(
            "admin/course_form.html",
            mode="edit",
            course=course,
            form={"title": course["title"], "description": course["description"] or ""},
            error=None,
        )

    form = {
        "title": request.form.get("title", ""),
        "description": request.form.get("description", ""),
    }
    try:
        courses_service.update_course(
            course_id=course_id,
            title=form["title"],
            description=form["description"],
            actor=_actor(),
        )
    except ServiceError as failure:
        return (
            render_template(
                "admin/course_form.html",
                mode="edit",
                course=course,
                form=form,
                error=str(failure),
            ),
            400,
        )
    flash("Изменения курса сохранены.", "info")
    return redirect(url_for("admin_courses.course_edit", course_id=course_id))


@bp.route("/courses/<int:course_id>/delete", methods=["GET", "POST"])
@access.admin_required
def course_delete(course_id: int):
    """Перечень последствий и ввод названия курса (FR-16, BR-22)."""
    course = courses_service.fetch_course(course_id)
    if course is None:
        access.abort_not_found()
    preview = courses_service.deletion_preview(course_id)

    if request.method == "GET":
        return render_template(
            "admin/course_delete.html",
            course=course,
            preview=preview,
            confirmation_text=courses_service.confirm_text(preview),
            typed="",
            error=None,
        )

    typed = request.form.get("confirmation", "")
    try:
        courses_service.delete_course(
            course_id=course_id, confirmation=typed, actor=_actor()
        )
    except ServiceError as failure:
        return (
            render_template(
                "admin/course_delete.html",
                course=course,
                preview=preview,
                confirmation_text=courses_service.confirm_text(preview),
                typed=typed,
                error=str(failure),
            ),
            400,
        )
    flash("Курс удалён: {title}. Записи журнала сохранены.".format(
        title=course["title"]
    ), "info")
    return redirect(url_for("admin_courses.courses_list"))
