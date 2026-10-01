"""Задания курса: список, форма с предпросмотром, удаление, материалы
(FR-32…FR-35, BR-23, BR-24, AC-29, AC-40).

Чтение списка открыто администратору и куратору
(``access.staff_required``): куратор смотрит задания своих курсов, ничего
не меняя. Создание, правка и удаление остаются за
``access.admin_required`` (BR-33), курс и задание проверяются на каждом
маршруте (FR-07, FR-08). Ответы 403 и 404 не смешиваются.

Порядок сценария задан FR-34 и не является деталью оформления:

1. ``assignment_form``     GET — пустая форма; POST — файлы кладутся в
                           проверку, форма собирается в черновик, и
                           делается переход на предпросмотр.
2. ``assignment_preview``  GET — собранная форма, но в базе ещё ничего
                           нет. Открытие формы без ``?preview=1``
                           сбрасывает незавершённый черновик, иначе
                           кнопка «Подтвердить» предложила бы сохранить
                           не то, что администратор видел.
3. ``assignment_confirm``  POST — повторная проверка и запись.
4. ``assignment_discard``  POST — отмена: файлы удаляются, в базу не
                           пишется ничего.

Отдельный адрес подтверждения и отмена через POST с ``data-confirm``
перенесены из импорта участников (§4.2 не запрещает подтверждения, но
случайный клик по ссылке из закладок не должен ничего сохранять).
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

from app import access, attachments, config, db
from app.services import ServiceError
from app.services import assignments as assignments_service
from app.services import courses as courses_service

bp = Blueprint("admin_assignments", __name__, url_prefix="/admin")


def _actor():
    return access.current_user()


def _course(course_id: int):
    course = courses_service.fetch_course(course_id)
    if course is None:
        access.abort_not_found()
    return course


def _assignment(course_id: int, assignment_id: int):
    """Задание, и только если оно принадлежит указанному курсу.

    Проверка принадлежности обязательна: идентификаторы заданий
    сквозные по всей базе, и без неё можно было бы править чужое
    задание, зная его номер из другого курса.
    """
    assignment = assignments_service.fetch(assignment_id)
    if assignment is None or assignment["course_id"] != course_id:
        access.abort_not_found()
    return assignment


def _list_files(assignment_id: int) -> list[dict]:
    """Материалы задания для формы и страницы просмотра."""
    rows = db.get_db().execute(
        """
        SELECT id, original_name, size_bytes, mime, created_at
          FROM assignment_files
         WHERE assignment_id = ?
         ORDER BY id
        """,
        (assignment_id,),
    ).fetchall()
    payload = [dict(row) for row in rows]
    for item in payload:
        item["size_label"] = attachments.human_size(item["size_bytes"])
    return payload


def _collect_uploads():
    """Сложить выбранные файлы в список описаний.

    Ошибка любого одного файла отменяет всю отправку: форма
    показывается заново с текстом отказа, а уже проверенные файлы
    убираются сборщиком мусора. Частично принятая пачка означала бы,
    что администратор видит в списке меньше файлов, чем отправил.
    """
    items: list[dict] = []
    errors: list[str] = []
    uploads = [item for item in request.files.getlist("files") if item.filename]
    for upload in uploads:
        try:
            items.append(attachments.stage_upload(upload))
        except ServiceError as failure:
            errors.append(str(failure))
    if errors:
        for item in items:
            attachments.discard_staged(item["staged_name"])
        raise ServiceError(" ".join(dict.fromkeys(errors)))
    return items


def _form_values():
    """Поля формы. Описание приходит текстом либо файлом (FR-33).

    Если приложен файл, его содержимое подставляется в описание и в
    базу не попадает: хранится только текст. Ручной текст при этом не
    теряется молча — если он тоже заполнен, отказ объясняет, что
    задано и то и другое, иначе администратор решил бы, что его правка
    сохранилась.
    """
    typed = request.form.get("description", "").strip()
    upload = request.files.get("description_file")
    if upload is None or not (upload.filename or "").strip():
        return {
            "title": request.form.get("title", ""),
            "description": typed,
            "due_at": request.form.get("due_at", ""),
        }
    if typed:
        raise ServiceError(
            "Заполните описание задания либо приложите файл, но не оба "
            "сразу: сейчас заполнены оба поля."
        )
    return {
        "title": request.form.get("title", ""),
        "description": attachments.read_description(upload),
        "due_at": request.form.get("due_at", ""),
    }


def _form_from_assignment(assignment) -> dict[str, str]:
    return {
        "title": assignment["title"],
        "description": assignment["description"] or "",
        "due_at": assignments_service.to_input_value(assignment["due_at"]),
    }


# --------------------------------------------------------------------------
# Список
# --------------------------------------------------------------------------


@bp.get("/courses/<int:course_id>/assignments")
@access.staff_required
def assignments_list(course_id: int):
    access.require_course(course_id)
    course = _course(course_id)
    items = assignments_service.list_for_course(course["id"])
    return render_template(
        "admin/assignments_list.html",
        course=course,
        items=items,
        active=[item for item in items if not item["is_archived"]],
        archived=[item for item in items if item["is_archived"]],
    )


# --------------------------------------------------------------------------
# Форма и предпросмотр
# --------------------------------------------------------------------------


def _render_form(
    *,
    mode: str,
    course,
    assignment,
    form: dict[str, str],
    files: list[dict],
    error=None,
    status=200,
):
    return (
        render_template(
            "admin/assignment_form.html",
            mode=mode,
            course=course,
            assignment=assignment,
            form=form,
            files=files,
            files_left=max(config.MAX_FILES_PER_ASSIGNMENT - len(files), 0),
            error=error,
        ),
        status,
    )


@bp.route("/courses/<int:course_id>/assignments/new", methods=["GET", "POST"])
@access.admin_required
def assignment_create(course_id: int):
    access.require_course(course_id)
    course = _course(course_id)

    if request.method == "GET":
        if request.args.get("preview") != "1":
            assignments_service.discard_draft()
        return _render_form(
            mode="create",
            course=course,
            assignment=None,
            form={"title": "", "description": "", "due_at": ""},
            files=[],
        )

    form = {
        "title": request.form.get("title", ""),
        "description": request.form.get("description", ""),
        "due_at": request.form.get("due_at", ""),
    }
    try:
        form = _form_values()
        items = _collect_uploads()
        assignments_service.stage_draft(
            course_id=course["id"],
            title=form["title"],
            description=form["description"],
            due_at=form["due_at"],
            files=items,
            mode="create",
        )
    except ServiceError as failure:
        return _render_form(
            mode="create",
            course=course,
            assignment=None,
            form=form,
            files=[],
            error=str(failure),
            status=400,
        )
    return redirect(
        url_for(
            "admin_assignments.assignment_preview",
            course_id=course["id"],
            preview=1,
        )
    )


@bp.route(
    "/courses/<int:course_id>/assignments/<int:assignment_id>/edit",
    methods=["GET", "POST"],
)
@access.admin_required
def assignment_edit(course_id: int, assignment_id: int):
    access.require_course(course_id)
    course = _course(course_id)
    assignment = _assignment(course_id, assignment_id)

    if request.method == "GET":
        if request.args.get("preview") != "1":
            assignments_service.discard_draft()
        return _render_form(
            mode="edit",
            course=course,
            assignment=assignment,
            form=_form_from_assignment(assignment),
            files=_list_files(assignment_id),
        )

    form = {
        "title": request.form.get("title", ""),
        "description": request.form.get("description", ""),
        "due_at": request.form.get("due_at", ""),
    }
    try:
        form = _form_values()
        items = _collect_uploads()
        assignments_service.stage_draft(
            course_id=course["id"],
            title=form["title"],
            description=form["description"],
            due_at=form["due_at"],
            files=items,
            mode="edit",
            assignment_id=assignment_id,
            existing_files=assignments_service.files_count(assignment_id),
        )
    except ServiceError as failure:
        return _render_form(
            mode="edit",
            course=course,
            assignment=assignment,
            form=form,
            files=_list_files(assignment_id),
            error=str(failure),
            status=400,
        )
    return redirect(
        url_for(
            "admin_assignments.assignment_preview",
            course_id=course["id"],
            assignment_id=assignment_id,
            preview=1,
        )
    )


# Два адреса у одной функции: при создании задания ещё нет номера, а
# при правке он есть. Параметр ``assignment_id`` в правиле URL
# необязательным быть не может, а одна функция нужна, чтобы правила
# показа и отмены не разошлись.
@bp.get("/courses/<int:course_id>/assignments/preview")
@bp.get("/courses/<int:course_id>/assignments/<int:assignment_id>/preview")
@access.admin_required
def assignment_preview(course_id: int, assignment_id: int | None = None):
    access.require_course(course_id)
    course = _course(course_id)
    assignment = None
    if assignment_id is not None:
        assignment = _assignment(course_id, assignment_id)

    draft = assignments_service.draft_for_view()
    if draft is None or draft["course_id"] != course["id"]:
        assignments_service.discard_draft()
        flash(config.MESSAGES["assignment_preview_expired"], "error")
        return redirect(
            url_for("admin_assignments.assignments_list", course_id=course["id"])
        )

    return render_template(
        "admin/assignment_preview.html",
        course=course,
        draft=draft,
        assignment=assignment,
        existing_files=_list_files(assignment["id"]) if assignment else [],
    )


@bp.post("/courses/<int:course_id>/assignments/confirm")
@access.admin_required
def assignment_confirm(course_id: int):
    access.require_course(course_id)
    course = _course(course_id)
    draft = assignments_service.read_draft()
    if draft is None or draft["course_id"] != course["id"]:
        flash(config.MESSAGES["assignment_preview_expired"], "error")
        return redirect(
            url_for("admin_assignments.assignments_list", course_id=course["id"])
        )

    try:
        if draft["mode"] == "edit" and draft["assignment_id"] is not None:
            _assignment(course["id"], draft["assignment_id"])
            assignments_service.update_assignment(
                assignment_id=draft["assignment_id"],
                title=draft["title"],
                description=draft["description"],
                due_at=draft["due_at"],
                actor=_actor(),
                files=draft["files"],
            )
            flash("Задание сохранено.", "info")
            target = url_for(
                "admin_assignments.assignment_edit",
                course_id=course["id"],
                assignment_id=draft["assignment_id"],
            )
        else:
            new_id = assignments_service.create_assignment(
                course_id=course["id"],
                title=draft["title"],
                description=draft["description"],
                due_at=draft["due_at"],
                actor=_actor(),
                files=draft["files"],
            )
            flash("Задание создано.", "info")
            target = url_for(
                "admin_assignments.assignment_edit",
                course_id=course["id"],
                assignment_id=new_id,
            )
    except ServiceError as failure:
        flash(str(failure), "error")
        # Предпросмотр должен открыть то же задание, которое правили:
        # без номера вернулось бы создание нового, и подтвердить можно
        # было бы не тот вариант, который виден на экране.
        return redirect(
            url_for(
                "admin_assignments.assignment_preview",
                course_id=course["id"],
                assignment_id=(
                    draft["assignment_id"]
                    if draft.get("mode") == "edit"
                    and draft.get("assignment_id") is not None
                    else None
                ),
                preview=1,
            )
        )

    assignments_service.discard_draft()
    return redirect(target)


@bp.post("/courses/<int:course_id>/assignments/discard")
@access.admin_required
def assignment_discard(course_id: int):
    access.require_course(course_id)
    _course(course_id)
    assignments_service.discard_draft()
    flash("Черновик отменён. В базу ничего не записано.", "info")
    return redirect(
        url_for("admin_assignments.assignments_list", course_id=course_id)
    )


# --------------------------------------------------------------------------
# Удаление
# --------------------------------------------------------------------------


@bp.route(
    "/courses/<int:course_id>/assignments/<int:assignment_id>/delete",
    methods=["GET", "POST"],
)
@access.admin_required
def assignment_delete(course_id: int, assignment_id: int):
    """Перечень последствий и подтверждение (BR-23, BR-24, §9.1).

    Что именно произойдёт — архивирование или физическое удаление —
    решает сервер по числу попыток сдачи, и этот же вывод показывается
    в перечне последствий. Ввод названия не требуется: он нужен только
    для удаления курса (FR-16), а здесь достаточно поля подтверждения.
    """
    access.require_course(course_id)
    course = _course(course_id)
    assignment = _assignment(course_id, assignment_id)

    submissions = assignments_service.submissions_count(assignment_id)
    files = assignments_service.files_count(assignment_id)
    will_archive = submissions > 0

    if request.method == "GET":
        return render_template(
            "admin/assignment_delete.html",
            course=course,
            assignment=assignment,
            submissions=submissions,
            files=files,
            will_archive=will_archive,
            error=None,
        )

    if request.form.get("confirm") != "on":
        return (
            render_template(
                "admin/assignment_delete.html",
                course=course,
                assignment=assignment,
                submissions=submissions,
                files=files,
                will_archive=will_archive,
                error="Отметьте подтверждение, чтобы удалить задание.",
            ),
            400,
        )

    outcome = assignments_service.delete_or_archive_assignment(
        assignment_id=assignment_id, actor=_actor()
    )
    if outcome == "archived":
        flash(
            "Задание перемещено в архив: работы и комментарии сохранены.",
            "info",
        )
    else:
        flash("Задание удалено: сдач по нему не было.", "info")
    return redirect(
        url_for("admin_assignments.assignments_list", course_id=course["id"])
    )


@bp.post(
    "/courses/<int:course_id>/assignments/<int:assignment_id>/files"
    "/<int:file_id>/delete"
)
@access.admin_required
def assignment_file_delete(course_id: int, assignment_id: int, file_id: int):
    access.require_course(course_id)
    _course(course_id)
    _assignment(course_id, assignment_id)
    try:
        assignments_service.remove_file(
            file_id=file_id, assignment_id=assignment_id, actor=_actor()
        )
    except ServiceError as failure:
        flash(str(failure), "error")
    else:
        flash("Файл откреплён от задания.", "info")
    return redirect(
        url_for(
            "admin_assignments.assignment_edit",
            course_id=course_id,
            assignment_id=assignment_id,
        )
    )
