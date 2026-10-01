"""Демонстрационные данные демо-курса (FR-65…FR-67, AC-42).

Источник правды — каталог ``Inputs/Демо-данные``: название и описание
курса, описания четырёх заданий и материалы заданий читаются оттуда,
поэтому папка одновременно служит и источником для ``seed-demo``, и
набором файлов для ручной загрузки через экраны приложения.

Создание идёт штатными сервисами, поэтому все действия попадают в
журнал действий (FR-62), а статусы выставляются теми же правилами, что
и в ручной работе: автостатус «Сдано после дедлайна» вычисляется
системой (FR-47, BR-18) из-за прошедшего дедлайна первого задания.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import current_app, session
from werkzeug.datastructures import FileStorage

from app import attachments, config, db
from app.services import (
    ServiceError,
    assignments as assignments_service,
    courses as courses_service,
    groups as groups_service,
    members as members_service,
    statuses as statuses_service,
    submissions as submissions_service,
)

DEMO_DIR = config.BASE_DIR / "Inputs" / "Демо-данные"
COURSE_FILE = "Описание курса.txt"

DEMO_PASSWORD = "Demo1234"
DEMO_GROUP_NAMES = ("Демо-группа 1", "Демо-группа 2")
CURATOR = ("Кирилл Веснин", "curator@demo-study.ru")

#: ``(группа 0|1, ФИО, e-mail)`` — вымышленные персоны, не совпадающие
#: с реестрами ``Inputs/Список студентов-ФК_*.xlsx`` (FR-67, AC-42).
STUDENTS = (
    (0, "Аркадий Ветров", "vetrov@demo-study.ru"),
    (0, "Лада Морозова", "morozova@demo-study.ru"),
    (0, "Тимур Гладков", "gladkov@demo-study.ru"),
    (0, "Яна Кузнецова", "kuznetsova@demo-study.ru"),
    (0, "Ратмир Белов", "belov@demo-study.ru"),
    (1, "Эльвира Соколова", "sokolova@demo-study.ru"),
    (1, "Добрыня Кравцов", "kravtsov@demo-study.ru"),
    (1, "Мирна Ланская", "lanskaya@demo-study.ru"),
    (1, "Олег Тюрин", "turyin@demo-study.ru"),
    (1, "Светлана Громова", "gromova@demo-study.ru"),
)

#: ``(название, файл описания, смещение дедлайна в днях, материал)``.
#: У первого задания дедлайн в прошлом — воспроизводим сценарий
#: автостатуса (FR-66); у четвёртого материала нет (FR-66).
ASSIGNMENTS = (
    (
        "Оценка компании методом DCF",
        "Описание задания 1.txt",
        -7,
        "ДЗ1_DCF.xlsx",
    ),
    (
        "Сравнительный анализ по мультипликаторам",
        "Описание задания 2.txt",
        14,
        "ДЗ2_Multiples.xlsx",
    ),
    (
        "Стоимость капитала: WACC и CAPM",
        "Описание задания 3.txt",
        None,
        "ДЗ3_Capitalization.xlsx",
    ),
    (
        "Инвестиционный меморандум",
        "Описание задания 4.txt",
        31,
        None,
    ),
)

#: План сдач: по одному коду на студента в каждом задании.
#: ``.`` — не сдано, ``s`` — сдано и ждёт проверки, ``r`` — сдано и
#: возвращено на доработку, ``a`` — принято, ``A`` и ``2`` — вторая
#: попытка (принята и возвращена соответственно).
PLAN = (
    "aAsr.as.2a",
    "as.asras.a",
    "saa.rs.as.",
    "r.ssaar.as",
)

STUDENT_COMMENT = "Отправляю работу, расчёты — в приложенной таблице."
STUDENT_COMMENT_RETRY = (
    "Вторая попытка: исправил замечания куратора, смотрите новую версию."
)
CURATOR_REVISION = (
    "Проверил работу: есть ошибки. Исправьте и отправьте повторно."
)
CURATOR_REVISION_RETRY = (
    "Исправления приняты не полностью: доработайте и отправьте ещё раз."
)
CURATOR_ACCEPT = "Работа принята, замечания учтены."


def _read_course_file() -> tuple[str, str]:
    """Название и описание курса: первая строка файла — название."""
    path = DEMO_DIR / COURSE_FILE
    if not path.is_file():
        raise ServiceError(
            "Не найден файл демо-данных: {path}. Проверьте папку"
            " «Inputs/Демо-данные».".format(path=path)
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    title = (lines[0] if lines else "").strip()
    description = "\n".join(lines[1:]).strip()
    if not title or not description:
        raise ServiceError(
            "Файл {path}: первая строка — название курса, далее —"
            " описание.".format(path=path)
        )
    return title, description


def _read_description(file_name: str) -> str:
    path = DEMO_DIR / file_name
    if not path.is_file():
        raise ServiceError(
            "Не найден файл описания задания: {path}.".format(path=path)
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ServiceError("Файл описания задания пуст: {path}.".format(path=path))
    return text


def _stage(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ServiceError(
            "Не найден материал задания: {path}.".format(path=path)
        )
    upload = FileStorage(stream=io.BytesIO(path.read_bytes()), filename=path.name)
    return attachments.stage_upload(upload)


def _stage_text(file_name: str, text: str) -> dict[str, Any]:
    upload = FileStorage(
        stream=io.BytesIO(text.encode("utf-8")), filename=file_name
    )
    return attachments.stage_upload(upload)


def _due(delta_days: int | None) -> str | None:
    if delta_days is None:
        return None
    moment = datetime.now() + timedelta(days=delta_days)
    return moment.strftime("%Y-%m-%dT%H:%M")


def _fetch_admin() -> sqlite3.Row:
    row = db.get_db().execute(
        "SELECT * FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        raise ServiceError(
            "Нет учётной записи администратора: выполните"
            " «python -m app.cli init-db»."
        )
    return row


def _fetch_user(user_id: int) -> sqlite3.Row:
    row = db.get_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise ServiceError("Учётная запись не найдена: {id}.".format(id=user_id))
    return row


def _fetch_assignment(assignment_id: int) -> sqlite3.Row:
    row = assignments_service.fetch(assignment_id)
    if row is None:
        raise ServiceError("Задание не найдено: {id}.".format(id=assignment_id))
    return row


def _find_demo_course(title: str) -> sqlite3.Row | None:
    return db.get_db().execute(
        "SELECT id, title FROM courses WHERE title = ? ORDER BY id LIMIT 1",
        (title,),
    ).fetchone()


def _summary(status: str, course_id: int | None) -> dict[str, Any]:
    connection = db.get_db()
    if course_id is None:
        return {
            "status": status,
            "course_id": None,
            "title": None,
            "groups": 0,
            "students": 0,
            "curators": 0,
            "assignments": 0,
            "submissions": 0,
            "not_submitted": 0,
        }
    title_row = connection.execute(
        "SELECT title FROM courses WHERE id = ?", (course_id,)
    ).fetchone()
    if title_row is None:
        return _summary(status, None)
    groups = connection.execute(
        "SELECT COUNT(*) FROM groups WHERE course_id = ?", (course_id,)
    ).fetchone()[0]
    students = connection.execute(
        """
        SELECT COUNT(DISTINCT u.id) FROM users u
        JOIN group_members gm ON gm.user_id = u.id
        JOIN groups g ON g.id = gm.group_id
        WHERE g.course_id = ? AND u.role = 'student'
        """,
        (course_id,),
    ).fetchone()[0]
    curators = connection.execute(
        """
        SELECT COUNT(DISTINCT u.id) FROM users u
        JOIN curator_groups cg ON cg.user_id = u.id
        JOIN groups g ON g.id = cg.group_id
        WHERE g.course_id = ? AND u.role = 'curator'
        """,
        (course_id,),
    ).fetchone()[0]
    assignments = connection.execute(
        "SELECT COUNT(*) FROM assignments"
        " WHERE course_id = ? AND archived_at IS NULL",
        (course_id,),
    ).fetchone()[0]
    submissions = connection.execute(
        """
        SELECT COUNT(*) FROM submissions s
        JOIN assignments a ON a.id = s.assignment_id
        WHERE a.course_id = ?
        """,
        (course_id,),
    ).fetchone()[0]
    pairs = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT s.assignment_id, s.student_id
            FROM submissions s
            JOIN assignments a ON a.id = s.assignment_id
            WHERE a.course_id = ?
        )
        """,
        (course_id,),
    ).fetchone()[0]
    return {
        "status": status,
        "course_id": course_id,
        "title": title_row["title"],
        "groups": groups,
        "students": students,
        "curators": curators,
        "assignments": assignments,
        "submissions": submissions,
        "not_submitted": max(students * assignments - pairs, 0),
    }


