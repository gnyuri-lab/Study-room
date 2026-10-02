"""Экспорт базы в JSON и восстановление из резервной копии (FR-68…FR-71).

Файл копии — один объект JSON в кодировке UTF-8 с четырьмя ключами:

``schemaVersion``  версия схемы, под которой сделан экспорт. Она равна
                   ``PRAGMA user_version`` и сверяется с текущей базой
                   при восстановлении (FR-70): чужая версия — отказ, а
                   не попытка угадать совместимость;
``exportedAt``     время экспорта в ISO 8601 (UTC);
``tables``         все четырнадцать таблиц целиком, включая журнал
                   и отчёты импорта; колонки названы как в схеме;
``files``          содержимое файлов вложений: ``stored_name`` → base64.

**Почему base64, а не отдельные файлы.** Копия обязана быть одним
переносимым файлом (FR-68): вложения лежат на диске под сгенерированными
именами и не образуют ни структуры каталогов, ни порядка следования.
Base64 даёт байты в JSON без потерь, а проверка раскодирования входит в
валидацию копии (FR-70).

**Почему пароли не вырезаются.** В ``users`` лежат ``password_enc``
(Fernet) и ``password_hash`` (pbkdf2) — открытого пароля в них нет, а
вход после восстановления обязан работать. Ключ шифрования
(``var/secret.key``) в копию не входит (NFR-08): с другим ключом
шифртекст не раскрывается, и выдача пароля администратором гаснет
штатной ошибкой, а не ломает базу.

**Почему восстановление заменяет всё.** Требование FR-71 — полная
замена текущих данных. Строки удаляются в обратном порядке зависимостей
и вставляются в прямом, идентификаторы сохраняются: по ним ссылаются
журнал и отчёты импорта. Порядок операций держит обещание «данные не
изменены» при любом отказе: файлы копии сначала раскладываются во
временный каталог, затем в одной транзакции меняется база, проверяется
``foreign_key_check`` и уже внутри транзакции подменяются файлы; отказ
на любом шаге откатывает и базу, и файлы.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from flask import session

from app import action_log, attachments, config, db
from app.services import ServiceError

#: Ключи верхнего уровня файла копии.
SCHEMA_VERSION_KEY = "schemaVersion"
EXPORTED_AT_KEY = "exportedAt"
TABLES_KEY = "tables"
FILES_KEY = "files"

#: Ключи сессии: под каким именем лежит загруженный файл и как его
#: прислал администратор (для предпросмотра). Только сгенерированное
#: имя — никаких данных копии, они слишком велики для cookie (NFR-06).
SESSION_KEY = "backup_file"
SESSION_NAME_KEY = "backup_original_name"

#: Возраст, после которого незавершённая загрузка считается брошенной.
#: Ровно столько же живёт сессия (``PERMANENT_SESSION_LIFETIME``).
STALE_AFTER_SECONDS = config.PERMANENT_SESSION_LIFETIME

#: Размер блока копирования загружаемого файла: лимит считается по ходу,
#: поэтому файл больше предела не доходит до конца и не остаётся.
CHUNK_SIZE = 64 * 1024

#: Таблицы в порядке зависимостей: от родителей к потомкам. Строки
#: копии пишутся в этом порядке, очистка идёт в обратном — так проходят
#: внешние ключи. Состав закреплён FR-71: копия забирает все таблицы.
TABLES: tuple[str, ...] = (
    "courses",
    "users",
    "groups",
    "group_members",
    "curator_groups",
    "assignments",
    "assignment_files",
    "submissions",
    "submission_files",
    "submission_comments",
    "submission_comment_files",
    "action_log",
    "import_batches",
    "import_errors",
)

#: Таблицы, чьи строки ссылаются на файлы на диске (``stored_name``).
FILE_TABLES: tuple[str, ...] = (
    "assignment_files",
    "submission_files",
    "submission_comment_files",
)

#: Подписи таблиц для предпросмотра (NFR-13): экран показывает
#: по-русски, в файле остаются технические имена.
TABLE_LABELS: dict[str, str] = {
    "courses": "Курсы",
    "users": "Учётные записи",
    "groups": "Группы",
    "group_members": "Участники групп",
    "curator_groups": "Кураторы групп",
    "assignments": "Задания",
    "assignment_files": "Материалы заданий",
    "submissions": "Сдачи",
    "submission_files": "Файлы сдач",
    "submission_comments": "Переписка по работам",
    "submission_comment_files": "Файлы комментариев",
    "action_log": "Журнал действий",
    "import_batches": "Отчёты импорта",
    "import_errors": "Ошибки импорта",
}

#: Служебные каталоги внутри ``UPLOADS_DIR``: копия кладёт новые файлы
#: рядом с текущими и меняет их местами в конце транзакции. Оба —
#: каталоги, поэтому перечень файлов при экспорте их не видит.
STAGED_PREFIX = ".restore-staged-"
OLD_PREFIX = ".restore-old-"


# --------------------------------------------------------------------------
# Каталоги
# --------------------------------------------------------------------------


def staging_dir() -> Path:
    """Каталог незавершённых загрузок копии.

    Лежит в ``VAR_DIR``, а не в ``UPLOADS_DIR``: это состояние сессии,
    а не данные приложения, и каталог не должен попадать ни в экспорт,
    ни в подмену файлов при восстановлении.
    """
    return Path(config.VAR_DIR) / config.BACKUP_STAGING_SUBDIR


def uploads_dir() -> Path:
    return Path(config.UPLOADS_DIR)


# --------------------------------------------------------------------------
# Чтение схемы
# --------------------------------------------------------------------------


def _table_columns(connection: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Колонки таблицы в порядке схемы: ``(имя, объявленный тип)``.

    Тип нужен обеим сторонам: при экспорте из него видно, что колонка
    ``BLOB`` (base64), при восстановлении — что байты пора вернуть из
    base64. Таблица берётся только из ``TABLES``, поэтому подстановки
    в ``PRAGMA`` не бывает.
    """
    rows = connection.execute(
        "PRAGMA table_info({0})".format(table)
    ).fetchall()
    if not rows:
        raise RuntimeError("Таблица {0} отсутствует в схеме.".format(table))
    return [(row["name"], (row["type"] or "").upper()) for row in rows]


