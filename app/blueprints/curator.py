"""Дашборд групп и проверка работ (FR-45…FR-50, FR-56, FR-58…FR-60,
BR-20, BR-21, AC-11…AC-16).

Куратор прикрепляется к группе, а не к курсу, поэтому и работать он
может только со студентами своих групп: правило считает объединение
всех учётных записей человека (``submissions.can_open``, BR-29), и
маршрут повторяет эту же проверку, а не полагается на декоратор.

Дашборд (Э-7) устроен так же: область видимости считает сервис по
роли, а маршрут только отдаёт ему строку запроса с фильтрами.

Три маршрута записи, и каждый — отдельное действие:

* ``/status``   — смена статуса работы «на проверке». Выбранное «сдано»
  может стать «сдано после дедлайна»: это считает сервер (FR-47);
* ``/revert``   — возврат принятой работы в доработку. Только
  администратор и только для терминального статуса (FR-48, BR-21);
* ``/comments`` — сообщение в переписку, доступное обеим сторонам
  независимо от статуса (FR-50).

Отказ по праву здесь — 404, а не 403: куратор чужой группы не должен
узнавать по коду ответа, существует ли работа (§9.3).
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
from app.services import comments as comments_service
from app.services import dashboard as dashboard_service
from app.services import groups as groups_service
from app.services import members as members_service
from app.services import statuses as statuses_service
from app.services import submissions as submissions_service

bp = Blueprint("curator", __name__, url_prefix="/curator")


def _group(group_id: int):
    """Группа, если текущий пользователь имеет к ней доступ (BR-29)."""
    group = groups_service.fetch_group(group_id)
    if group is None:
        access.abort_not_found()
    access.require_group(group_id)
    return group


def _submission(submission_id: int):
    """Работа, которую текущий пользователь вправе открыть, иначе 404.

    Доступ сверяется здесь, а не только в сервисе: карточка отдаёт
    переписку и файлы студента, и одна проверка в ``can_open`` должна
    закрывать и страницу, и её содержимое (FR-49).
    """
    user = access.current_user()
    context = submissions_service.fetch_context(submission_id)
    if context is None or not submissions_service.can_open(user, context):
        access.abort_not_found()
    return user, context


def _collect_uploads() -> list[dict]:
    """Сложить выбранные файлы в список описаний (FR-46, FR-52)."""
    items: list[dict] = []
    errors: list[str] = []
    uploads = [item for item in request.files.getlist("files") if item.filename]
    for upload in uploads:
        try:
            items.append(attachments.stage_upload(upload))
        except ServiceError as failure:
            errors.append(str(failure))
    if errors:
        attachments.discard_all([item["staged_name"] for item in items])
        raise ServiceError(" ".join(dict.fromkeys(errors)))
    return items


def _discard(items: list[dict]) -> None:
    attachments.discard_all([item["staged_name"] for item in items])


# --------------------------------------------------------------------------
# Дашборд куратора (FR-56, FR-58…FR-60, AC-16, AC-17)
# --------------------------------------------------------------------------


@bp.get("/dashboard")
@access.staff_required
def dashboard():
    """Сводные показатели и прогресс в границах групп куратора.

    Декоратор допускает администратора: по §3 у него «все дашборды».
    Область видимости считает сервис по роли — администратор увидит
    те же цифры, что и на своём дашборде, а куратору чужие группы не
    попадут в опции фильтров и в выборку (AC-16, BR-29).
    """
    return render_template(
        "curator/dashboard.html",
        dashboard=dashboard_service.build(access.current_user(), request.args),
    )


# --------------------------------------------------------------------------
# Работы группы: студенты и их статусы (FR-45, FR-49)
# --------------------------------------------------------------------------


@bp.get("/groups/<int:group_id>/students")
@access.staff_required
def group_students(group_id: int):
    """Студенты группы и статус их работ по заданиям курса.

    Для каждого студента берётся последняя попытка по каждому заданию:
    история целиком открывается на карточке работы (FR-44), а здесь
    нужен только итоговый статус, чтобы глазом увидеть, кому писать.
    """
    group = _group(group_id)
    connection = db.get_db()

    participants = members_service.list_participants(group_id)
    students = [p for p in participants if p["role"] == "student"]
    assignments = assignments_service.list_for_course(group["course_id"])

    latest: dict[tuple[int, int], dict] = {}
    if students and assignments:
        student_marks = ",".join("?" for _ in students)
        assignment_marks = ",".join("?" for _ in assignments)
        rows = connection.execute(
            """
            SELECT id, student_id, assignment_id, attempt_no, status
              FROM submissions
             WHERE student_id IN ({students})
               AND assignment_id IN ({assignments})
             ORDER BY attempt_no
            """.format(students=student_marks, assignments=assignment_marks),
            [s["id"] for s in students] + [a["id"] for a in assignments],
        ).fetchall()
        for row in rows:
            # Порядок по attempt_no: последняя запись пары и есть
            # текущая работа студента.
            latest[(row["student_id"], row["assignment_id"])] = dict(row)

    for student in students:
        cells = []
        for assignment in assignments:
            submission = latest.get((student["id"], assignment["id"]))
            cells.append(
                {
                    "assignment": assignment,
                    "submission_id": submission["id"] if submission else None,
                    "status": (
                        submission["status"]
                        if submission
                        else "not_submitted"
                    ),
                    "attempt_no": (
                        submission["attempt_no"] if submission else 0
                    ),
                }
            )
        student["cells"] = cells

    return render_template(
        "curator/group_students.html",
        group=group,
        course_id=group["course_id"],
        course_title=group["course_title"],
        students=students,
        assignments=assignments,
        action_url=url_for(
            "admin_groups.group_members", group_id=group["id"]
        ),
        action_label="Участники группы",
    )


# --------------------------------------------------------------------------
# Карточка работы (FR-44…FR-46)
# --------------------------------------------------------------------------


def _card_context(user, context):
    """Всё содержимое карточки: история, текущая попытка, переписка."""
    attempts = submissions_service.attempts(
        context["assignment_id"], context["student_id"]
    )
    current = next(
        (item for item in attempts if item["id"] == context["id"]),
        None,
    )
    thread = comments_service.thread(context["id"])
    return {
        "context": context,
        "attempts": attempts,
        "current": current,
        "thread": thread,
        "files": submissions_service.files_of(context["id"]),
        "can_review": submissions_service.can_review(user, context),
        "is_admin": user["role"] == access.ADMIN_ROLE,
        "terminal": statuses_service.is_terminal(context["status"]),
        "max_comment": config.COMMENT_MAX_LENGTH,
        "max_comment_files": comments_service.MAX_FILES_PER_COMMENT,
        "attempt_limit": config.SUBMISSION_ATTEMPT_LIMIT,
    }


@bp.get("/submissions/<int:submission_id>")
@access.staff_required
def submission_card(submission_id: int):
    user, context = _submission(submission_id)
    return render_template(
        "curator/submission.html",
        **_card_context(user, context),
    )


# --------------------------------------------------------------------------
# Смена статуса (FR-45, FR-47, BR-17…BR-20, AC-11…AC-14)
# --------------------------------------------------------------------------


@bp.post("/submissions/<int:submission_id>/status")
@access.staff_required
def submission_status(submission_id: int):
    """Поставить «сдано» либо «на доработку» (FR-45).

    Фактический статус возвращается сервисом: «сдано» после дедлайна
    сохраняется как «сдано после дедлайна», и об этом же говорится в
    подтверждении — иначе куратор решил бы, что нажал не то.
    """
    _submission(submission_id)
    target = url_for("curator.submission_card", submission_id=submission_id)

    items: list[dict] = []
    try:
        items = _collect_uploads()
        final = statuses_service.change_status(
            submission_id=submission_id,
            requested=request.form.get("requested", ""),
            actor=access.current_user(),
            comment=request.form.get("comment"),
            files=items,
        )
    except ServiceError as failure:
        _discard(items)
        flash(str(failure), "error")
        return redirect(target)

    flash(
        "Статус работы: {0}.".format(config.STATUSES[final]["label"]),
        "info",
    )
    return redirect(target)


# --------------------------------------------------------------------------
# Отзыв проверки (FR-48, BR-21, AC-15)
# --------------------------------------------------------------------------


@bp.post("/submissions/<int:submission_id>/revert")
@access.admin_required
def submission_revert(submission_id: int):
    """Вернуть принятую работу в «на доработку» (только администратор)."""
    _submission(submission_id)
    target = url_for("curator.submission_card", submission_id=submission_id)

    items: list[dict] = []
    try:
        items = _collect_uploads()
        statuses_service.revert_review(
            submission_id=submission_id,
            actor=access.current_user(),
            comment=request.form.get("comment"),
            files=items,
        )
    except ServiceError as failure:
        _discard(items)
        flash(str(failure), "error")
        return redirect(target)

    flash("Работа возвращена на доработку.", "info")
    return redirect(target)


# --------------------------------------------------------------------------
# Комментарий проверяющего (FR-46, FR-50)
# --------------------------------------------------------------------------


@bp.post("/submissions/<int:submission_id>/comments")
@access.staff_required
def submission_comment(submission_id: int):
    """Сообщение куратора или администратора в переписке (FR-50)."""
    _submission(submission_id)
    target = url_for("curator.submission_card", submission_id=submission_id)

    items: list[dict] = []
    try:
        items = _collect_uploads()
        with db.transaction() as connection:
            comments_service.add(
                submission_id=submission_id,
                author=access.current_user(),
                body=request.form.get("body"),
                files=items,
                connection=connection,
            )
    except ServiceError as failure:
        _discard(items)
        flash(str(failure), "error")
        return redirect(target)

    flash("Комментарий добавлен.", "info")
    return redirect(target)
