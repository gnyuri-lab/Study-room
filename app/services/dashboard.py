"""Сводные показатели, фильтры и разрезы прогресса (FR-55…FR-60).

Экран аналитики для администратора (FR-55) и куратора (FR-56) собран
из одного сервиса: разница только в области видимости, а не в логике
считывания. Четыре решения этапа.

**Работа — это пара «студент × задание», а не строка ``submissions``.**
Статус пары — статус последней попытки, а если попыток нет —
``not_submitted``: «не сдано» не хранится (Д-10), но на дашборде оно
обязано числиться, иначе распределение по пяти статусам не сойдётся
с числом работ (FR-55, AC-41). Каждая пара появляется в выборке ровно
один раз, поэтому «Всего» в любой таблице — это одно и то же число.
Сводка «Студентов» и «Заданий» тоже считается из этих пар, а не из
отфильтрованных списков: иначе фильтр, сужающий только одну сторону
(группа, задание, поиск по имени), показывал бы в сводке объекты, до
которых ни у кого не осталось ни одной работы.

**Область видимости считается здесь, а не берётся из сессии.**
``access.visible_group_ids`` читает текущего пользователя из сеанса,
а сервису нужен явный ``user`` — так сервис проверяется без входа и
не может молча посчитать данные чужого курса. Правила те же, что в
``access`` (BR-28, BR-29): администратор видит всё, куратор — только
свои группы и только неархивные. Неверное значение фильтра даёт пустую
выборку целиком, а не частичную (FR-08, AC-16).

**Фильтры складываются «и», сброс — по одному ключу.** Значения всех
четырёх полей приходят в одной строке запроса: выбор в любом поле
сохраняет остальные, а кнопка «Сбросить» передаёт имя только своего
ключа — так выполняется FR-58 («сбрасываются независимо друг от
друга») и AC-41 («сбрасывает их по одному»).

**Студент выбирается поиском по имени со списком подсказок (замена по
замечаниям заказчика 01.10.2026).** Три поля-идентификатора (курс,
группа, задание) читаются как числа и проверяются на принадлежность
области видимости; четвёртое поле — свободный текст, который ищется
подстрокой в ФИО студентов области. Рядом с полем сервер отдаёт список
всех ФИО области: при вводе букв показываются все подходящие, и выбор
происходит из списка — так выполняется замечание «выводить полные
ФИО и выбирать из списка». Список считается по всей области и не
сужается применёнными фильтрами, как и опции остальных полей (FR-58).
Никакого отклонения это не добавляет: запрос вне области видимости и
запрос без совпадений дают одно и то же — обычную пустую выборку с
подписью «нет данных для выбранных фильтров», без сообщения о неверном
фильтре.

**E-mail и пароли не читаются вовсе.** В запросах сервиса нет полей
``email``, ``password_enc`` и ``password_hash``, поэтому таблица
успеваемости не может показать их даже по ошибке (FR-59, AC-17,
AC-22, BR-30).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from werkzeug.datastructures import MultiDict

from app import access, charts, config, db
from app.services import assignments as assignments_service

#: Порядок полей на экране (FR-58): курс, группа, задание, поиск
#: по имени студента.
FILTER_KEYS = ("course", "group", "assignment", "student")

#: Подпись поля и подпись пустого варианта «ничего не выбрано».
#: У поискового поля пустого варианта нет: его роль выполняет
#: пустое значение, а кнопка «Сбросить» появляется рядом.
FILTER_TITLES = {
    "course": ("Курс", "Все курсы"),
    "group": ("Группа", "Все группы"),
    "assignment": ("Задание", "Все задания"),
    "student": ("Поиск по имени", ""),
}

#: Подсказка внутри поискового поля: показывает, что вводится
#: часть ФИО, а не точное имя.
STUDENT_PLACEHOLDER = "Часть ФИО"

#: Студент без групп в выборке — отдельная строка разреза, а не
#: пропуск: его работа существует и должна попасть в распределение.
NO_GROUP = "Без группы"

#: Подпись архивного объекта: работы в архиве сохранены и участвуют
#: в распределении, но помечены (чек-лист §12).
ARCHIVED_SUFFIX = " (архив)"


def _connection() -> sqlite3.Connection:
    return db.get_db()


def _placeholders(values) -> str:
    return ",".join("?" for _ in values)


# --------------------------------------------------------------------------
# Область видимости (BR-28, BR-29)
# --------------------------------------------------------------------------


def scope_of(user: sqlite3.Row | None) -> dict[str, Any]:
    """Курсы и группы, данные которых дашборд вправе показать.

    Правила повторяют ``access.visible_course_ids`` и
    ``access.visible_group_ids``, но считаются по переданному
    ``user``, а не по сеансу: одно и то же правило должно работать и
    в маршруте, и в тестах сервиса.
    """
    connection = _connection()
    if user is None:
        return {"course_ids": set(), "group_ids": set(), "is_admin": False}

    if user["role"] == access.ADMIN_ROLE:
        courses = {row["id"] for row in connection.execute("SELECT id FROM courses")}
        groups = {row["id"] for row in connection.execute("SELECT id FROM groups")}
        return {"course_ids": courses, "group_ids": groups, "is_admin": True}

    ids = [int(row["id"]) for row in access.identity_rows(user)]
    if not ids:
        return {"course_ids": set(), "group_ids": set(), "is_admin": False}
    table = "curator_groups" if user["role"] == access.CURATOR_ROLE else "group_members"
    # Архивные группы из активной области видимости исключены (FR-23):
    # работы в них сохранены, но группа больше не участвует в работе.
    rows = connection.execute(
        """
        SELECT DISTINCT g.id, g.course_id
          FROM groups g
          JOIN {table} m ON m.group_id = g.id
         WHERE m.user_id IN ({marks}) AND g.archived_at IS NULL
        """.format(table=table, marks=_placeholders(ids)),
        ids,
    ).fetchall()
    groups = {row["id"] for row in rows}
    courses = {row["course_id"] for row in rows}
    return {"course_ids": courses, "group_ids": groups, "is_admin": False}


# --------------------------------------------------------------------------
# Выборка объектов области видимости
# --------------------------------------------------------------------------


def _courses(scope: dict[str, Any]) -> list[dict[str, Any]]:
    if not scope["course_ids"]:
        return []
    ids = sorted(scope["course_ids"])
    rows = _connection().execute(
        "SELECT id, title FROM courses WHERE id IN ({0}) ORDER BY title, id".format(
            _placeholders(ids)
        ),
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _groups(scope: dict[str, Any]) -> list[dict[str, Any]]:
    if not scope["group_ids"]:
        return []
    ids = sorted(scope["group_ids"])
    rows = _connection().execute(
        """
        SELECT id, name, course_id, archived_at
          FROM groups
         WHERE id IN ({0})
         ORDER BY name, id
        """.format(_placeholders(ids)),
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _students(scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Студенты области видимости.

    Администратор видит всех студентов своих курсов — включая тех,
    кто пока не состоит ни в одной группе. Куратор видит только
    студентов своих групп (BR-29): чужой студент не должен попасть
    ни в выборку, ни в результат поиска по имени.
    """
    connection = _connection()
    if scope["is_admin"]:
        if not scope["course_ids"]:
            return []
        ids = sorted(scope["course_ids"])
        rows = connection.execute(
            """
            SELECT id, full_name, course_id
              FROM users
             WHERE role = 'student' AND is_active = 1
               AND course_id IN ({0})
             ORDER BY full_name, id
            """.format(_placeholders(ids)),
            ids,
        ).fetchall()
        return [dict(row) for row in rows]

    if not scope["group_ids"]:
        return []
    ids = sorted(scope["group_ids"])
    rows = connection.execute(
        """
        SELECT DISTINCT u.id, u.full_name, u.course_id
          FROM users u
          JOIN group_members gm ON gm.user_id = u.id
         WHERE gm.group_id IN ({0})
           AND u.role = 'student' AND u.is_active = 1
         ORDER BY u.full_name, u.id
        """.format(_placeholders(ids)),
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _assignments(scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Задания области видимости; архивные остаются с подписью.

    Порядок — ``order_index``, заданный при создании: §4.2 запрещает
    средства сортировки, но заданный порядок показывать можно.
    """
    if not scope["course_ids"]:
        return []
    ids = sorted(scope["course_ids"])
    rows = _connection().execute(
        """
        SELECT a.id, a.title, a.course_id, a.due_at, a.order_index,
               a.archived_at, c.title AS course_title
          FROM assignments a
          JOIN courses c ON c.id = a.course_id
         WHERE a.course_id IN ({0})
         ORDER BY c.title, a.order_index, a.id
        """.format(_placeholders(ids)),
        ids,
    ).fetchall()
    payload = [dict(row) for row in rows]
    for item in payload:
        item["is_archived"] = item["archived_at"] is not None
        item["due_label"] = assignments_service.format_due(item["due_at"])
    return payload


def _groups_of_students(
    scope: dict[str, Any], students: list[dict[str, Any]]
) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """Группы студента в его курсе: ``(student_id, course_id) → строки``.

    Только группы области видимости: куратор не должен разглядеть
    название группы, к которой не прикреплён (AC-16).
    """
    index: dict[tuple[int, int], list[dict[str, Any]]] = {}
    if not students or not scope["group_ids"]:
        return index
    student_ids = [item["id"] for item in students]
    group_ids = sorted(scope["group_ids"])
    rows = _connection().execute(
        """
        SELECT gm.user_id, g.id, g.name, g.course_id, g.archived_at
          FROM group_members gm
          JOIN groups g ON g.id = gm.group_id
         WHERE gm.user_id IN ({0}) AND g.id IN ({1})
         ORDER BY g.name, g.id
        """.format(_placeholders(student_ids), _placeholders(group_ids)),
        student_ids + group_ids,
    ).fetchall()
    for row in rows:
        index.setdefault((int(row["user_id"]), int(row["course_id"])), []).append(
            dict(row)
        )
    return index


def _members_of_group(group_id: int) -> set[int]:
    """Студенты группы — для фильтра «группа»."""
    rows = _connection().execute(
        "SELECT user_id FROM group_members WHERE group_id = ?", (group_id,)
    ).fetchall()
    return {int(row["user_id"]) for row in rows}


def _latest_attempts(
    students: list[dict[str, Any]], assignments: list[dict[str, Any]]
) -> dict[tuple[int, int], dict[str, Any]]:
    """Последняя попытка каждой пары «студент × задание» (Д-10).

    Один запрос на всю выборку: строки идут по возрастанию
    ``attempt_no``, поэтому последняя запись пары перезаписывает
    предыдущую.
    """
    latest: dict[tuple[int, int], dict[str, Any]] = {}
    if not students or not assignments:
        return latest
    student_ids = [item["id"] for item in students]
    assignment_ids = [item["id"] for item in assignments]
    rows = _connection().execute(
        """
        SELECT id, student_id, assignment_id, attempt_no, status
          FROM submissions
         WHERE student_id IN ({students})
           AND assignment_id IN ({assignments})
         ORDER BY attempt_no
        """.format(
            students=_placeholders(student_ids),
            assignments=_placeholders(assignment_ids),
        ),
        student_ids + assignment_ids,
    ).fetchall()
    for row in rows:
        latest[(int(row["student_id"]), int(row["assignment_id"]))] = dict(row)
    return latest


# --------------------------------------------------------------------------
# Фильтры (FR-58)
# --------------------------------------------------------------------------


def parse_filters(args: MultiDict) -> tuple[dict[str, int | str | None], bool]:
    """Значения полей из строки запроса, уже с учётом сброса.

    ``reset`` очищает ровно один ключ (или все сразу), остальные
    значения остаются прежними — этим выполняется независимость
    сбросов. Три ключа-идентификатора обязаны быть положительными
    числами: нечисловое значение не выбрасывается молча, оно
    считается неверным фильтром и обнуляет выборку, чтобы
    подставленное значение не могло ничего изменить.

    ``student`` — исключение из этого правила: это поиск по имени,
    свободный текст, поэтому любое его значение допустимо и лишь
    сужает выборку. ``None`` у любого ключа означает «фильтр не
    задан», у ``student`` это пустая строка после очистки.

    Возвращает ``(значения, есть_ли_неверное_числовое_значение)``.
    """
    raw = {key: (args.get(key) or "").strip() for key in FILTER_KEYS}
    reset = (args.get("reset") or "").strip()
    if reset == "all":
        raw = {key: "" for key in FILTER_KEYS}
    elif reset in FILTER_KEYS:
        raw[reset] = ""

    applied: dict[str, int | str | None] = {}
    malformed = False
    for key, value in raw.items():
        if not value:
            applied[key] = None
            continue
        if key == "student":
            applied[key] = value
            continue
        try:
            parsed = int(value)
        except ValueError:
            applied[key] = None
            malformed = True
            continue
        if parsed < 1:
            applied[key] = None
            malformed = True
            continue
        applied[key] = parsed
    return applied, malformed


def _outside_scope(
    applied: dict[str, int | str | None],
    malformed: bool,
    scope: dict[str, Any],
    assignments: list[dict[str, Any]],
) -> bool:
    """Есть ли значение идентификатора вне области видимости (FR-08, AC-16).

    Проверяются три поля-идентификатора: курс, группа и задание.
    Ответ один и тот же для «чужого» и «несуществующего» объекта,
    поэтому проверка — только принадлежность своей области.

    Поиск по имени студента объектом не является, поэтому сюда не
    попадает: запрос вне области видимости и запрос без совпадений
    дают обычную пустую выборку, а не сообщение о неверном фильтре.
    """
    if malformed:
        return True
    course = applied.get("course")
    group = applied.get("group")
    assignment = applied.get("assignment")
    if course is not None and course not in scope["course_ids"]:
        return True
    if group is not None and group not in scope["group_ids"]:
        return True
    if assignment is not None and assignment not in {item["id"] for item in assignments}:
        return True
    return False


def _filter_students(
    students: list[dict[str, Any]], applied: dict[str, int | str | None]
) -> list[dict[str, Any]]:
    """Студенты, прошедшие все поля (складываются «и»).

    Поиск по имени — подстрока без учёта регистра: частичное ФИО
    находит студента так же, как полное. Сравнение идёт только по
    студентам области видимости, чужое ФИО сюда попасть не может.
    """
    result = students
    course = applied.get("course")
    if course is not None:
        result = [item for item in result if item["course_id"] == course]
    group = applied.get("group")
    if group is not None:
        members = _members_of_group(group)
        result = [item for item in result if item["id"] in members]
    query = applied.get("student")
    if query:
        needle = query.casefold()
        result = [item for item in result if needle in item["full_name"].casefold()]
    return result


def _filter_assignments(
    assignments: list[dict[str, Any]], applied: dict[str, int | str | None]
) -> list[dict[str, Any]]:
    result = assignments
    course = applied.get("course")
    if course is not None:
        result = [item for item in result if item["course_id"] == course]
    assignment = applied.get("assignment")
    if assignment is not None:
        result = [item for item in result if item["id"] == assignment]
    return result


# --------------------------------------------------------------------------
# Пары «студент × задание»
# --------------------------------------------------------------------------


def _pairs(
    students: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
    latest: dict[tuple[int, int], dict[str, Any]],
    groups_of: dict[tuple[int, int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Все пары «студент × задание» внутри одного курса.

    Пара существует только в пределах курса: студент курса A не
    получает работ по заданиям курса B, даже если где-то состоит.
    """
    by_course_student: dict[int, list[dict[str, Any]]] = {}
    for item in students:
        by_course_student.setdefault(item["course_id"], []).append(item)
    by_course_assignment: dict[int, list[dict[str, Any]]] = {}
    for item in assignments:
        by_course_assignment.setdefault(item["course_id"], []).append(item)

    pairs: list[dict[str, Any]] = []
    for course_id, course_students in by_course_student.items():
        course_assignments = by_course_assignment.get(course_id, [])
        if not course_assignments:
            continue
        for student in course_students:
            member_groups = groups_of.get((student["id"], course_id), [])
            for assignment in course_assignments:
                attempt = latest.get((student["id"], assignment["id"]))
                status = attempt["status"] if attempt else "not_submitted"
                pairs.append(
                    {
                        "student_id": student["id"],
                        "student_name": student["full_name"],
                        "course_id": course_id,
                        "course_title": assignment["course_title"],
                        "groups": [
                            {"id": group["id"], "name": group["name"]}
                            for group in member_groups
                        ],
                        "group_label": (
                            ", ".join(group["name"] for group in member_groups)
                            if member_groups
                            else NO_GROUP
                        ),
                        "assignment_id": assignment["id"],
                        "assignment_title": assignment["title"],
                        "assignment_order": assignment["order_index"],
                        "is_archived": assignment["is_archived"],
                        "due_label": assignment["due_label"],
                        "status": status,
                        "status_label": config.STATUSES[status]["label"],
                        "submission_id": attempt["id"] if attempt else None,
                    }
                )
    pairs.sort(
        key=lambda item: (
            item["student_name"].casefold(),
            item["course_title"].casefold(),
            item["assignment_order"],
            item["assignment_id"],
        )
    )
    return pairs


def _breakdown(
    pairs: list[dict[str, Any]],
    key_of: Callable[[dict[str, Any]], Any],
    label_of: Callable[[dict[str, Any]], str],
    sort_of: Callable[[dict[str, Any]], Any],
) -> list[dict[str, Any]]:
    """Прогресс в разрезе одного признака (FR-60).

    ``key_of`` решает, куда попадает работа, ``label_of`` — как строка
    подписывается, ``sort_of`` — в каком порядке строки выводятся
    (§4.2 запрещает пользовательскую сортировку, порядок вывода
    фиксирован здесь).
    """
    index: dict[Any, dict[str, Any]] = {}
    for item in pairs:
        key = key_of(item)
        entry = index.get(key)
        if entry is None:
            entry = {
                "key": key,
                "label": label_of(item),
                "_sort": sort_of(item),
                "counts": {code: 0 for code in config.STATUS_ORDER},
                "total": 0,
            }
            index[key] = entry
        entry["counts"][item["status"]] += 1
        entry["total"] += 1
    return _sorted_rows(index)


def _sorted_rows(index: dict[Any, dict[str, Any]]) -> list[dict[str, Any]]:
    """Строки разреза в заданном порядке и без служебного поля."""
    rows = list(index.values())
    rows.sort(key=lambda row: row["_sort"])
    for row in rows:
        row.pop("_sort", None)
    return rows


def _group_breakdown(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Разрез по группам: одна работа учитывается в каждой группе студента.

    Студент без групп области видимости идёт отдельной строкой
    «Без группы» — его работа не должна пропасть из общего числа.
    """
    index: dict[Any, dict[str, Any]] = {}

    def touch(key: Any, label: str, sort_value: Any) -> dict[str, Any]:
        entry = index.get(key)
        if entry is None:
            entry = {
                "key": key,
                "label": label,
                "_sort": sort_value,
                "counts": {code: 0 for code in config.STATUS_ORDER},
                "total": 0,
            }
            index[key] = entry
        return entry

    for item in pairs:
        if item["groups"]:
            for group in item["groups"]:
                entry = touch(
                    ("group", group["id"]),
                    group["name"],
                    (0, group["name"].casefold(), item["course_title"].casefold()),
                )
                entry["counts"][item["status"]] += 1
                entry["total"] += 1
        else:
            entry = touch(
                ("none", item["course_id"]),
                NO_GROUP,
                (1, item["course_title"].casefold(), ""),
            )
            entry["counts"][item["status"]] += 1
            entry["total"] += 1

    return _sorted_rows(index)


def _rows(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Строки таблицы успеваемости (FR-59, AC-17).

    Порядок — по ФИО, затем по курсу и порядку задания: таблицу
    читают глазами по студентам, а не по заданиям. Поля ``email`` и
    пароли сюда не попадают — сервис их не читает.
    """
    return [
        {
            "student_name": item["student_name"],
            "group_label": item["group_label"],
            "course_title": item["course_title"],
            "assignment_title": item["assignment_title"],
            "is_archived": item["is_archived"],
            "due_label": item["due_label"],
            "status": item["status"],
            "status_label": item["status_label"],
            "submission_id": item["submission_id"],
        }
        for item in pairs
    ]


# --------------------------------------------------------------------------
# Сборка дашборда
# --------------------------------------------------------------------------


def build(user: sqlite3.Row | None, args: MultiDict) -> dict[str, Any]:
    """Полный контекст экрана дашборда.

    Отдаёт область видимости, опции и применённые значения фильтров,
    сводные показатели, распределение по пяти статусам, четыре разреза
    прогресса (FR-60), таблицу успеваемости (FR-59) и SVG-диаграмму
    (FR-57).
    """
    scope = scope_of(user)
    courses = _courses(scope)
    groups = _groups(scope)
    students = _students(scope)
    assignments = _assignments(scope)
    groups_of = _groups_of_students(scope, students)

    applied, malformed = parse_filters(args)
    filter_error = _outside_scope(applied, malformed, scope, assignments)

    active = any(applied.get(key) is not None for key in FILTER_KEYS)
    # Опции считаются по всей области и не сужаются применённым
    # полем: иначе выбор в одном поле скрыл бы значение другого и
    # сброс перестал бы быть независимым (FR-58). Это относится и
    # к списку ФИО в поисковом поле: подсказки показывают всех
    # студентов области, а не только оставшихся после фильтров.
    fields = _fields(courses, groups, assignments, students, applied)

    if filter_error:
        # Пустая выборка целиком, а не частичная: иначе по остаточным
        # данным можно было бы определить чужой объект (FR-08).
        students = []
        assignments = []
    else:
        students = _filter_students(students, applied)
        assignments = _filter_assignments(assignments, applied)

    latest = _latest_attempts(students, assignments)
    pairs = _pairs(students, assignments, latest, groups_of)

    counts = {code: 0 for code in config.STATUS_ORDER}
    for item in pairs:
        counts[item["status"]] += 1
    total = len(pairs)

    # Сводка считается из тех же пар, что и вся остальная часть экрана.
    # Иначе фильтр, сужающий только одну сторону, лгал бы: «группа»
    # оставляла бы все шесть заданий двух курсов при четырёх работах,
    # а «поиск по имени» — чужое задание, до которого у студента нет
    # ни одной пары. Разрезы и так считаются по парам, поэтому сводка
    # обязана совпадать с ними.
    summary_students = {item["student_id"] for item in pairs}
    summary_assignments = {item["assignment_id"] for item in pairs}

    return {
        "scope_title": "Все курсы" if scope["is_admin"] else "Мои группы",
        "is_admin": scope["is_admin"],
        "filters": {"fields": fields, "active": active},
        "filter_error": filter_error,
        "filtered": active or filter_error,
        "has_data": total > 0,
        "summary": {
            "students": len(summary_students),
            "assignments": len(summary_assignments),
            "works": total,
            "accepted": counts["accepted"] + counts["accepted_late"],
        },
        "counts": counts,
        "total": total,
        "chart": charts.status_distribution_svg(counts),
        "breakdowns": {
            "course": _breakdown(
                pairs,
                key_of=lambda item: item["course_id"],
                label_of=lambda item: item["course_title"],
                sort_of=lambda item: item["course_title"].casefold(),
            ),
            "group": _group_breakdown(pairs),
            "assignment": _breakdown(
                pairs,
                key_of=lambda item: item["assignment_id"],
                label_of=lambda item: item["assignment_title"]
                + (ARCHIVED_SUFFIX if item["is_archived"] else ""),
                sort_of=lambda item: (
                    item["course_title"].casefold(),
                    item["assignment_order"],
                    item["assignment_id"],
                ),
            ),
            "student": _breakdown(
                pairs,
                key_of=lambda item: item["student_id"],
                label_of=lambda item: item["student_name"],
                sort_of=lambda item: item["student_name"].casefold(),
            ),
        },
        "rows": _rows(pairs),
    }


def _fields(
    courses: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
    students: list[dict[str, Any]],
    applied: dict[str, int | str | None],
) -> list[dict[str, Any]]:
    """Три выпадающих списка и поисковое поле с подсказками (FR-58).

    Названия вне единственного курса уточняются его именем — иначе
    «ФК-1» двух курсов неразличимы в выпадающем списке. Поисковое
    поле свободного ввода, но не слепое: ``options`` — все полные
    ФИО студентов области видимости, из которых администратор и
    куратор выбирают (замечание заказчика 01.10.2026). Дубли имён
    убираются: два однофамильца и так найдутся одним подстрочным
    запросом.
    """
    multi = len(courses) > 1
    course_titles = {item["id"]: item["title"] for item in courses}

    def qualify(label: str, course_id: int) -> str:
        if not multi:
            return label
        return "{0} — {1}".format(course_titles.get(course_id, ""), label)

    def select(key: str, options: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "key": key,
            "type": "select",
            "label": FILTER_TITLES[key][0],
            "empty": FILTER_TITLES[key][1],
            "value": applied.get(key),
            "options": options,
        }

    names = list(dict.fromkeys(item["full_name"] for item in students))

    return [
        select(
            "course",
            [{"value": item["id"], "label": item["title"]} for item in courses],
        ),
        select(
            "group",
            [
                {
                    "value": item["id"],
                    "label": qualify(
                        item["name"]
                        + (ARCHIVED_SUFFIX if item["archived_at"] else ""),
                        item["course_id"],
                    ),
                }
                for item in groups
            ],
        ),
        select(
            "assignment",
            [
                {
                    "value": item["id"],
                    "label": qualify(
                        item["title"]
                        + (ARCHIVED_SUFFIX if item["is_archived"] else ""),
                        item["course_id"],
                    ),
                }
                for item in assignments
            ],
        ),
        {
            "key": "student",
            "type": "search",
            "label": FILTER_TITLES["student"][0],
            "placeholder": STUDENT_PLACEHOLDER,
            "value": applied.get("student") or "",
            "options": [{"value": name, "label": name} for name in names],
        },
    ]
