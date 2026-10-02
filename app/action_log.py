"""Журнал действий (FR-12, FR-61…FR-64).

Модуль появился на этапе Э-2 вместе с входом, выходом и сменой
учётных данных, но пишет все действия приложения: журнал наполняется
из любого раздела, а не только из аутентификации.

Два правила, зашитые в модуль:

* ``action`` и ``entity_type`` берутся только из справочника §7.12.
  Новое значение нельзя записать молча — ``ValueError`` заставит
  сначала дописать ТЗ.
* ``details_json`` не должен содержать пароли (FR-64, О-11). Ключи,
  похожие на пароль, отвергаются: это страховка от того, что значение
  случайно попадёт в ``details`` и будет записано в открытом виде.

IP-адреса здесь не хранятся и не собираются: колонок для них в схеме
нет, и добавлять их запрещено (FR-64, раздел 4.2 п. 11).

С Этапа Э-8 модуль содержит и чтение журнала (FR-61, FR-63): выборка
для экрана администратора и для выгрузки CSV. Читает журнал только
``blueprints/admin_audit.py`` — фильтры, подписи и формат выгрузки
держатся здесь, чтобы экран и файл не могли разойтись.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from app import config

#: Ключи, которые нельзя складывать в ``details_json``.
FORBIDDEN_DETAIL_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "пароль",
    "pwd",
    "secret",
    "ключ",
)


class UnsafeDetailsError(ValueError):
    """В ``details`` попал ключ, похожий на пароль или секрет."""


class UnknownJournalValueError(ValueError):
    """Значение не входит в справочник §7.12."""


def utc_now_iso() -> str:
    """Текущее время в ISO 8601 с суффиксом ``Z``.

    Используется ``timezone.utc``, а не ``zoneinfo``: на Windows для
    ``zoneinfo`` нужен пакет ``tzdata``, которого нет в §12.1. Сравнение
    времени сдачи с дедлайном идёт в одной зоне (допущение Д-09),
    поэтому пересчёт зон на этом этапе не нужен.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _walk_keys(value: Any, path: str = "details") -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from _walk_keys(nested, "{}.{}".format(path, key))
            lowered = str(key).lower()
            if any(marker in lowered for marker in FORBIDDEN_DETAIL_MARKERS):
                yield "{}.{}".format(path, key)
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            yield from _walk_keys(nested, "{}[{}]".format(path, index))


def assert_details_safe(details: dict[str, Any] | None) -> None:
    """Отвергнуть ``details`` с ключами, похожими на пароль.

    Проверяются ключи во всех вложенных объектах и списках. Значения не
    просматриваются: запрещён именно ключ, который означает пароль.
    """
    if not details:
        return
    for path in _walk_keys(details):
        raise UnsafeDetailsError(
            "Ключ {path} нельзя писать в журнал: в details_json не "
            "допускаются пароли и секреты (FR-64).".format(path=path)
        )


def _assert_known(value: str, allowed: Iterable[str], field: str) -> None:
    if value not in set(allowed):
        raise UnknownJournalValueError(
            "Значение {value!r} поля {field} отсутствует в справочнике "
            "§7.12. Сначала допишите его в ТЗ.".format(value=value, field=field)
        )


def log_action(
    action: str,
    *,
    actor_id: int | None,
    actor_role: str,
    entity_type: str,
    entity_id: int | None = None,
    course_id: int | None = None,
    group_id: int | None = None,
    assignment_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    details: dict[str, Any] | None = None,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Записать действие в ``action_log`` и вернуть ``id`` записи.

    Запись делается в уже открытой транзакции, если она есть: успех
    действия и его журнал фиксируются вместе. Отдельного коммита здесь
    нет намеренно.
    """
    _assert_known(action, config.ACTION_CODES, "action")
    _assert_known(entity_type, config.ENTITY_TYPES, "entity_type")
    _assert_known(actor_role, config.ROLE_ORDER, "actor_role")
    for field, value in (("from_status", from_status), ("to_status", to_status)):
        if value is not None:
            _assert_known(value, config.STATUS_ORDER, field)
    assert_details_safe(details)

    if connection is None:
        from app import db

        connection = db.get_db()

    payload = None
    if details:
        payload = json.dumps(details, ensure_ascii=False, sort_keys=True)

    cursor = connection.execute(
        """
        INSERT INTO action_log (
            actor_id, actor_role, action, entity_type, entity_id,
            course_id, group_id, assignment_id,
            from_status, to_status, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_id,
            actor_role,
            action,
            entity_type,
            entity_id,
            course_id,
            group_id,
            assignment_id,
            from_status,
            to_status,
            payload,
            utc_now_iso(),
        ),
    )
    return cursor.lastrowid


