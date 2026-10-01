"""Предпросмотр и запись импорта списка (FR-25…FR-30, AC-23, AC-26, AC-38).

Модуль держит четыре операции: ``preview`` (разбор файла и вердикт по
каждой строке), ``confirm`` (запись принятых строк), ``discard`` (отмена
предпросмотра) и ``history`` (список прошлых импортов курса). Оценка
строк живёт в ``roster_validation`` и вызывается отсюда дважды — при
предпросмотре и при подтверждении.

**Почему файл кладётся на диск, а не в сессию.** Сессия — это подписанная
cookie, и её содержимое читает браузер (Э-2, AC-02). В реестре пароли
лежат открытым текстом, а строк может быть до ``IMPORT_ROSTER_MAX_ROWS``.
Поэтому файл сохраняется под сгенерированным именем, а в сессии лежит
только это имя (NFR-06, §7.14).

**Почему при подтверждении файл перечитывается.** Пока администратор
смотрит предпросмотр, e-mail могли занять вручную или другим импортом.
Повторная оценка перед записью гарантирует, что в базу не попадёт уже
неактуальная строка, а отчёт совпадёт с тем, что действительно записано
(AC-26, BR-06). Сессия при этом не хранит вердикты: их могло бы быть
тысячи, и cookie такой размер не уносит.

**Что пишется в базу до подтверждения.** Ничего (FR-25, чек-лист §6.1):
предпросмотр не создаёт ни строк реестра, ни отчёта, ни учётных записей, ни
записи в журнале. Код ``import.previewed`` из справочника §7.12 поэтому не
используется — решение заказчика от 29.09.2026 в пользу строгого прочтения
FR-25. Сама запись появится, когда §7.12 или §9 дополнят описанием того, что
журнал должен видеть и предпросмотр.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from flask import session

from app import config, db
from app.services import ServiceError, roster_validation

#: Расширения файлов импорта (FR-24). Это не весь
#: ``ALLOWED_FILE_EXTENSIONS``: тот список относится к материалам
#: заданий и работам (FR-51…FR-54).
ACCEPTED_EXTENSIONS = frozenset({".xlsx", ".csv"})

#: Префикс сгенерированных имён файлов импорта. По нему ``_sweep``
#: понимает, какие файлы можно удалять, не затрагивая выгрузки и
#: материалы заданий.
IMPORT_PREFIX = "import-"

#: Подкаталог для файлов импорта: рядом с остальными вложениями, но
#: отдельно от них.
UPLOAD_SUBDIR = "import"

#: Ключи сессии. В них лежит только сгенерированное имя файла и его
#: оригинал для отчёта — никаких паролей (см. модульную строку).
SESSION_KEY = "import_file"
SESSION_ORIGINAL_KEY = "import_original_name"

#: После какого возраста неоткрытый предпросмотр считается брошенным.
#: Ровно столько же живёт сессия (``PERMANENT_SESSION_LIFETIME``).
STALE_AFTER_SECONDS = config.PERMANENT_SESSION_LIFETIME


def uploads_dir() -> Path:
    return Path(config.UPLOADS_DIR) / UPLOAD_SUBDIR


# --------------------------------------------------------------------------
# Хранение загруженного файла
# --------------------------------------------------------------------------


def _sweep() -> None:
    """Удалить файлы импорта, к которым никто не вернулся.

    Предпросмотр без подтверждения оставляет файл на диске. Удалять
    его при следующей загрузке безусловно нельзя — открытый
    предпросмотр перестанет подтверждаться, — поэтому смотрим на возраст
    файла (больше lifetime сессии) и только на наш префикс.
    """
    directory = uploads_dir()
    if not directory.is_dir():
        return
    limit = time.time() - STALE_AFTER_SECONDS
    for path in directory.glob(IMPORT_PREFIX + "*"):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            # Файл занят или уже удалён — не повод срывать импорт.
            continue


def _stored_name(original_name: str) -> str:
    """Сгенерированное имя файла вместо присланного (NFR-06, §7.14).

    Присланное имя в пути не участвует: в нём может быть «../»,
    кириллица и длина больше предела в схеме. Оригинал хранится
    рядом — в сессии и в отчёте, а не в имени файла.
    """
    return "{prefix}{token}{suffix}".format(
        prefix=IMPORT_PREFIX,
        token=secrets.token_hex(16),
        suffix=Path(original_name).suffix.lower()[:10],
    )


def _original_name(raw: str) -> str:
    """Имя файла из multipart-заголовка: только имя, без пути."""
    cleaned = (raw or "").replace("\\", "/").strip()
    return Path(cleaned).name[:255] or "файл"


def _store(upload) -> tuple[Path, str]:
    """Сохранить загруженный файл, проверив имя и размер.

    Содержимое проверяет функция чтения реестра: она отвергает файл,
    который не является ни таблицей ``.xlsx``, ни текстом ``.csv``, и
    возвращает понятный ``roster_unreadable`` вместо ``BadZipFile``.
    """
    original = _original_name(getattr(upload, "filename", ""))
    if Path(original).suffix.lower() not in ACCEPTED_EXTENSIONS:
        raise ServiceError(config.MESSAGES["roster_extension"])

    directory = uploads_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / _stored_name(original)
    upload.save(str(target))
    size = target.stat().st_size
    if size == 0:
        target.unlink(missing_ok=True)
        raise ServiceError(config.MESSAGES["no_roster_rows"])
    if size > config.MAX_FILE_SIZE_BYTES:
        target.unlink(missing_ok=True)
        raise ServiceError(config.MESSAGES["file_too_large"])
    return target, original


def _pending() -> Path | None:
    """Файл, ожидающий подтверждения, — или ``None``."""
    stored = session.get(SESSION_KEY)
    if not stored:
        return None
    path = uploads_dir() / stored
    if not path.is_file():
        # Сессия жива, а файла нет: предпросмотр больше не подтвердить.
        session.pop(SESSION_KEY, None)
        session.pop(SESSION_ORIGINAL_KEY, None)
        return None
    return path


# --------------------------------------------------------------------------
# Оценка
# --------------------------------------------------------------------------


def summarise(verdicts: list[dict[str, Any]]) -> dict[str, int]:
    """Счётчики строк для предпросмотра и для отчёта (FR-26, FR-27).

    ``invalid`` и ``errors`` считают разное, и это не случайно: в
    сообщении §9.3 стоит «Строк с ошибками: {ошибок}», то есть там
    именно строки (AC-25: строки 5 и 6, а не четыре причины на две
    строки). Число причин нужно там, где показывается их перечень.
    """
    invalid = sum(1 for verdict in verdicts if not verdict["accepted"])
    return {
        "total": len(verdicts),
        "valid": len(verdicts) - invalid,
        "invalid": invalid,
        "errors": sum(len(verdict["errors"]) for verdict in verdicts),
    }


def _evaluate(path: Path, *, course_id: int) -> list[dict[str, Any]]:
    """Вердикты по всем строкам файла при текущем состоянии курса."""
    roster = roster_validation.read_roster(path, file_name=path.name)
    return roster_validation.qualify(roster, course_id=course_id)


# --------------------------------------------------------------------------
# Операции
# --------------------------------------------------------------------------


def preview(*, course_id: int, upload) -> dict[str, Any]:
    """Разобрать файл и вернуть вердикт по каждой строке (FR-26).

    Файл сохраняется под сгенерированным именем, оригинал кладётся в
    сессию. В базу не пишется ничего: ни строки реестра, ни отчёта, ни
    записи в журнале (FR-25, чек-лист §6.1). Поэтому и ``actor`` здесь
    не нужен: предпросмотр ничего не делает с курсом.
    """
    discard()
    _sweep()

    path, original = _store(upload)
    try:
        verdicts = _evaluate(path, course_id=course_id)
    except Exception:
        # Файл непригоден целиком: предпросмотра не будет, незачем
        # оставлять файл на диске.
        path.unlink(missing_ok=True)
        raise

    session[SESSION_KEY] = path.name
    session[SESSION_ORIGINAL_KEY] = original
    return {
        "verdicts": verdicts,
        "counts": summarise(verdicts),
        "file_name": original,
    }


def confirm(*, course_id: int, actor: sqlite3.Row) -> dict[str, Any]:
    """Записать принятые строки и сохранить отчёт (FR-28, FR-29).

    Оценка повторяется здесь, а не берётся из сессии: за время
    предпросмотра база могла измениться (FR-30, BR-06).
    """
    path = _pending()
    if path is None:
        raise ServiceError(config.MESSAGES["import_session_expired"])

    verdicts = _evaluate(path, course_id=course_id)
    original = session.get(SESSION_ORIGINAL_KEY) or path.name
    counts = summarise(verdicts)
    try:
        batch_id = _write(
            verdicts=verdicts,
            counts=counts,
            course_id=course_id,
            actor=actor,
            file_name=original,
        )
    finally:
        # Файл больше не нужен при любом исходе: при откате транзакции
        # повторное подтверждение всё равно не имело бы смысла — причину
        # отказа покажут те же строки заново.
        path.unlink(missing_ok=True)
        discard()
    return {"counts": counts, "batch_id": batch_id, "file_name": original}


def discard() -> None:
    """Убрать файл, ожидающий подтверждения, и забыть его."""
    stored = session.get(SESSION_KEY)
    if stored:
        (uploads_dir() / stored).unlink(missing_ok=True)
    session.pop(SESSION_KEY, None)
    session.pop(SESSION_ORIGINAL_KEY, None)


def history(*, course_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Последние импорты курса (AC-38, FR-29)."""
    rows = db.get_db().execute(
        """
        SELECT id, course_id, file_name, total_rows, valid_rows, created_at
          FROM import_batches
         WHERE course_id = ?
         ORDER BY id DESC
         LIMIT ?
        """,
        (course_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def batch_errors(batch_id: int) -> list[dict[str, Any]]:
    """Ошибки одного импорта по строкам файла (FR-27)."""
    rows = db.get_db().execute(
        """
        SELECT row_number, field, error_text
          FROM import_errors
         WHERE batch_id = ?
         ORDER BY row_number, id
        """,
        (batch_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Запись
# --------------------------------------------------------------------------


def _write(
    *,
    verdicts: list[dict[str, Any]],
    counts: dict[str, int],
    course_id: int,
    actor: sqlite3.Row,
    file_name: str,
) -> int:
    """Записать учётные записи, отчёт и журнал одной транзакцией.

    Отчёт ``import_batches`` пишется всегда, даже если ни одна строка не
    прошла: иначе повторный импорт того же файла (AC-27) не оставил бы
    после себя след, и администратор не увидел бы, почему всё отклонено.
    """
    from app import action_log

    with db.transaction() as connection:
        if (
            connection.execute(
                "SELECT 1 FROM courses WHERE id = ?", (course_id,)
            ).fetchone()
            is None
        ):
            raise ServiceError(config.MESSAGES["not_found"])

        groups_cache: dict[str, int] = {}
        for verdict in verdicts:
            if not verdict["accepted"]:
                continue
            _create_account(
                verdict=verdict,
                course_id=course_id,
                actor=actor,
                groups_cache=groups_cache,
            )

        batch_id = _record_batch(
            connection=connection,
            course_id=course_id,
            actor=actor,
            file_name=file_name,
            verdicts=verdicts,
            counts=counts,
        )
        action_log.log_action(
            "import.committed",
            actor_id=actor["id"],
            actor_role=actor["role"],
            entity_type="import",
            entity_id=batch_id,
            course_id=course_id,
            details={
                "file_name": file_name,
                "total_rows": counts["total"],
                "valid_rows": counts["valid"],
                "error_count": counts["errors"],
            },
            connection=connection,
        )
    return batch_id


def _create_account(
    *,
    verdict: dict[str, Any],
    course_id: int,
    actor: sqlite3.Row,
    groups_cache: dict[str, int],
) -> None:
    """Создать учётную запись строки и включить её в её группу.

    Строка уже оценена ``roster_validation``, поэтому повторная проверка
    формы — не лишняя работа, а страховка: тот же ``ServiceError``
    откатит всю транзакцию, а не оставит импорт наполовину.

    Группа из файла создаётся, если её ещё нет, и переиспользуется, если
    уже есть (AC-24: группа ``ФК11`` не должна задваиваться на третьей
    строке того же файла). Обе операции пишут журнал через
    ``groups.create_group`` и ``members.create_member`` — отдельных
    записей об импорте для них не нужно.
    """
    from app.services import groups, members

    values = verdict["values"]
    role = roster_validation.role_code(values["role"])
    if role is None:
        # Недостижимо: строка без корректной роли не доходит до записи.
        raise ServiceError(
            roster_validation.BR_MESSAGES["BR-02"].format(value=values["role"])
        )

    name = values["group"]
    group_id = groups_cache.get(name)
    if group_id is None:
        existing = groups.find_by_name(course_id, name)
        if existing is not None:
            group_id = existing["id"]
        else:
            group_id = groups.create_group(
                course_id=course_id, name=name, actor=actor
            )
        groups_cache[name] = group_id

    members.create_member(
        course_id=course_id,
        group_id=group_id,
        full_name=values["full_name"],
        role=role,
        email=values["email"],
        password=values["password"],
        actor=actor,
        added_via="import",
    )


def _record_batch(
    *,
    connection: sqlite3.Connection,
    course_id: int,
    actor: sqlite3.Row,
    file_name: str,
    verdicts: list[dict[str, Any]],
    counts: dict[str, int],
) -> int:
    from app import action_log

    cursor = connection.execute(
        """
        INSERT INTO import_batches (
            actor_id, course_id, file_name, total_rows, valid_rows, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            actor["id"],
            course_id,
            file_name[:255],
            counts["total"],
            counts["valid"],
            action_log.utc_now_iso(),
        ),
    )
    batch_id = cursor.lastrowid
    for verdict in verdicts:
        for error in verdict["errors"]:
            connection.execute(
                """
                INSERT INTO import_errors (batch_id, row_number, field, error_text)
                VALUES (?, ?, ?, ?)
                """,
                (batch_id, verdict["row_number"], error["field"], error["text"]),
            )
    return batch_id
