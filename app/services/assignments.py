"""Задания: чтение, валидация, предпросмотр формы, правка и удаление
(FR-32…FR-37, BR-23, BR-24, AC-05, AC-06, AC-29, AC-40).

Пять решений, которые модуль фиксирует намеренно.

**Дедлайн.** §7.7 требует ISO 8601, а В-02 оставляет зону открытым и
предлагает по умолчанию локальную зону сервера. Здесь принимается именно
этот вариант: поле формы — ``datetime-local``, то есть время без зоны;
оно читается как местное время сервера, переводится в UTC и хранится с
суффиксом ``Z``, как и остальные даты приложения (``action_log``).
Показывается обратно в местной зоне. Сравнение сдачи с дедлайном на
Э-6 therefore идёт в одной зоне и пересчёта зон не потребует.

**Описание без ограничения длины.** §7.7 для ``description`` верхней
границы не задаёт, ограничения в схеме нет. Константа
``ASSIGNMENT_DESCRIPTION_MAX_LENGTH`` этому противоречит и, как и
``COURSE_DESCRIPTION_MAX_LENGTH`` на Э-3, здесь не применяется.

**Предпросмотр ничего не пишет.** Форма собирается в черновик на диске
(FR-34), и до подтверждения не появляется ни строки в ``assignments``,
ни строки в ``assignment_files``, ни записи в журнале. Повторная
проверка при подтверждении — не перестраховка, а необходимость: между
предпросмотром и подтверждением администратор мог заполнить пять файлов
на другое задание или вовсе удалить его.

**Куда девается черновик.** Он лежит в подкаталоге ``staging`` рядом с
невостребованными материалами и убирается тем же сборщиком мусора, что
и они. В сессии лежит только имя файла черновика: описание задания
может быть длиннее предела cookie, а подписанная cookie читается
браузером.

**Удаление задания без сдач не имеет кода в справочнике §7.12.** FR-62
требует записать удаление задания в журнал, но §7.12 содержит только
``assignment.archived``. Здесь используется приём, применённый на Э-3
для физического удаления группы: записывается ``assignment.updated`` с
``details.deleted = true``, а при архивировании — ``assignment.archived``.
Новое значение в справочник не добавлено молча; вопрос вынесен в отчёт
этапа.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import session

from app import access, action_log, attachments, config, db
from app.services import ServiceError

#: Ключ сессии: имя файла черновика. Ничего больше там не лежит.
DRAFT_SESSION_KEY = "assignment_draft"

#: Префикс имени файла черновика. Начинается с ``staged-``, поэтому за
#: него отвечает тот же сборщик ``attachments._sweep``.
DRAFT_PREFIX = "staged-form-"

#: Формат хранения дат в приложении — тот же, что у ``action_log``.
STORED_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Что показывается в поле ``datetime-local``.
INPUT_DATETIME_FORMAT = "%Y-%m-%dT%H:%M"

#: Форматы, которые приходят из поля ввода. Браузер отдаёт минуты,
#: но вставка текста руками может дать и секунды.
INPUT_ACCEPTED = (
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)

#: Формат даты на экране: «01.10.2026 23:59».
DISPLAY_DATETIME_FORMAT = "%d.%m.%Y %H:%M"


def _connection() -> sqlite3.Connection:
    return db.get_db()


# --------------------------------------------------------------------------
# Дедлайн
# --------------------------------------------------------------------------


def parse_due(raw: str | None) -> str | None:
    """Поле формы → время хранения. Пустое поле — дедлайна нет (FR-32).

    Время без зоны читается как местное время сервера и переводится в
    UTC: так его можно сравнивать с ``submitted_at``, который тоже
    пишется в UTC, не пересчитывая зоны (допущение Д-09, В-02).
    """
    candidate = (raw or "").strip()
    if not candidate:
        return None
    for pattern in INPUT_ACCEPTED:
        try:
            parsed = datetime.strptime(candidate, pattern)
        except ValueError:
            continue
        return parsed.astimezone(timezone.utc).strftime(STORED_DATETIME_FORMAT)
    raise ServiceError(config.MESSAGES["assignment_due_invalid"])


def to_input_value(stored: str | None) -> str:
    """Время хранения → значение поля ``datetime-local``."""
    if not stored:
        return ""
    parsed = _parse_stored(stored)
    if parsed is None:
        return ""
    return parsed.astimezone().strftime(INPUT_DATETIME_FORMAT)


def format_due(stored: str | None) -> str:
    """Время хранения → текст на экране. Пустая строка — дедлайна нет."""
    if not stored:
        return ""
    parsed = _parse_stored(stored)
    if parsed is None:
        return ""
    return parsed.astimezone().strftime(DISPLAY_DATETIME_FORMAT)


def _parse_stored(stored: str) -> datetime | None:
    try:
        parsed = datetime.strptime(stored, STORED_DATETIME_FORMAT)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def parse_moment(stored: str | None) -> datetime | None:
    """Разобрать сохранённое время; ``None`` для пустой или битой строки.

    Публичная обёртка над ``_parse_stored``: сравнение сдачи с дедлайном
    на Э-6 работает с теми же строками, что и показ даты, и не должно
    знать о внутреннем формате модуля.
    """
    if not stored:
        return None
    return _parse_stored(stored)


# --------------------------------------------------------------------------
# Чтение
# --------------------------------------------------------------------------


def fetch(assignment_id: int) -> sqlite3.Row | None:
    return _connection().execute(
        "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
    ).fetchone()


def fetch_with_course(assignment_id: int) -> sqlite3.Row | None:
    """Задание вместе с названием курса."""
    return _connection().execute(
        """
        SELECT a.*, c.title AS course_title
          FROM assignments a
          JOIN courses c ON c.id = a.course_id
         WHERE a.id = ?
        """,
        (assignment_id,),
    ).fetchone()


def files_count(assignment_id: int) -> int:
    return _connection().execute(
        "SELECT COUNT(*) FROM assignment_files WHERE assignment_id = ?",
        (assignment_id,),
    ).fetchone()[0]


def submissions_count(assignment_id: int) -> int:
    """Число попыток сдачи — им решается судьба удаления (BR-23)."""
    return _connection().execute(
        "SELECT COUNT(*) FROM submissions WHERE assignment_id = ?",
        (assignment_id,),
    ).fetchone()[0]


def list_for_course(
    course_id: int, *, include_archived: bool = True
) -> list[dict[str, Any]]:
    """Задания курса для администратора.

    Архивные показываются отдельным блоком и подписаны: удалённое
    задание со сдачами обязано оставаться видимым администратору
    (AC-29, чек-лист §12). Порядок вывода — ``order_index``, а не
    дата создания: §4.2 запрещает отдельные средства сортировки, но
    заданный при создании порядок показывать можно.
    """
    condition = "" if include_archived else " AND a.archived_at IS NULL"
    rows = _connection().execute(
        """
        SELECT a.id, a.title, a.description, a.due_at, a.order_index,
               a.archived_at, a.created_at,
               (SELECT COUNT(*) FROM assignment_files f
                 WHERE f.assignment_id = a.id) AS files_count,
               (SELECT COUNT(*) FROM submissions s
                 WHERE s.assignment_id = a.id) AS submissions_count
          FROM assignments a
         WHERE a.course_id = ?{condition}
         ORDER BY a.order_index, a.id
        """.format(condition=condition),
        (course_id,),
    ).fetchall()
    payload = [dict(row) for row in rows]
    for item in payload:
        item["due_label"] = format_due(item["due_at"])
        item["is_archived"] = item["archived_at"] is not None
    return payload


def list_for_student(course_id: int, student_id: int) -> list[dict[str, Any]]:
    """Задания курса для студента: описание, дедлайн и статус работы.

    Статус берётся из последней попытки сдачи; попыток нет — значит
    ``not_submitted`` (Д-10: «не сдано» не хранится отдельной строкой).
    Архивные задания студенту не показываются (FR-23, чек-лист §12).
    """
    rows = _connection().execute(
        """
        SELECT a.id, a.title, a.description, a.due_at, a.order_index,
               (SELECT s.status FROM submissions s
                 WHERE s.assignment_id = a.id AND s.student_id = ?
                 ORDER BY s.attempt_no DESC LIMIT 1) AS status
          FROM assignments a
         WHERE a.course_id = ? AND a.archived_at IS NULL
         ORDER BY a.order_index, a.id
        """,
        (student_id, course_id),
    ).fetchall()
    payload = [dict(row) for row in rows]
    for item in payload:
        item["status"] = item["status"] or "not_submitted"
        item["due_label"] = format_due(item["due_at"])
    return payload


def student_status(assignment_id: int, student_id: int) -> str:
    """Статус последней попытки сдачи или ``not_submitted``.

    Служебная функция чтения для FR-37. Присвоение статусов на Э-6
    заведёт ``app/services/statuses.py``; здесь нужен только показ, и
    дублировать правила переходов значило бы развести их надвое.
    """
    row = _connection().execute(
        """
        SELECT status FROM submissions
         WHERE assignment_id = ? AND student_id = ?
         ORDER BY attempt_no DESC LIMIT 1
        """,
        (assignment_id, student_id),
    ).fetchone()
    return row["status"] if row is not None else "not_submitted"


# --------------------------------------------------------------------------
# Квалификация
# --------------------------------------------------------------------------


def validate_title(title: str) -> str:
    """Название задания: не пусто и не длиннее 200 символов (§7.7)."""
    candidate = (title or "").strip()
    if not candidate:
        raise ServiceError(config.MESSAGES["assignment_title_required"])
    if len(candidate) > config.ASSIGNMENT_TITLE_MAX_LENGTH:
        raise ServiceError(config.MESSAGES["assignment_title_too_long"])
    return candidate


def validate_description(description: str | None) -> str | None:
    """Описание необязательно; пустая строка хранится как NULL."""
    if description is None:
        return None
    candidate = description.strip()
    return candidate or None


# --------------------------------------------------------------------------
# Черновик формы (FR-34)
# --------------------------------------------------------------------------


def _draft_path(name: str) -> Path:
    return attachments.staging_path(name)


def stage_draft(
    *,
    course_id: int,
    title: str,
    description: str | None,
    due_at: str | None,
    files: list[dict[str, Any]],
    mode: str,
    assignment_id: int | None = None,
    existing_files: int = 0,
) -> dict[str, Any]:
    """Собрать черновик и показать его как предпросмотр (FR-34, AC-40).

    Проверки выполняются здесь же, а не только при подтверждении.
    Иначе администратор увидел бы предпросмотр задания без названия или
    с шестью файлами и узнал бы об отказе уже на шаге, где отмена
    стоит отдельного действия. §9.1 требует показать превышение лимита
    на том экране, где его вызвали.

    Прежний черновик и его файлы удаляются: предпросмотр у формы один,
    и оставить на диске файлы отменённого варианта незачем. В базу при
    этом не пишется ничего.
    """
    validate_title(title)
    parse_due(due_at)
    attachments.check_count(existing_files, len(files))
    discard_draft()
    payload = {
        "mode": mode,
        "assignment_id": assignment_id,
        "course_id": course_id,
        "title": title,
        "description": description,
        "due_at": due_at,
        "files": [
            {
                "staged_name": item["staged_name"],
                "original_name": item["original_name"],
                "extension": item["extension"],
                "size_bytes": item["size_bytes"],
            }
            for item in files
        ],
    }
    name = DRAFT_PREFIX + secrets.token_hex(16) + ".json"
    directory = attachments.staging_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _draft_path(name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    session[DRAFT_SESSION_KEY] = name
    return preview_of(payload)


def read_draft() -> dict[str, Any] | None:
    """Черновик из сессии либо ``None``.

    Если файл не нашёлся, сессия очищается: подтверждать больше нечего,
    и форма должна показаться заново.
    """
    name = session.get(DRAFT_SESSION_KEY)
    if not name:
        return None
    path = _draft_path(name)
    if not path.is_file():
        session.pop(DRAFT_SESSION_KEY, None)
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        session.pop(DRAFT_SESSION_KEY, None)
        return None
    payload["_staged_names"] = [
        item["staged_name"] for item in payload.get("files", [])
    ]
    return payload


def draft_for_view() -> dict[str, Any] | None:
    """Черновик в виде, пригодном для показа в шаблоне."""
    payload = read_draft()
    if payload is None:
        return None
    return preview_of(payload)


def preview_of(payload: dict[str, Any]) -> dict[str, Any]:
    """Представление черновика: то, что показывает предпросмотр."""
    files = []
    for item in payload.get("files", []):
        entry = dict(item)
        entry["size_label"] = attachments.human_size(entry["size_bytes"])
        files.append(entry)
    view = dict(payload)
    view["files"] = files
    # В черновике дедлайн лежит так, как его ввели в форму, а
    # ``format_due`` ждёт уже сохранённый UTC. Показывать надо то же
    # самое, что и после сохранения, поэтому строка приводится к
    # сохранённому виду, а не наоборот: иначе предпросмотр показал бы
    # пустой дедлайн у задания, который после подтверждения окажется
    # с датой.
    raw_due = payload.get("due_at")
    if raw_due:
        try:
            view["due_label"] = format_due(parse_due(raw_due))
        except ServiceError:
            view["due_label"] = ""
    else:
        view["due_label"] = ""
    return view


def discard_draft() -> None:
    """Убрать черновик и его файлы, не записав их в базу."""
    name = session.get(DRAFT_SESSION_KEY)
    session.pop(DRAFT_SESSION_KEY, None)
    if not name:
        return
    try:
        payload = json.loads(_draft_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Файл черновика битый или уже удалён: очищаем только его, а
        # файлы материалов уберёт сборщик мусора по возрасту.
        attachments.staging_path(name).unlink(missing_ok=True)
        return
    attachments.discard_all(
        item["staged_name"] for item in payload.get("files", [])
    )
    _draft_path(name).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Создание и правка
# --------------------------------------------------------------------------


def _next_order_index(connection: sqlite3.Connection, course_id: int) -> int:
    """Следующий порядок вывода в курсе.

    Архивные задания учитываются: иначе после архивирования первого
    задания нумерация второго поехала бы вниз и список поехал бы вместе
    с ней. Первое задание получает 1.
    """
    current = connection.execute(
        "SELECT COALESCE(MAX(order_index), 0) FROM assignments WHERE course_id = ?",
        (course_id,),
    ).fetchone()[0]
    return int(current) + 1


def _insert_files(
    *,
    connection: sqlite3.Connection,
    assignment_id: int,
    course_id: int,
    actor: sqlite3.Row,
    files: list[dict[str, Any]],
) -> list[str]:
    """Перенести файлы из проверки в хранение и записать их строки.

    Возвращает имена перемещённых файлов: откат транзакции убирает
    строки из базы, а убрать с диска должен вызывающий — он знает, что
    откат случился уже после возврата (например, при сбое записи в
    журнал). Ошибка внутри самого переноса убирает файлы здесь же.
    """
    committed: list[str] = []
    try:
        for item in files:
            stored_name = attachments.commit_staged(item["staged_name"])
            committed.append(stored_name)
            connection.execute(
                """
                INSERT INTO assignment_files (
                    assignment_id, original_name, stored_name, mime,
                    size_bytes, uploaded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment_id,
                    item["original_name"],
                    stored_name,
                    _mime_of(item["extension"]),
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


def _drop_committed(stored_names: list[str]) -> None:
    """Убрать с диска файлы, чьи строки не удержались в базе."""
    for stored_name in stored_names:
        attachments.delete_stored(stored_name)


#: Расширение → MIME для колонки ``mime``. Значение не участвует в
#: проверке доступа и в выдаче: браузер получает файл как вложение.
MIME_BY_EXTENSION = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "txt": "text/plain",
    "md": "text/markdown",
    "png": "image/png",
    "jpg": "image/jpeg",
    "zip": "application/zip",
}