def _is_stored_name_safe(name: Any) -> bool:
    """Допустимо ли имя файла для каталога вложений (FR-54, NFR-06).

    Повторяет CHECK из схемы: без разделителей пути и без ведущей точки.
    Копия могла быть собрана руками, поэтому здесь это проверяется
    заново — файлы раскладываются по именам из JSON.
    """
    return (
        isinstance(name, str)
        and bool(name)
        and "/" not in name
        and "\\" not in name
        and not name.startswith(".")
    )


# --------------------------------------------------------------------------
# Экспорт (FR-68)
# --------------------------------------------------------------------------


def export_payload(*, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Собрать содержимое базы и файлов вложений в словарь копии.

    Файл, пропавший с диска, — отказ, а не пустая строка: тихо
    неполная копия хуже отсутствующей (FR-68).
    """
    if connection is None:
        connection = db.get_db()

    columns = {table: _table_columns(connection, table) for table in TABLES}
    tables: dict[str, list[dict[str, Any]]] = {}
    stored_names: set[str] = set()

    for table in TABLES:
        names = [name for name, _ in columns[table]]
        rows: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM {0}".format(table)
        ):
            item: dict[str, Any] = {}
            for name in names:
                value = row[name]
                if isinstance(value, (bytes, bytearray, memoryview)):
                    # Единственный BLOB в схеме — password_enc; общий
                    # приём на случай новых двоичных колонок.
                    value = base64.b64encode(bytes(value)).decode("ascii")
                item[name] = value
            if table in FILE_TABLES:
                stored_names.add(item["stored_name"])
            rows.append(item)
        tables[table] = rows

    files: dict[str, str] = {}
    for name in sorted(stored_names):
        path = attachments.stored_path(name)
        if not path.is_file():
            raise ServiceError(config.MESSAGES["file_missing_on_disk"])
        files[name] = base64.b64encode(path.read_bytes()).decode("ascii")

    return {
        SCHEMA_VERSION_KEY: db.applied_version(connection),
        EXPORTED_AT_KEY: action_log.utc_now_iso(),
        TABLES_KEY: tables,
        FILES_KEY: files,
    }


def summarise(payload: dict[str, Any]) -> dict[str, Any]:
    """Счётчики копии для предпросмотра и для журнала."""
    counts = {
        table: len(payload[TABLES_KEY][table]) for table in TABLES
    }
    return {
        "counts": counts,
        "records": sum(counts.values()),
        "files": len(payload[FILES_KEY]),
        "exported_at": payload[EXPORTED_AT_KEY],
        "schema_version": payload[SCHEMA_VERSION_KEY],
    }


def serialize(payload: dict[str, Any]) -> bytes:
    """Словарь копии → байты JSON (UTF-8, без ASCII-экранирования).

    Размер сверяется с пределом загрузки: экспорт, который невозможно
    восстановить обратно, бесполезен.
    """
    data = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(data) > config.BACKUP_MAX_BYTES:
        raise ServiceError(
            config.MESSAGES["backup_too_large"].format(limit=_limit_mb())
        )
    return data


def _limit_mb() -> int:
    return config.BACKUP_MAX_BYTES // (1024 * 1024)


# --------------------------------------------------------------------------
# Валидация (FR-70)
# --------------------------------------------------------------------------


def _invalid() -> ServiceError:
    return ServiceError(config.MESSAGES["backup_invalid"])


def load_payload(path: Path) -> dict[str, Any]:
    """Прочитать файл копии как JSON в UTF-8 (FR-70)."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise _invalid()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise _invalid()
    if not isinstance(data, dict):
        raise _invalid()
    return data


def _validate_value(value: Any, declared: str) -> None:
    """Тип одного значения колонки (FR-70).

    ``bool`` отдельно: в Python это подкласс ``int``, а в схеме логических
    колонок нет — ``true`` в JSON должен быть отвергнут, а не принят
    как 1.
    """
    if value is None:
        return
    if declared == "BLOB":
        if not isinstance(value, str):
            raise _invalid()
        _decode_base64(value)
        return
    if declared == "INTEGER":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _invalid()
        return
    if declared == "TEXT":
        if not isinstance(value, str):
            raise _invalid()
        return
    # Неизвестный объявленный тип: принимаем только базовые JSON-типы.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _invalid()


def _decode_base64(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid()


def validate(payload: Any) -> dict[str, Any]:
    """Проверить структуру копии и версию схемы (FR-70).

    Проверка идёт от общего к частому: объект → ключи → версия →
    полнота таблиц и колонок → типы значений → файлы. Сначала сверяется
    версия, чтобы копия другой версии получила свой текст отказа, а не
    общий «повреждена». Возвращает счётчики для предпросмотра.
    """
    if not isinstance(payload, dict):
        raise _invalid()
    for key in (SCHEMA_VERSION_KEY, EXPORTED_AT_KEY, TABLES_KEY, FILES_KEY):
        if key not in payload:
            raise _invalid()

    version = payload[SCHEMA_VERSION_KEY]
    if isinstance(version, bool) or not isinstance(version, int):
        raise _invalid()
    if not isinstance(payload[EXPORTED_AT_KEY], str):
        raise _invalid()

    tables = payload[TABLES_KEY]
    files = payload[FILES_KEY]
    if not isinstance(tables, dict) or not isinstance(files, dict):
        raise _invalid()

    connection = db.get_db()
    current = db.applied_version(connection)
    if version != current:
        raise ServiceError(
            config.MESSAGES["backup_version"].format(
                file=version, current=current
            )
        )

    if set(tables) != set(TABLES):
        raise _invalid()

    for table in TABLES:
        columns = _table_columns(connection, table)
        expected = {name for name, _ in columns}
        rows = tables[table]
        if not isinstance(rows, list):
            raise _invalid()
        for row in rows:
            if not isinstance(row, dict) or set(row) != expected:
                raise _invalid()
            for name, declared in columns:
                _validate_value(row[name], declared)

    for name, encoded in files.items():
        if not _is_stored_name_safe(name) or not isinstance(encoded, str):
            raise _invalid()
        _decode_base64(encoded)

    referenced: set[str] = set()
    for table in FILE_TABLES:
        for row in tables[table]:
            referenced.add(row["stored_name"])
    if referenced != set(files):
        raise _invalid()

    return summarise(payload)


# --------------------------------------------------------------------------
# Загрузка файла (FR-70)
# --------------------------------------------------------------------------


def _sweep() -> None:
    """Убрать незавершённые загрузки, к которым никто не вернулся."""
    directory = staging_dir()
    if not directory.is_dir():
        return
    limit = time.time() - STALE_AFTER_SECONDS
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            # Файл занят или уже удалён — не повод срывать загрузку.
            continue


def _copy_with_limit(stream, target: Path) -> int:
    """Скопировать поток в файл, остановившись на превышении предела.

    Размер считается по ходу: файл больше ``BACKUP_MAX_BYTES`` не
    доходит до конца и не остаётся на диске.
    """
    written = 0
    with target.open("wb") as handle:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > config.BACKUP_MAX_BYTES:
                handle.close()
                target.unlink(missing_ok=True)
                raise ServiceError(
                    config.MESSAGES["backup_too_large"].format(
                        limit=_limit_mb()
                    )
                )
            handle.write(chunk)
    return written


def stage(upload) -> dict[str, Any]:
    """Проверить загруженный файл и положить на подтверждение (FR-70).

    Валидация идёт до сессии: непригодный файл удаляется и не ждёт
    подтверждения. Строки базы при этом не читаются — предпросмотр
    показывает счётчики из самой копии.
    """
    discard()
    _sweep()

    raw_name = (getattr(upload, "filename", "") or "").replace("\\", "/")
    original = Path(raw_name).name[:255].strip()
    if not original:
        raise ServiceError(config.MESSAGES["backup_no_file"])

    directory = staging_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "backup-{0}.json".format(secrets.token_hex(16))

    stream = getattr(upload, "stream", None) or upload
    try:
        _copy_with_limit(stream, target)
        payload = load_payload(target)
        summary = validate(payload)
    except BaseException:
        # Файл непригоден целиком: подтверждать нечего.
        target.unlink(missing_ok=True)
        raise

    session[SESSION_KEY] = target.name
    session[SESSION_NAME_KEY] = original
    summary["file_name"] = original
    return summary


def _pending() -> Path | None:
    """Файл, ожидающий подтверждения, — или ``None``."""
    stored = session.get(SESSION_KEY)
    if not stored:
        return None
    path = staging_dir() / stored
    if not path.is_file():
        # Сессия жива, а файла нет: восстановить из него нельзя.
        session.pop(SESSION_KEY, None)
        session.pop(SESSION_NAME_KEY, None)
        return None
    return path


def discard() -> None:
    """Убрать файл, ожидающий подтверждения, и забыть его."""
    stored = session.get(SESSION_KEY)
    if stored:
        (staging_dir() / stored).unlink(missing_ok=True)
    session.pop(SESSION_KEY, None)
    session.pop(SESSION_NAME_KEY, None)


# --------------------------------------------------------------------------
# Восстановление (FR-71)
# --------------------------------------------------------------------------


def _replace_tables(
    connection: sqlite3.Connection, tables: dict[str, Any]
) -> None:
    """Полностью заменить строки базы строками копии.

    Идентификаторы не пересоздаются: они приходят из копии и на них
    ссылаются журнал, отчёты импорта и вложения между собой.
    """
    for table in reversed(TABLES):
        connection.execute("DELETE FROM {0}".format(table))
    for table in TABLES:
        columns = _table_columns(connection, table)
        names = [name for name, _ in columns]
        sql = "INSERT INTO {0} ({1}) VALUES ({2})".format(
            table,
            ", ".join(names),
            ", ".join("?" for _ in names),
        )
        for row in tables[table]:
            values = []
            for name, declared in columns:
                value = row[name]
                if declared == "BLOB" and value is not None:
                    value = _decode_base64(value)
                values.append(value)
            connection.execute(sql, values)


def _check_foreign_keys(connection: sqlite3.Connection) -> None:
    """Последняя страховка перед фиксацией: целостность связей (FR-71)."""
    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise ServiceError(config.MESSAGES["backup_failed"])


def _write_staged(files: dict[str, str], staged: Path) -> None:
    """Раскодировать файлы копии во временный каталог."""
    staged.mkdir(parents=True, exist_ok=True)
    for name, encoded in files.items():
        (staged / name).write_bytes(_decode_base64(encoded))


def _remove_tree(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _install_files(staged: Path) -> None:
    """Поставить файлы копии в каталог вложений, заменив текущие.

    Вызывается внутри транзакции: при отказе старые файлы возвращаются
    на место, транзакция откатывается, и обещание «данные не изменены»
    остаётся верным. Подкаталоги ``staging/`` и ``import/`` не трогаются:
    заменяются только файлы в корне, на которые ссылается база.
    """
    root = uploads_dir()
    root.mkdir(parents=True, exist_ok=True)
    old_dir = root / (OLD_PREFIX + secrets.token_hex(8))
    moved_old: list[str] = []
    moved_new: list[str] = []
    try:
        old_dir.mkdir()
        for path in sorted(root.iterdir()):
            if path.is_file():
                path.replace(old_dir / path.name)
                moved_old.append(path.name)
        for path in sorted(staged.iterdir()):
            path.replace(root / path.name)
            moved_new.append(path.name)
    except OSError:
        # Возврат старых файлов — best effort: дальше всё равно откат
        # транзакции, а база источник истины для пересчёта счётчиков.
        for name in moved_new:
            try:
                (root / name).unlink(missing_ok=True)
            except OSError:
                pass
        for name in moved_old:
            try:
                (old_dir / name).replace(root / name)
            except OSError:
                pass
        _remove_tree(old_dir)
        raise ServiceError(config.MESSAGES["backup_failed"])
    _remove_tree(old_dir)


def restore(*, actor) -> dict[str, Any]:
    """Полностью заменить базу и файлы данными копии (FR-71).

    ``actor`` — запись администратора: её id уходит в журнал внутри
    же транзакции, поэтому успешное восстановление и его след в журнале
    фиксируются вместе. После успеха сессию чистит вызывающая сторона:
    текущая учётная запись могла исчезнуть из копии.
    """
    path = _pending()
    if path is None:
        raise ServiceError(config.MESSAGES["backup_session_expired"])
    original = session.get(SESSION_NAME_KEY) or path.name

    payload = load_payload(path)
    summary = validate(payload)

    connection = db.get_db()
    staged = uploads_dir() / (STAGED_PREFIX + secrets.token_hex(8))
    try:
        try:
            _write_staged(payload[FILES_KEY], staged)
        except OSError:
            raise ServiceError(config.MESSAGES["backup_failed"])

        try:
            with db.transaction():
                _replace_tables(connection, payload[TABLES_KEY])
                _check_foreign_keys(connection)
                action_log.log_action(
                    "backup.restored",
                    actor_id=actor["id"],
                    actor_role=actor["role"],
                    entity_type="backup",
                    details={
                        "records": summary["records"],
                        "files": summary["files"],
                        "file_name": original,
                    },
                    connection=connection,
                )
                _install_files(staged)
        except ServiceError:
            raise
        except sqlite3.Error:
            # Нарушение CHECK или ключа: откат уже сделан, база
            # осталась прежней (FR-71).
            raise ServiceError(config.MESSAGES["backup_failed"])
    finally:
        _remove_tree(staged)
        path.unlink(missing_ok=True)
        session.pop(SESSION_KEY, None)
        session.pop(SESSION_NAME_KEY, None)

    return summary