def _submit(
    assignment: sqlite3.Row,
    student: sqlite3.Row,
    *,
    attempt_no: int,
    comment: str,
) -> int:
    title_index = next(
        index
        for index, spec in enumerate(ASSIGNMENTS)
        if spec[0] == assignment["title"]
    )
    surname = student["full_name"].split()[-1]
    file_name = "{surname}_ДЗ{n}_попытка{k}.txt".format(
        surname=surname, n=title_index + 1, k=attempt_no
    )
    body = (
        "{name}\n{title}\nПопытка {k}.\n{comment}\n".format(
            name=student["full_name"],
            title=assignment["title"],
            k=attempt_no,
            comment=comment,
        )
    )
    return submissions_service.create(
        assignment=assignment,
        actor=student,
        comment=comment,
        files=[_stage_text(file_name, body)],
    )


def _review(
    submission_id: int,
    curator: sqlite3.Row,
    *,
    requested: str,
    comment: str | None,
) -> str:
    return statuses_service.change_status(
        submission_id=submission_id,
        requested=requested,
        actor=curator,
        comment=comment,
        files=[],
    )


def _build_submissions(
    assignment_rows: list[sqlite3.Row],
    student_rows: list[sqlite3.Row],
    curator: sqlite3.Row,
) -> None:
    for assignment, plan in zip(assignment_rows, PLAN):
        for student, code in zip(student_rows, plan):
            if code == ".":
                continue
            if code in ("s", "r", "a"):
                submission_id = _submit(
                    assignment,
                    student,
                    attempt_no=1,
                    comment=STUDENT_COMMENT,
                )
                if code == "r":
                    _review(
                        submission_id,
                        curator,
                        requested="revision",
                        comment=CURATOR_REVISION,
                    )
                elif code == "a":
                    _review(
                        submission_id,
                        curator,
                        requested="accepted",
                        comment=CURATOR_ACCEPT,
                    )
                continue
            first_id = _submit(
                assignment,
                student,
                attempt_no=1,
                comment=STUDENT_COMMENT,
            )
            _review(
                first_id,
                curator,
                requested="revision",
                comment=CURATOR_REVISION,
            )
            second_id = _submit(
                assignment,
                student,
                attempt_no=2,
                comment=STUDENT_COMMENT_RETRY,
            )
            if code == "2":
                _review(
                    second_id,
                    curator,
                    requested="revision",
                    comment=CURATOR_REVISION_RETRY,
                )
            else:
                _review(
                    second_id,
                    curator,
                    requested="accepted",
                    comment=CURATOR_ACCEPT,
                )