def _mime_of(extension: str) -> str | None:
    return MIME_BY_EXTENSION.get(extension)


def mime_for_extension(extension: str) -> str | None:
    """Расширение → MIME для колонки ``mime``.

    Публичная обёртка над тем же словарём: файлы сдачи (Э-6) записывают
    тип содержимого так же, как материалы задания, и держать второй
    словарь значило бы развести их надвое.
    """
    return MIME_BY_EXTENSION.get(extension)


def create_assignment(
    *,
    course_id: int,
    title: str,
    description: str | None,
    due_at: str | None,
    actor: sqlite3.Row,
    files: list[dict[str, Any]],
) -> int:
    """Создать задание с материалами (FR-32, FR-33, FR-35, AC-40)."""
    clean_title = validate_title(title)
    clean_description = validate_description(description)
    clean_due = parse_due(due_at)
    attachments.check_count(0, len(files))

    from app.services import courses

    if courses.fetch_course(course_id) is None:
        access.abort_not_found()

    committed: list[str] = []
    with db.transaction() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO assignments (
                    course_id, title, description, due_at, order_index, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    course_id,
                    clean_title,
                    clean_description,
                    clean_due,
                    _next_order_index(connection, course_id),
                    action_log.utc_now_iso(),
                ),
            )
            assignment_id = cursor.lastrowid
            committed = _insert_files(
                connection=connection,
                assignment_id=assignment_id,
                course_id=course_id,
                actor=actor,
                files=files,
            )
            action_log.log_action(
                "assignment.created",
                actor_id=actor["id"],
                actor_role=actor["role"],
                entity_type="assignment",
                entity_id=assignment_id,
                course_id=course_id,
                assignment_id=assignment_id,
                details={
                    "title": clean_title,
                    "due_at": clean_due,
                    "files": len(files),
                },
                connection=connection,
            )
        except BaseException:
            # Откат уберёт строки из базы — файлы на диске тоже не
            # должны остаться, иначе они не будут ни видны, ни удалены.
            _drop_committed(committed)
            raise
    return assignment_id


