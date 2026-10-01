"""Студенческие экраны: курсы, задания, сдача и переписка
(AC-04, FR-37…FR-44, FR-50, AC-05…AC-10).

На Э-5 модуль умел только читать; здесь появляются две записи — новая
попытка сдачи и комментарий — и одна страница курсов студента. Правила
этого файла:

* **Сдача — только своё задание своего курса.** Курс и задание
  сверяются с адресом (FR-08), а архивное задание недоступно и на
  прямой ссылке (FR-23).
* **Причина отказа показывается, а не догадывается.** Блокировка
  сдачи приходит текстом из сервиса (§9.3): лимит, «на проверке»,
  «принято» — студент видит причину рядом с формой, а не пустую кнопку.
* **Сбой валидации не теряет файлы.** Всё, что лежит на проверке, но не
  попало в базу, снимается с проверки сразу после отказа; иначе файлы
  копились бы до чистки мусора.
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
from app.services import courses as courses_service
from app.services import submissions as submissions_service

bp = Blueprint("student", __name__, url_prefix="/student")


def _student():
    """Текущий пользователь, если он студент, иначе 403.

    Отдельного декоратора ``student_required`` в ``access`` нет: роль
    «студент» проверяется здесь, а куратор смотрит работы из своей
    панели (FR-19, FR-20).
    """
    user = access.current_user()
    if user["role"] != "student":
        access.abort_forbidden()
    return user


def _course(course_id: int):
    """Курс, если студент имеет на него право (FR-08, BR-28)."""
    course = courses_service.fetch_course(course_id)
    if course is None:
        access.abort_not_found()
    if not access.can_view_course(course_id):
        access.abort_forbidden()
    return course


def _assignment(course, assignment_id: int):
    """Задание курса; архивное для студента не существует (FR-23)."""
    assignment = assignments_service.fetch_with_course(assignment_id)
    if assignment is None or assignment["course_id"] != course["id"]:
        access.abort_not_found()
    if assignment["archived_at"] is not None:
        access.abort_not_found()
    return assignment


def _collect_uploads() -> list[dict]:
    """Сложить выбранные файлы в список описаний (FR-52).

    Отказ любого файла отменяет всю пачку: частично принятая отправка
    означала бы, что студент видит в списке меньше файлов, чем прислал.
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
        attachments.discard_all([item["staged_name"] for item in items])
        raise ServiceError(" ".join(dict.fromkeys(errors)))
    return items


def _discard(items: list[dict]) -> None:
    attachments.discard_all([item["staged_name"] for item in items])


# --------------------------------------------------------------------------
# Мои курсы (AC-04, BR-28)
# --------------------------------------------------------------------------


@bp.get("/courses")
@access.login_required
def courses_list():
    """Курсы студента со счётчиком работ (AC-04, BR-28).

    Список собирается не по сеансной строке, а по всем записям человека:
    один e-mail может быть заведён в двух курсах (FR-05), и студент
    обязан увидеть оба.
    """
    user = _student()
    connection = db.get_db()

    visible = sorted(access.visible_course_ids())
    courses = []
    progress: dict[int, dict[str, int]] = {}
    # Прогресс считается по последней попытке каждого задания: более
    # ранние сдачи уже не показывают положения работы. Предыдущая
    # попытка могла остаться «на проверке» только вручную, а её
    # «на доработке» после пересдачи никого не интересует (AC-04).
    rows = connection.execute(
        """
        SELECT a.course_id AS course_id, s.status AS status, COUNT(*) AS total
          FROM submissions s
          JOIN assignments a ON a.id = s.assignment_id
         WHERE s.student_id = ?
           AND s.attempt_no = (
               SELECT MAX(s2.attempt_no) FROM submissions s2
                WHERE s2.assignment_id = s.assignment_id
                  AND s2.student_id = s.student_id
           )
         GROUP BY a.course_id, s.status
        """,
        (user["id"],),
    ).fetchall()
    for row in rows:
        bucket = progress.setdefault(
            row["course_id"],
            {"submitted": 0, "revision": 0, "accepted": 0, "accepted_late": 0},
        )
        bucket[row["status"]] = row["total"]

    for course_id in visible:
        course = courses_service.fetch_course(course_id)
        if course is None:
            continue
        counts = connection.execute(
            """
            SELECT COUNT(*) FROM assignments
             WHERE course_id = ? AND archived_at IS NULL
            """,
            (course_id,),
        ).fetchone()[0]
        buckets = progress.get(course_id, {})
        courses.append(
            {
                "id": course_id,
                "title": course["title"],
                "assignments_total": counts,
                "accepted": buckets.get("accepted", 0)
                + buckets.get("accepted_late", 0),
                "revision": buckets.get("revision", 0),
                "on_check": buckets.get("submitted", 0),
            }
        )

    return render_template("student/courses.html", courses=courses)


# --------------------------------------------------------------------------
# Список и карточка задания (FR-37, AC-05, AC-06)
# --------------------------------------------------------------------------


