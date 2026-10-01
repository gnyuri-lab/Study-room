"""Импорт списка участников курса из файла (FR-24…FR-30, AC-23…AC-27).

Только администратор: ``access.admin_required`` на каждом маршруте
(BR-33), курс проверяется ``access.require_course`` (FR-08). Ответы 403
и 404 остаются разными, как и на остальных административных экранах.

Три маршрута, и порядок между ними — часть сценария, а не деталь:

``import_upload``   GET — форма загрузки и последний отчёт; POST —
                    разбор файла и предпросмотр. Отказ на уровне файла
                    (не тот формат, нечитаемо, нет строк) показывается
                    на этой же странице с кодом 400.
``import_confirm``  POST — запись принятых строк и отчёта.
``import_cancel``   POST — отмена: файл удаляется, в базу не пишется
                    ничего (FR-25).

Предпросмотр и подтверждение разнесены по разным адресам намеренно:
подтверждение — POST без формы, с отменой через ``data-confirm``, чтобы
случайный клик по «Открыть» из закладок не импортировал файл.
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
from app.services import ServiceError, importer

bp = Blueprint("admin_import", __name__, url_prefix="/admin")


def _actor():
    return access.current_user()


def _course(course_id: int):
    from app import db

    course = db.get_db().execute(
        "SELECT * FROM courses WHERE id = ?", (course_id,)
    ).fetchone()
    if course is None:
        access.abort_not_found()
    return course


def _render_upload(course, *, error=None, status=200):
    """Страница загрузки: форма и последний отчёт по курсу (AC-38)."""
    batches = importer.history(course_id=course["id"])
    latest = batches[0] if batches else None
    errors = importer.batch_errors(latest["id"]) if latest else []
    return (
        render_template(
            "admin/import_upload.html",
            course=course,
            error=error,
            batches=batches,
            latest=latest,
            latest_errors=errors,
        ),
        status,
    )


@bp.route("/courses/<int:course_id>/import", methods=["GET", "POST"])
@access.admin_required
def import_upload(course_id: int):
    access.require_course(course_id)
    course = _course(course_id)

    if request.method == "GET":
        # Открытие страницы сбрасывает незавершённый предпросмотр: файл
        # лежит на диске и ждёт подтверждения, поэтому без явного
        # ?preview=1 его надо убрать, иначе кнопка «Подтвердить»
        # показывала бы не тот файл, который администратор видел.
        if request.args.get("preview") != "1":
            importer.discard()
        return _render_upload(course)

    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        return _render_upload(
            course, error=config.MESSAGES["roster_no_file"], status=400
        )

    try:
        preview = importer.preview(course_id=course_id, upload=upload)
    except ServiceError as failure:
        return _render_upload(course, error=str(failure), status=400)

    return render_template(
        "admin/import_preview.html",
        course=course,
        verdicts=preview["verdicts"],
        counts=preview["counts"],
        file_name=preview["file_name"],
    )


@bp.route("/courses/<int:course_id>/import/confirm", methods=["POST"])
@access.admin_required
def import_confirm(course_id: int):
    access.require_course(course_id)
    try:
        result = importer.confirm(course_id=course_id, actor=_actor())
    except ServiceError as failure:
        flash(str(failure), "error")
        return redirect(
            url_for("admin_import.import_upload", course_id=course_id)
        )
    counts = result["counts"]
    flash(
        config.MESSAGES["import_report"].format(
            корректных=counts["valid"],
            всего=counts["total"],
            ошибок=counts["invalid"],
        ),
        "info",
    )
    return redirect(url_for("admin_import.import_upload", course_id=course_id))


@bp.route("/courses/<int:course_id>/import/cancel", methods=["POST"])
@access.admin_required
def import_cancel(course_id: int):
    access.require_course(course_id)
    importer.discard()
    flash("Импорт отменён. В базу ничего не записано.", "info")
    return redirect(url_for("admin_import.import_upload", course_id=course_id))