def update_assignment(
    *,
    assignment_id: int,
    title: str,
    description: str | None,
    due_at: str | None,
    actor: sqlite3.Row,
    files: list[dict[str, Any]],
) -> None:
    """Сохранить правку задания и добавить новые материалы (FR-34, FR-35)."""
    assignment = fetch(assignment_id)
    if assignment is None:
        access.abort_not_found()
    clean_title = validate_title(title)
    clean_description = validate_description(description)
    clean_due = parse_due(due_at)
    attachments.check_count(files_count(assignment_id), len(files))

    committed: list[str] = []
    with db.transaction() as connection:
        try:
            connection.execute(
                """
                UPDATE assignments
                   SET title = ?, description = ?, due_at = ?
                 WHERE id = ?
                """,
                (clean_title, clean_description, clean_due, assignment_id),
            )
            committed = _insert_files(
                connection=connection,
                assignment_id=assignment_id,
                course_id=assignment["course_id"],
                actor=actor,
                files=files,
            )
            action_log.log_action(
                "assignment.updated",
                actor_id=actor["id"],
                actor_role=actor["role"],
                entity_type="assignment",
                entity_id=assignment_id,
                course_id=assignment["course_id"],
                assignment_id=assignment_id,
                details={
                    "title": clean_title,
                    "due_at": clean_due,
                    "files_added": len(files),
                },
                connection=connection,
            )
        except BaseException:
            _drop_committed(committed)
            raise