def log_authentication(
    action: str,
    user,
    *,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Записать действие аутентификации от имени пользователя.

    Курс в запись не попадает: при входе он ещё может быть не выбран,
    а при выходе неважен. ``entity_id`` — сама учётная запись.
    """
    return log_action(
        action,
        actor_id=user["id"],
        actor_role=user["role"],
        entity_type="user",
        entity_id=user["id"],
        connection=connection,
    )


# --------------------------------------------------------------------------
# Чтение журнала (FR-61, FR-63, этап Э-8)
# --------------------------------------------------------------------------

#: Формат времени в ``created_at`` — тот же, что пишет ``utc_now_iso``.
STORED_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Текст времени на экране и в выгрузке: «01.10.2026 14:05».
#: Перевод в местную зону — как у дедлайнов (``format_due``): журнал
#: и остальные экраны показывают одно и то же wall-clock время.
DISPLAY_DATETIME_FORMAT = "%d.%m.%Y %H:%M"

#: Ключи фильтров экрана (FR-63): дата, роль, курс, тип объекта.
FILTER_KEYS = ("date", "role", "course", "entity")

#: Подписи действий справочника §7.12 для экрана и выгрузки.
#: Ключи обязаны совпадать с ``config.ACTION_CODES``: тест это
#: проверяет, чтобы новое значение справочника не осталось без подписи.
ACTION_LABELS: dict[str, str] = {
    "auth.login": "Вход",
    "auth.logout": "Выход",
    "auth.credentials_changed": "Смена логина и пароля",
    "pwd.viewed_by_admin": "Просмотр пароля администратором",
    "pwd.reissued_by_admin": "Повторная выдача пароля",
    "pwd.changed_by_user": "Смена пароля",
    "course.created": "Курс создан",
    "course.updated": "Курс изменён",
    "course.deleted": "Курс удалён",
    "group.created": "Группа создана",
    "group.updated": "Группа изменена",
    "group.archived": "Группа в архиве",
    "group.member_added": "Участник добавлен",
    "group.member_updated": "Участник изменён",
    "group.member_removed": "Участник удалён",
    "curator.attached": "Куратор прикреплён",
    "curator.detached": "Куратор откреплён",
    "import.previewed": "Импорт: предпросмотр",
    "import.committed": "Импорт подтверждён",
    "assignment.created": "Задание создано",
    "assignment.updated": "Задание изменено",
    "assignment.archived": "Задание в архиве",
    "submission.created": "Сдача отправлена",
    "submission.status_changed": "Смена статуса",
    "submission.review_reverted": "Отзыв проверки",
    "file.downloaded": "Файл скачан",
    "file.deleted": "Файл удалён",
    "backup.exported": "Экспорт базы JSON",
    "backup.restored": "Восстановление базы",
}

#: Физическое удаление там, где в §7.12 нет отдельного кода: группа и
#: задание удаляются записью ``*.updated`` с признаком ``deleted`` в
#: ``details`` (приём Э-3, см. ``services/groups.py`` и
#: ``services/assignments.py``). Подпись на экране не должна врать.
_DELETED_VIA_UPDATE = {
    "group.updated": "Группа удалена",
    "assignment.updated": "Задание удалено",
}

#: Заголовки выгрузки CSV. Столбец «Код действия» дублирует подпись
#: значением из §7.12 — по нему строку можно сверить с базой.
CSV_HEADERS = (
    "Дата и время",
    "Автор",
    "Роль",
    "Действие",
    "Код действия",
    "Тип объекта",
    "ID объекта",
    "Курс",
    "Группа",
    "Задание",
    "Статус (был)",
    "Статус (стал)",
    "Детали",
)


def action_label(action: str, details: Mapping[str, Any] | None = None) -> str:
    """Подпись действия для экрана и выгрузки."""
    if details and details.get("deleted"):
        replaced = _DELETED_VIA_UPDATE.get(action)
        if replaced:
            return replaced
    return ACTION_LABELS.get(action, action)


def format_timestamp(stored: str | None) -> str:
    """Время хранения → текст «01.10.2026 14:05» в местной зоне.

    Нечитаемое значение возвращается как есть: строка журнала не должна
    исчезать с экрана из-за ошибки форматирования.
    """
    if not stored:
        return ""
    try:
        parsed = datetime.strptime(stored, STORED_DATETIME_FORMAT)
    except ValueError:
        return stored
    return parsed.replace(tzinfo=timezone.utc).astimezone().strftime(
        DISPLAY_DATETIME_FORMAT
    )


def local_date(stored: str | None) -> str:
    """Дата времени хранения в местной зоне (``ГГГГ-ММ-ДД``).

    Именно эта дата показывается в колонке «Дата и время», поэтому
    фильтр по дате сравнивает её же: выборка всегда совпадает с тем,
    что человек видит в строке.
    """
    if not stored:
        return ""
    try:
        parsed = datetime.strptime(stored, STORED_DATETIME_FORMAT)
    except ValueError:
        return ""
    return parsed.replace(tzinfo=timezone.utc).astimezone().date().isoformat()


def parse_filters(args) -> tuple[dict[str, Any], bool]:
    """Значения фильтров журнала из строки запроса (FR-63).

    Семантика повторяет фильтры дашборда: ``reset`` очищает ровно один
    ключ (или все сразу), остальные значения остаются прежними —
    сбросы независимы. Пустое значение означает «фильтр не задан»
    (``None``).

    Неверное значение не выбрасывается молча: оно считается ошибкой
    фильтра (``True`` вторым элементом) и обнуляет выборку целиком —
    иначе по остаточным данным экрана можно было бы распознать
    подставленное значение. Дата обязана быть календарной датой
    ``ГГГГ-ММ-ДД``, роль — из ``ROLE_ORDER``, тип объекта — из
    ``ENTITY_TYPES``, курс — положительным числом (существующий он
    или уже удалённый — не проверяется: записи журнала переживают
    удаление курса, Д-11).
    """
    raw = {key: (args.get(key) or "").strip() for key in FILTER_KEYS}
    reset = (args.get("reset") or "").strip()
    if reset == "all":
        raw = {key: "" for key in FILTER_KEYS}
    elif reset in FILTER_KEYS:
        raw[reset] = ""

    applied: dict[str, Any] = {key: None for key in FILTER_KEYS}
    malformed = False

    value = raw["date"]
    if value:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            malformed = True
        else:
            applied["date"] = parsed.date().isoformat()

    value = raw["role"]
    if value:
        if value in config.ROLE_ORDER:
            applied["role"] = value
        else:
            malformed = True

    value = raw["course"]
    if value:
        try:
            parsed_course = int(value)
        except ValueError:
            malformed = True
        else:
            if parsed_course < 1:
                malformed = True
            else:
                applied["course"] = parsed_course

    value = raw["entity"]
    if value:
        if value in config.ENTITY_TYPES:
            applied["entity"] = value
        else:
            malformed = True

    return applied, malformed


def _detail_value(value: Any) -> str:
    """Значение из ``details`` — текстом для экрана и выгрузки."""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (list, tuple)):
        return ", ".join(_detail_value(item) for item in value)
    if isinstance(value, Mapping):
        return _details_label(value)
    return str(value)


def _details_label(details: Mapping[str, Any] | None) -> str:
    """``details`` → «ключ: значение; ключ: значение» (FR-61)."""
    if not details:
        return ""
    return "; ".join(
        "{0}: {1}".format(key, _detail_value(value))
        for key, value in details.items()
    )


def _context_label(entity_id: Any, title: Any) -> str | None:
    """Подпись контекста: название, «№N (удалён)» или ничего.

    Идентификатор остаётся в журнале после удаления объекта (Д-11),
    поэтому строка с пустым названием не должна исчезать — она
    показывает, что запись относилась к уже удалённому объекту.
    """
    if entity_id is None:
        return None
    if title:
        return str(title)
    return "№{0} (удалён)".format(entity_id)


def _present(row: Mapping[str, Any]) -> dict[str, Any]:
    """Строка ``action_log`` → данные экрана и выгрузки (FR-61)."""
    raw_details = row["details_json"]
    details = json.loads(raw_details) if raw_details else {}
    from_status = row["from_status"]
    to_status = row["to_status"]
    status_parts = [
        config.STATUSES[code]["label"]
        for code in (from_status, to_status)
        if code
    ]
    role = row["actor_role"]
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "time_label": format_timestamp(row["created_at"]),
        # Обезличенный автор (удаление курса, Д-11): e-mail нет, но
        # роль на момент действия в записи сохранилась.
        "anonymized": row["actor_email"] is None,
        "actor_name": row["actor_name"],
        "actor_email": row["actor_email"],
        "actor_role": role,
        "role_label": config.ROLES.get(role, {}).get("label", role),
        "action": row["action"],
        "action_label": action_label(row["action"], details),
        "entity_type": row["entity_type"],
        "entity_type_label": config.ENTITY_TYPE_LABELS.get(
            row["entity_type"], row["entity_type"]
        ),
        "entity_id": row["entity_id"],
        "course_label": _context_label(row["course_id"], row["course_title"]),
        "group_label": _context_label(row["group_id"], row["group_name"]),
        "assignment_label": _context_label(
            row["assignment_id"], row["assignment_title"]
        ),
        "from_status": from_status,
        "to_status": to_status,
        "status_label": " → ".join(status_parts),
        "details": details,
        "details_label": _details_label(details),
    }


def query(
    filters: Mapping[str, Any],
    *,
    connection: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Выборка журнала по фильтрам (FR-63), новые записи сверху.

    Роль, курс и тип объекта фильтруются в SQL; дата — после выборки,
    по местной дате строки, чтобы фильтр совпадал с колонкой
    «Дата и время» (см. ``local_date``). Постраничного разбиения нет:
    раздел 4.2 его запрещает, поэтому возвращается вся выборка.
    """
    if connection is None:
        from app import db

        connection = db.get_db()

    sql = """
        SELECT a.*,
               u.full_name AS actor_name,
               u.email AS actor_email,
               c.title AS course_title,
               g.name AS group_name,
               s.title AS assignment_title
          FROM action_log a
          LEFT JOIN users u ON u.id = a.actor_id
          LEFT JOIN courses c ON c.id = a.course_id
          LEFT JOIN groups g ON g.id = a.group_id
          LEFT JOIN assignments s ON s.id = a.assignment_id
         WHERE 1 = 1
    """
    params: list[Any] = []
    if filters.get("role"):
        sql += " AND a.actor_role = ?"
        params.append(filters["role"])
    if filters.get("course") is not None:
        sql += " AND a.course_id = ?"
        params.append(filters["course"])
    if filters.get("entity"):
        sql += " AND a.entity_type = ?"
        params.append(filters["entity"])
    sql += " ORDER BY a.created_at DESC, a.id DESC"

    rows = [_present(dict(row)) for row in connection.execute(sql, params)]
    date = filters.get("date")
    if date:
        rows = [row for row in rows if local_date(row["created_at"]) == date]
    return rows


def to_csv(rows: Iterable[Mapping[str, Any]]) -> str:
    """Выборка → текст CSV (FR-63).

    Разделитель — запятая, перевод строки — CRLF: так файл открывается
    в Excel «как есть». BOM добавляет вызывающая сторона, кодируя
    результат в ``utf-8-sig``.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)
    for row in rows:
        writer.writerow(
            [
                row["time_label"],
                row["actor_email"] or ("обезличен" if row["anonymized"] else ""),
                row["role_label"],
                row["action_label"],
                row["action"],
                row["entity_type_label"],
                row["entity_id"] if row["entity_id"] is not None else "",
                row["course_label"] or "",
                row["group_label"] or "",
                row["assignment_label"] or "",
                (
                    config.STATUSES[row["from_status"]]["label"]
                    if row["from_status"]
                    else ""
                ),
                (
                    config.STATUSES[row["to_status"]]["label"]
                    if row["to_status"]
                    else ""
                ),
                row["details_label"],
            ]
        )
    return buffer.getvalue()


def course_options(
    *, connection: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """Курсы для списка фильтра — все, видимые администратору (FR-63)."""
    if connection is None:
        from app import db

        connection = db.get_db()
    return [
        {"id": row["id"], "title": row["title"]}
        for row in connection.execute(
            "SELECT id, title FROM courses ORDER BY title, id"
        )
    ]
