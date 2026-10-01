"""Сдача работ: попытки, лимит, файлы и доступ (FR-38…FR-44, BR-13…BR-16).

Модуль держит четыре решения этапа.

**Одна строка — одна попытка.** ``attempt_no`` нумеруется от единицы и
уникален в паре ``(assignment_id, student_id)`` (§7.9), поэтому история
попыток — это просто строки, отсортированные по номеру. Текущий статус —
статус последней попытки: отдельного «рабочего» поля в схеме нет, а
«не сдано» вообще не хранится, это отсутствие строк (Д-10).

**Лимит проверяется раньше статуса.** BR-16 запрещает сдачу при
исчерпанном лимите «независимо от статуса», а AC-10 ждёт именно текст
о лимите, когда после третьей попытки работа стоит «на проверке».
Поэтому сначала считается число попыток и только потом читается статус:
иначе AC-08 и AC-10 дали бы один ответ на разные ситуации.

**Право на работу — не то же самое, что право на курс.** Студент одного
курса видит материалы заданий, но не работы однокурсников (FR-49):
для вложений требуется либо быть автором, либо быть прикреплённым
куратором группы автора, либо быть администратором (BR-29).

**Файлы коммитятся внутри транзакции.** Как и в
``services/assignments.py``: откат базы убирает строки, а убрать файлы
с диска должен тот, кто их закоммитил.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import access, action_log, attachments, config, db
from app.services import ServiceError
from app.services import assignments as assignments_service

#: Статусы, из которых сдача невозможна (FR-42, BR-15).
ACCEPTED_STATUSES = ("accepted", "accepted_late")

#: Статус, из которого пересдача закрыта до обратной связи (FR-41, BR-14).
ON_CHECK_STATUSES = ("submitted",)


def _connection() -> sqlite3.Connection:
    return db.get_db()


def format_moment(stored: str | None) -> str:
    """Время хранения → текст на экране (тот же формат, что у дедлайна)."""
    return assignments_service.format_due(stored)


# --------------------------------------------------------------------------
# Чтение
# --------------------------------------------------------------------------


def fetch(submission_id: int) -> sqlite3.Row | None:
    return _connection().execute(
        "SELECT * FROM submissions WHERE id = ?", (submission_id,)
    ).fetchone()


def fetch_context(submission_id: int) -> dict[str, Any] | None:
    """Попытка вместе с заданием, курсом и автором.

    Один запрос вместо трёх: карточка работы показывает всё сразу, а
    правило доступа считается по ``course_id`` и ``student_id`` — ими
    же пользуется скачивание файлов.
    """
    row = _connection().execute(
        """
        SELECT s.id, s.assignment_id, s.student_id, s.attempt_no, s.status,
               s.comment, s.submitted_at, s.created_at,
               a.course_id, a.title AS assignment_title, a.due_at,
               a.archived_at, c.title AS course_title,
               u.full_name AS student_name, u.role AS student_role
          FROM submissions s
          JOIN assignments a ON a.id = s.assignment_id
          JOIN courses c ON c.id = a.course_id
          JOIN users u ON u.id = s.student_id
         WHERE s.id = ?
        """,
        (submission_id,),
    ).fetchone()
    if row is None:
        return None
    payload = dict(row)
    payload["submitted_label"] = format_moment(payload["submitted_at"])
    payload["due_label"] = assignments_service.format_due(payload["due_at"])
    payload["status_label"] = config.STATUSES[payload["status"]]["label"]
    return payload


def attempt_count(assignment_id: int, student_id: int) -> int:
    return _connection().execute(
        "SELECT COUNT(*) FROM submissions"
        " WHERE assignment_id = ? AND student_id = ?",
        (assignment_id, student_id),
    ).fetchone()[0]


def latest(assignment_id: int, student_id: int) -> sqlite3.Row | None:
    """Последняя попытка либо ``None``, если сдач ещё не было."""
    return _connection().execute(
        """
        SELECT * FROM submissions
         WHERE assignment_id = ? AND student_id = ?
         ORDER BY attempt_no DESC LIMIT 1
        """,
        (assignment_id, student_id),
    ).fetchone()


def current_status(assignment_id: int, student_id: int) -> str:
    """Статус последней попытки или ``not_submitted`` (Д-10)."""
    row = latest(assignment_id, student_id)
    return row["status"] if row is not None else "not_submitted"


def files_of(submission_id: int, *, kind: str = "submission") -> list[dict]:
    """Файлы попытки в порядке загрузки (FR-38, FR-46).

    ``kind`` выбирает таблицу: сама работа лежит в ``submission_files``,
    вложение к комментарию — в ``submission_comment_files``; адрес
    скачивания у них разный, поэтому и подпись у каждого своя.
    """
    table = (
        "submission_files" if kind == "submission" else "submission_comment_files"
    )
    rows = _connection().execute(
        """
        SELECT id, original_name, mime, size_bytes, created_at
          FROM {table}
         WHERE submission_id = ?
         ORDER BY id
        """.format(table=table),
        (submission_id,),
    ).fetchall()
    payload = [dict(row) for row in rows]
    for item in payload:
        item["size_label"] = attachments.human_size(item["size_bytes"])
        item["download_url"] = attachments.download_url(item["id"], kind=kind)
    return payload


def attempts(assignment_id: int, student_id: int) -> list[dict[str, Any]]:
    """История попыток для карточки задания (FR-44).

    Номер, время, статус, комментарий студента и файлы каждой попытки.
    Порядок — по номеру попытки, то есть хронологический.
    """
    rows = _connection().execute(
        """
        SELECT id, attempt_no, status, comment, submitted_at, created_at
          FROM submissions
         WHERE assignment_id = ? AND student_id = ?
         ORDER BY attempt_no
        """,
        (assignment_id, student_id),
    ).fetchall()
    payload = [dict(row) for row in rows]
    for item in payload:
        item["submitted_label"] = format_moment(item["submitted_at"])
        item["status_label"] = config.STATUSES[item["status"]]["label"]
        item["files"] = files_of(item["id"])
    return payload


# --------------------------------------------------------------------------
# Квалидация (FR-38, FR-43, FR-52)
# --------------------------------------------------------------------------


def validate_comment(body: str | None) -> str:
    """Комментарий сдачи: обязателен и не длиннее 4000 символов (§7.9)."""
    candidate = (body or "").strip()
    if not candidate:
        raise ServiceError(config.MESSAGES["submission_comment_required"])
    if len(candidate) > config.COMMENT_MAX_LENGTH:
        raise ServiceError(config.MESSAGES["submission_comment_too_long"])
    return candidate


def check_files(files: list[dict[str, Any]]) -> None:
    """Число файлов одной сдачи не больше 5 (FR-52, §9.3)."""
    if len(files) > config.MAX_FILES_PER_SUBMISSION:
        raise ServiceError(config.MESSAGES["files_limit_submission"])


def assert_submittable(assignment: sqlite3.Row, student_id: int) -> str:
    """Проверить, что сдача разрешена (BR-13…BR-16). Возвращает статус.

    Порядок проверок — не деталь реализации, а условие критериев:
    лимит (BR-16) читается первым независимо от статуса, затем
    «на проверке» (BR-14), затем терминальные статусы (BR-15).
    """
    used = attempt_count(assignment["id"], student_id)
    limit = config.SUBMISSION_ATTEMPT_LIMIT
    if used >= limit:
        raise ServiceError(
            config.MESSAGES["submission_limit_reached"].format(
                used=used, limit=limit
            )
        )
    status = current_status(assignment["id"], student_id)
    if status in ON_CHECK_STATUSES:
        raise ServiceError(config.MESSAGES["submission_on_check"])
    if status in ACCEPTED_STATUSES:
        raise ServiceError(config.MESSAGES["submission_accepted"])
    return status


# --------------------------------------------------------------------------
# Создание попытки (FR-38, FR-39, AC-07)
# --------------------------------------------------------------------------


def _insert_files(
    *,
    connection: sqlite3.Connection,
    submission_id: int,
    actor: sqlite3.Row,
    files: list[dict[str, Any]],
) -> list[str]:
    """Перенести файлы попытки из проверки в хранение и записать строки."""
    committed: list[str] = []
    try:
        for item in files:
            stored_name = attachments.commit_staged(item["staged_name"])
            committed.append(stored_name)
            connection.execute(
                """
                INSERT INTO submission_files (
                    submission_id, original_name, stored_name, mime,
                    size_bytes, uploaded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    item["original_name"],
                    stored_name,
                    assignments_service.mime_for_extension(item["extension"]),
                    item["size_bytes"],
                    actor["id"],
                    action_log.utc_now_iso(),
                ),
            )
    except BaseException:
        for stored_name in committed:
            attachments.delete_stored(stored_name)
        raise
    return committed