def seed_demo(*, force: bool = False) -> dict[str, Any]:
    """Создать демо-курс целиком либо сообщить, что он уже загружен.

    ``force`` удаляет существующий демо-курс (каскадно, с подтверждением
    названия) и создаёт данные заново — единственный способ обновить
    демо-данные после ручной правки.

    Нужен контекст запроса, а не только приложения: проверка работы
    куратором (``statuses.change_status``) читает область видимости из
    сессии, поэтому на время проверки в сессии стоит сам куратор.
    """
    with current_app.test_request_context():
        return _seed_demo(force=force)


def _seed_demo(*, force: bool) -> dict[str, Any]:
    admin = _fetch_admin()
    title, description = _read_course_file()

    existing = _find_demo_course(title)
    if existing is not None:
        if not force:
            return _summary("exists", existing["id"])
        courses_service.delete_course(
            course_id=existing["id"],
            confirmation=existing["title"],
            actor=admin,
        )

    course_id = courses_service.create_course(
        title=title, description=description, actor=admin
    )
    group_ids = [
        groups_service.create_group(course_id=course_id, name=name, actor=admin)
        for name in DEMO_GROUP_NAMES
    ]
    curator_id = members_service.create_member(
        course_id=course_id,
        group_id=group_ids[0],
        full_name=CURATOR[0],
        role="curator",
        email=CURATOR[1],
        password=DEMO_PASSWORD,
        actor=admin,
        added_via="seed",
    )
    members_service.attach_curator(
        group_id=group_ids[1], user_id=curator_id, actor=admin
    )
    curator = _fetch_user(curator_id)

    student_rows: list[sqlite3.Row] = []
    for group_index, full_name, email in STUDENTS:
        student_id = members_service.create_member(
            course_id=course_id,
            group_id=group_ids[group_index],
            full_name=full_name,
            role="student",
            email=email,
            password=DEMO_PASSWORD,
            actor=admin,
            added_via="seed",
        )
        student_rows.append(_fetch_user(student_id))

    assignment_rows: list[sqlite3.Row] = []
    for spec in ASSIGNMENTS:
        name, description_file, delta_days, material = spec
        files = [_stage(DEMO_DIR / material)] if material else []
        assignment_id = assignments_service.create_assignment(
            course_id=course_id,
            title=name,
            description=_read_description(description_file),
            due_at=_due(delta_days),
            actor=admin,
            files=files,
        )
        assignment_rows.append(_fetch_assignment(assignment_id))

    session["user_id"] = curator["id"]
    _build_submissions(assignment_rows, student_rows, curator)
    return _summary("created", course_id)