# --------------------------------------------------------------------------
# Удаление (BR-23, BR-24, AC-29)
# --------------------------------------------------------------------------


def delete_or_archive_assignment(
    *, assignment_id: int, actor: sqlite3.Row
) -> str:
    """Удалить задание или отправить его в архив. Возвращает исход слова.

    Решение принимается по числу попыток сдачи, а не по флагу в
    запросе: администратор нажимает одну кнопку «Удалить», и текст
    подтверждения объясняет, что произойдёт. Задание со сдачами
    физически не удаляется никогда (BR-23) — иначе пропали бы работы,
    комментарии и файлы сдачи.
    """
    assignment = fetch(assignment_id)
    if assignment is None:
        access.abort_not_found()

    submissions = submissions_count(assignment_id)
    archived = submissions > 0
    stored_names: list[str] = []

    with db.transaction() as connection:
        if archived:
            connection.execute(
                "UPDATE assignments SET archived_at = ? WHERE id = ?",
                (action_log.utc_now_iso(), assignment_id),
            )
            action = "assignment.archived"
        else:
            stored_names = [
                row["stored_name"]
                for row in connection.execute(
                    "SELECT stored_name FROM assignment_files"
                    " WHERE assignment_id = ?",
                    (assignment_id,),
                ).fetchall()
            ]
            connection.execute(
                "DELETE FROM assignment_files WHERE assignment_id = ?",
                (assignment_id,),
            )
            connection.execute(
                "DELETE FROM assignments WHERE id = ?", (assignment_id,)
            )
            # В §7.12 нет кода удаления задания (см. строку модуля);
            # используется приём Э-3 для физического удаления группы.
            action = "assignment.updated"
        action_log.log_action(
            action,
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="assignment",
            entity_id=assignment_id,
            course_id=assignment["course_id"],
            assignment_id=assignment_id,
            details={
                "title": assignment["title"],
                "submissions": submissions,
                "deleted": not archived,
            },
            connection=connection,
        )

    # Файлы удаляются после коммита: база была источником истины, и при
    # откате строки остались бы на месте.
    for stored_name in stored_names:
        attachments.delete_stored(stored_name)
    return "archived" if archived else "deleted"


def remove_file(*, file_id: int, assignment_id: int, actor: sqlite3.Row) -> None:
    """Открепить материал от задания (FR-35, FR-62).

    Открепление не удаляет задание и не трогает работы студентов: это
    правка состава материалов, поэтому в журнале ``assignment.updated``
    с перечислением оставшегося числа файлов.
    """
    row = attachments.fetch_assignment_file_row(file_id)
    if row is None or row["assignment_id"] != assignment_id:
        access.abort_not_found()
    assignment = fetch(assignment_id)
    if assignment is None:
        access.abort_not_found()

    remaining = files_count(assignment_id) - 1
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM assignment_files WHERE id = ?", (file_id,)
        )
        action_log.log_action(
            "file.deleted",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="file",
            entity_id=file_id,
            course_id=assignment["course_id"],
            assignment_id=assignment_id,
            details={
                "file_name": row["original_name"],
                "assignment_title": assignment["title"],
            },
            connection=connection,
        )
        action_log.log_action(
            "assignment.updated",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="assignment",
            entity_id=assignment_id,
            course_id=assignment["course_id"],
            assignment_id=assignment_id,
            details={"files": max(remaining, 0), "file_removed": True},
            connection=connection,
        )
    attachments.delete_stored(row["stored_name"])