def create(
    *,
    assignment: sqlite3.Row,
    actor: sqlite3.Row,
    comment: str | None,
    files: list[dict[str, Any]],
) -> int:
    """Записать новую попытку (FR-38, FR-39, BR-13, AC-07).

    Номер попытки берётся из числа уже существующих строк плюс единица —
    так он не может разойтись с реальным числом работ, даже если две
    отправки придут одновременно: уникальность ``(assignment_id,
    student_id, attempt_no)`` в схеме не даст создать дубликат.
    """
    if assignment["archived_at"] is not None:
        access.abort_not_found()
    clean_comment = validate_comment(comment)
    check_files(files)
    assert_submittable(assignment, actor["id"])

    attempt_no = attempt_count(assignment["id"], actor["id"]) + 1
    if attempt_no > config.SUBMISSION_ATTEMPT_LIMIT:
        raise ServiceError(
            config.MESSAGES["submission_limit_reached"].format(
                used=config.SUBMISSION_ATTEMPT_LIMIT,
                limit=config.SUBMISSION_ATTEMPT_LIMIT,
            )
        )

    now = action_log.utc_now_iso()
    committed: list[str] = []
    with db.transaction() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO submissions (
                    assignment_id, student_id, attempt_no, status, comment,
                    submitted_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment["id"],
                    actor["id"],
                    attempt_no,
                    "submitted",
                    clean_comment,
                    now,
                    now,
                ),
            )
            submission_id = cursor.lastrowid
            committed = _insert_files(
                connection=connection,
                submission_id=submission_id,
                actor=actor,
                files=files,
            )
            action_log.log_action(
                "submission.created",
                actor_id=actor["id"],
                actor_role=actor["role"],
                entity_type="submission",
                entity_id=submission_id,
                course_id=assignment["course_id"],
                assignment_id=assignment["id"],
                to_status="submitted",
                details={
                    "attempt_no": attempt_no,
                    "files": len(files),
                    "assignment_title": assignment["title"],
                },
                connection=connection,
            )
        except BaseException:
            for stored_name in committed:
                attachments.delete_stored(stored_name)
            raise
    return submission_id