@bp.get("/courses/<int:course_id>/assignments")
@access.login_required
def assignments_list(course_id: int):
    user = _student()
    course = _course(course_id)
    items = assignments_service.list_for_student(course["id"], user["id"])
    return render_template(
        "student/assignments.html",
        course=course,
        items=items,
    )


def _card_context(user, course, assignment):
    """Всё, что нужно карточке задания: статус, история, переписка.

    История попыток и переписка читаются здесь, а не в шаблоне: форма
    сдачи должна знать причину отказа, и она тоже собирается в
    сервисном слое (FR-42, §9.3). Переписка берётся по **всем**
    попыткам: комментарий к первой сдаче остаётся частью разговора и
    после пересдачи (FR-44, FR-50, AC-09), а форма нового сообщения
    привязана к последней попытке — писать в закрытую сдачу нельзя.
    """
    attempts = submissions_service.attempts(assignment["id"], user["id"])
    thread = comments_service.thread_many([item["id"] for item in attempts])
    block_reason = None
    try:
        submissions_service.assert_submittable(assignment, user["id"])
    except ServiceError as failure:
        block_reason = str(failure)

    files = attachments.list_assignment_files(assignment["id"])
    return {
        "course": course,
        "assignment": assignment,
        "files": files,
        "due_label": assignments_service.format_due(assignment["due_at"]),
        "status": assignments_service.student_status(
            assignment["id"], user["id"]
        ),
        "attempts": attempts,
        "thread": thread,
        "latest_submission_id": attempts[-1]["id"] if attempts else None,
        "block_reason": block_reason,
        "max_files": config.MAX_FILES_PER_SUBMISSION,
        "max_comment": config.COMMENT_MAX_LENGTH,
        "max_comment_files": comments_service.MAX_FILES_PER_COMMENT,
        "attempt_limit": config.SUBMISSION_ATTEMPT_LIMIT,
    }


@bp.get("/courses/<int:course_id>/assignments/<int:assignment_id>")
@access.login_required
def assignment_card(course_id: int, assignment_id: int):
    user = _student()
    course = _course(course_id)
    assignment = _assignment(course, assignment_id)
    return render_template(
        "student/assignment.html",
        **_card_context(user, course, assignment),
    )


# --------------------------------------------------------------------------
# Сдача работы (FR-38…FR-43, BR-13…BR-16, AC-07…AC-10)
# --------------------------------------------------------------------------


@bp.post("/courses/<int:course_id>/assignments/<int:assignment_id>/submit")
@access.login_required
def assignment_submit(course_id: int, assignment_id: int):
    """Записать новую попытку (FR-38, AC-07).

    Ответ — переход назад на карточку: и при успехе, и при отказе
    студент остаётся на том же задании и видит итог и причину. Текст
    комментария при отказе сохранить нельзя (он не пишется в базу),
    поэтому форма показывается заново пустой — сообщение объясняет, что
    именно пошло не так.
    """
    user = _student()
    course = _course(course_id)
    assignment = _assignment(course, assignment_id)
    target = url_for(
        "student.assignment_card",
        course_id=course_id,
        assignment_id=assignment_id,
    )

    items: list[dict] = []
    try:
        items = _collect_uploads()
        submissions_service.create(
            assignment=assignment,
            actor=user,
            comment=request.form.get("comment"),
            files=items,
        )
    except ServiceError as failure:
        _discard(items)
        flash(str(failure), "error")
        return redirect(target)

    attempts = submissions_service.attempt_count(
        assignment["id"], user["id"]
    )
    flash(
        "Работа отправлена на проверку (попытка {0} из {1}).".format(
            attempts, config.SUBMISSION_ATTEMPT_LIMIT
        ),
        "info",
    )
    return redirect(target)


# --------------------------------------------------------------------------
# Комментарий студента к своей работе (FR-50)
# --------------------------------------------------------------------------


@bp.post("/submissions/<int:submission_id>/comments")
@access.login_required
def submission_comment(submission_id: int):
    """Добавить сообщение в переписку по своей работе (FR-50).

    Право на работу (а значит, и на её переписку) берётся из
    ``submissions.can_open``: студенту открыта только своя, чужая
    отвечает 404 — по номеру нельзя понять, существует ли она.
    """
    user = _student()
    context = submissions_service.fetch_context(submission_id)
    if context is None or not submissions_service.can_open(user, context):
        access.abort_not_found()
    target = url_for(
        "student.assignment_card",
        course_id=context["course_id"],
        assignment_id=context["assignment_id"],
    )

    items: list[dict] = []
    try:
        items = _collect_uploads()
        with db.transaction() as conn:
            comments_service.add(
                submission_id=submission_id,
                author=user,
                body=request.form.get("body"),
                files=items,
                connection=conn,
            )
    except ServiceError as failure:
        _discard(items)
        flash(str(failure), "error")
        return redirect(target)

    flash("Комментарий добавлен.", "info")
    return redirect(target)
