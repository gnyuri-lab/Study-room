"""Статусы работы: автостатус, переходы и отзыв проверки
(FR-45, FR-47, FR-48, BR-17…BR-21, AC-11…AC-15).

Три правила, которые нельзя отдать на откуп форме.

**«Сдано после дедлайна» вычисляет система.** У куратора есть кнопка
«сдано», но нет кнопки «сдано после дедлайна» (FR-47): выбранное
«сдано» превращается в ``accepted_late``, если момент сдачи позже
дедлайна (BR-18), и остаётся ``accepted`` в остальных случаях (BR-17,
BR-19). Без дедлайна ``accepted_late`` недостижим вовсе.

**Куратор меняет только работу «на проверке»** (BR-20): из
``revision`` и из терминальных статусов переход запрещён, и текст
отказа §9.3 различает эти два случая.

**Терминальный статус меняет только администратор** и только в
``revision`` (BR-21, сценарий А-2). Это отдельный маршрут с отдельным
кодом журнала ``submission.review_reverted``, а не общий «сменить
статус»: иначе в журнале нельзя было бы отличить проверку от отзыва.

Сравнение времени идёт по строкам в едином формате UTC, который задан
в ``assignments.STORED_DATETIME_FORMAT`` (допущение Д-09).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app import access, action_log, config, db
from app.services import ServiceError
from app.services import assignments as assignments_service
from app.services import comments as comments_service
from app.services import submissions as submissions_service

#: Что куратор может выбрать на форме проверки (FR-45).
CURATOR_CHOICES = ("accepted", "revision")

#: Терминальные статусы — их меняет только администратор (BR-21).
TERMINAL = config.TERMINAL_STATUSES


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def _moment(stored: str | None) -> datetime | None:
    return assignments_service.parse_moment(stored)


def resolve_accepted(*, submitted_at: str, due_at: str | None) -> str:
    """Каким станет статус «сдано» для этой попытки (BR-17…BR-19).

    Дедлайна нет — ``accepted`` (BR-19). Есть — сравнение моментов:
    позже дедлайна значит ``accepted_late`` (BR-18), иначе ``accepted``
    (BR-17). Неразобранное время приравнивается к «дедлайн не задан»:
    отказывать студенту из-за битой даты нельзя, а ``accepted_late``
    тем более — это статус, который система ставит сама.
    """
    due = _moment(due_at)
    if due is None:
        return "accepted"
    moment = _moment(submitted_at)
    if moment is None:
        return "accepted"
    return "accepted_late" if moment > due else "accepted"


def _load_for_review(submission_id: int, actor: sqlite3.Row) -> dict[str, Any]:
    """Работа, которую actor имеет право рассматривать, иначе 404.

    Отказ не по роли (403), а по существованию (404): куратор чужой
    группы не должен по коду ответа узнать, есть ли такая работа
    (FR-08, §9.3).
    """
    context = submissions_service.fetch_context(submission_id)
    if context is None:
        access.abort_not_found()
    if not submissions_service.can_review(actor, context):
        access.abort_not_found()
    return context


# --------------------------------------------------------------------------
# Проверка работы (FR-45, BR-20)
# --------------------------------------------------------------------------


def change_status(
    *,
    submission_id: int,
    requested: str,
    actor: sqlite3.Row,
    comment: str | None,
    files: list[dict[str, Any]],
) -> str:
    """Поставить статус проверки и, если есть, комментарий.

    Возвращает фактически сохранённый статус: для «сдано» это может
    оказаться ``accepted_late`` (FR-47).
    """
    context = _load_for_review(submission_id, actor)
    if requested not in CURATOR_CHOICES:
        raise ServiceError(config.MESSAGES["status_value_invalid"])

    current = context["status"]
    if is_terminal(current):
        # AC-14: куратору показывается сообщение, а не молчаливый отказ.
        raise ServiceError(config.MESSAGES["status_terminal"])
    if current != "submitted":
        raise ServiceError(
            config.MESSAGES["status_not_on_review"].format(
                status=config.STATUSES[current]["label"]
            )
        )

    clean_comment = (comment or "").strip()
    if requested == "revision" and not clean_comment:
        raise ServiceError(config.MESSAGES["status_comment_required"])
    comments_service.check_files(files)

    final = (
        resolve_accepted(
            submitted_at=context["submitted_at"], due_at=context["due_at"]
        )
        if requested == "accepted"
        else "revision"
    )

    with db.transaction() as connection:
        connection.execute(
            "UPDATE submissions SET status = ? WHERE id = ?",
            (final, submission_id),
        )
        comments_service.add(
            submission_id=submission_id,
            author=actor,
            body=clean_comment,
            files=files,
            connection=connection,
            required=False,
        )
        action_log.log_action(
            "submission.status_changed",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="submission",
            entity_id=submission_id,
            course_id=context["course_id"],
            assignment_id=context["assignment_id"],
            from_status=current,
            to_status=final,
            details={
                "attempt_no": context["attempt_no"],
                "assignment_title": context["assignment_title"],
                "student": context["student_name"],
            },
            connection=connection,
        )
    return final


# --------------------------------------------------------------------------
# Отзыв проверки (FR-48, BR-21, AC-15, сценарий А-2)
# --------------------------------------------------------------------------


def revert_review(
    *,
    submission_id: int,
    actor: sqlite3.Row,
    comment: str | None,
    files: list[dict[str, Any]],
) -> str:
    """Вернуть принятую работу в «на доработку» (только администратор)."""
    context = _load_for_review(submission_id, actor)
    if actor["role"] != access.ADMIN_ROLE:
        access.abort_forbidden()
    if not is_terminal(context["status"]):
        raise ServiceError(config.MESSAGES["review_revert_only_terminal"])
    comments_service.check_files(files)

    with db.transaction() as connection:
        connection.execute(
            "UPDATE submissions SET status = ? WHERE id = ?",
            ("revision", submission_id),
        )
        comments_service.add(
            submission_id=submission_id,
            author=actor,
            body=(comment or "").strip(),
            files=files,
            connection=connection,
            required=False,
        )
        action_log.log_action(
            "submission.review_reverted",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="submission",
            entity_id=submission_id,
            course_id=context["course_id"],
            assignment_id=context["assignment_id"],
            from_status=context["status"],
            to_status="revision",
            details={
                "attempt_no": context["attempt_no"],
                "assignment_title": context["assignment_title"],
                "student": context["student_name"],
            },
            connection=connection,
        )
    return "revision"