# --------------------------------------------------------------------------
# Доступ к работе и её файлам (FR-49, FR-53, BR-28, BR-29)
# --------------------------------------------------------------------------


def shares_group(curator: sqlite3.Row, student_id: int) -> bool:
    """Прикреплён ли куратор к группе, где состоит студент (BR-29).

    Куратор, заведённый на двух курсах, считается по всем его записям —
    так же, как это делает ``access.visible_group_ids``.
    """
    ids = access.identity_ids(curator)
    if not ids:
        return False
    marks = ",".join("?" for _ in ids)
    row = _connection().execute(
        """
        SELECT 1 FROM group_members gm
          JOIN curator_groups cg ON cg.group_id = gm.group_id
         WHERE gm.user_id = ? AND cg.user_id IN ({0}) LIMIT 1
        """.format(marks),
        [student_id, *ids],
    ).fetchone()
    return row is not None


def can_open(viewer: sqlite3.Row | None, context: dict[str, Any]) -> bool:
    """Есть ли у пользователя право открыть работу и её файлы (FR-49, FR-53).

    Студент — только свои работы; администратор — все; куратор — работы
    студентов своих групп. Чужой курс закрыт независимо от роли
    (BR-28, BR-29).
    """
    if viewer is None or not access.can_view_course(context["course_id"]):
        return False
    role = viewer["role"]
    if role == access.ADMIN_ROLE:
        return True
    if role == access.STUDENT_ROLE:
        return int(viewer["id"]) == int(context["student_id"])
    if role == access.CURATOR_ROLE:
        return shares_group(viewer, context["student_id"])
    return False


def can_review(viewer: sqlite3.Row | None, context: dict[str, Any]) -> bool:
    """Можно ли менять статус работы (FR-45, FR-48).

    Отличие от ``can_open``: студент, открывший свою работу, права на
    смену статуса не получает, а у куратора требуется прикрепление к
    группе автора.
    """
    if viewer is None:
        return False
    if viewer["role"] not in (access.ADMIN_ROLE, access.CURATOR_ROLE):
        return False
    return can_open(viewer, context)
