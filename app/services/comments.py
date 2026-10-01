"""Переписка по работе: комментарии студентов, кураторов и администратора
(FR-45, FR-46, FR-50).

Комментарий — это строка ``submission_comments``: автор, текст и время.
К нему может прилагаться от нуля до пяти файлов (§7.10, FR-46): файл
лежит в ``submission_comment_files`` и ссылается на владельца через
``comment_id``, поэтому одно сообщение собирает несколько вложений, а
лимит берётся из ``config.MAX_FILES_PER_COMMENT`` — единственный источник
правды, как и для работы. Строки комментариев не привязаны к статусу:
переписка продолжается и после принятия работы.

Комментарию не соответствует ни один код из справочника §7.12, поэтому
в журнал действий оно не пишется — писать новый код молча запрещено.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import action_log, attachments, config, db
from app.services import ServiceError
from app.services import assignments as assignments_service
from app.services import submissions as submissions_service

#: Сколько файлов можно приложить к одному комментарию (§7.10).
#: Значение живёт в ``config`` вместе с текстом отказа о превышении.
MAX_FILES_PER_COMMENT = config.MAX_FILES_PER_COMMENT


def _connection() -> sqlite3.Connection:
    return db.get_db()


def validate_body(body: str | None, *, required: bool = True) -> str:
    """Текст комментария: не пусто (если требуется) и не длиннее 4000."""
    candidate = (body or "").strip()
    if not candidate:
        if required:
            raise ServiceError(config.MESSAGES["comment_body_required"])
        return ""
    if len(candidate) > config.COMMENT_MAX_LENGTH:
        raise ServiceError(config.MESSAGES["submission_comment_too_long"])
    return candidate


def check_files(files: list[dict[str, Any]]) -> None:
    """К одному комментарию — не больше пяти файлов (§7.10, FR-46)."""
    if len(files) > MAX_FILES_PER_COMMENT:
        raise ServiceError(config.MESSAGES["files_limit_comment"])


def thread(submission_id: int) -> list[dict[str, Any]]:
    """Все комментарии одной попытки в хронологическом порядке (FR-50).

    Роль автора берётся из его учётной записи на момент чтения и
    показывается подписью рядом с именем: студент и куратор отличаются
    в переписке даже без цветовой индикации.
    """
    return thread_many([submission_id])


def thread_many(submission_ids: list[int]) -> list[dict[str, Any]]:
    """Переписка по нескольким попыткам одной работы (FR-50).

    Карточка задания показывает работу целиком, а не только последнюю
    попытку: комментарий куратора к первой сдаче остаётся частью
    переписки и после пересдачи (AC-09). Строки идут в порядке
    добавления, то есть хронологически.
    """
    ids = [int(item) for item in submission_ids]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    connection = _connection()
    rows = connection.execute(
        """
        SELECT c.id, c.body, c.created_at,
               u.full_name AS author_name, u.role AS author_role
          FROM submission_comments c
          JOIN users u ON u.id = c.author_id
         WHERE c.submission_id IN ({0})
         ORDER BY c.id
        """.format(marks),
        ids,
    ).fetchall()
    files_by_comment: dict[int, list[dict[str, Any]]] = {}
    for row in connection.execute(
        """
        SELECT f.id, f.comment_id, f.original_name, f.size_bytes
          FROM submission_comment_files f
         WHERE f.comment_id IN (
               SELECT c.id FROM submission_comments c
                WHERE c.submission_id IN ({0})
         )
         ORDER BY f.id
        """.format(marks),
        ids,
    ).fetchall():
        files_by_comment.setdefault(row["comment_id"], []).append(
            {
                "file_url": attachments.download_url(row["id"], kind="comment"),
                "file_name": row["original_name"],
                "size_label": attachments.human_size(row["size_bytes"]),
            }
        )
    payload = []
    for row in rows:
        item = dict(row)
        item["created_label"] = submissions_service.format_moment(
            item["created_at"]
        )
        item["author_role_label"] = config.ROLES[item["author_role"]]["label"]
        item["files"] = files_by_comment.get(item["id"], [])
        payload.append(item)
    return payload


def add(
    *,
    submission_id: int,
    author: sqlite3.Row,
    body: str | None,
    files: list[dict[str, Any]],
    connection: sqlite3.Connection | None = None,
    required: bool = True,
) -> int | None:
    """Записать комментарий и приложенные к нему файлы.

    Возвращает ``id`` записи либо ``None``, если писать нечего: пустой
    комментарий без вложения — это не ошибка, а отсутствие сообщения
    (так форма «сдано» позволяет принять работу без текста, чего
    ``revision`` не допускает — там ``required`` проверен раньше).

    Строка комментария вставляется первой: файлы ссылается на неё через
    ``comment_id``. Каждый файл коммитится с диска в базу по мере
    вставки и удаляется с диска, если транзакция откатилась, — тот же
    приём, что в ``services/submissions.py``.
    """
    check_files(files)
    text = validate_body(body, required=required or bool(files))
    if not text:
        return None

    if connection is None:
        connection = _connection()

    committed: list[str] = []
    try:
        cursor = connection.execute(
            """
            INSERT INTO submission_comments (
                submission_id, author_id, body, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                submission_id,
                author["id"],
                text,
                action_log.utc_now_iso(),
            ),
        )
        comment_id = cursor.lastrowid
        for item in files:
            stored_name = attachments.commit_staged(item["staged_name"])
            committed.append(stored_name)
            connection.execute(
                """
                INSERT INTO submission_comment_files (
                    submission_id, comment_id, original_name, stored_name,
                    mime, size_bytes, uploaded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    comment_id,
                    item["original_name"],
                    stored_name,
                    assignments_service.mime_for_extension(item["extension"]),
                    item["size_bytes"],
                    author["id"],
                    action_log.utc_now_iso(),
                ),
            )
        return comment_id
    except BaseException:
        for stored_name in committed:
            attachments.delete_stored(stored_name)
        raise
