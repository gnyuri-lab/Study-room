"""Скачивание файлов: материалы задания, работы и комментарии
(FR-36, FR-53, FR-54, NFR-06, AC-33, AC-34).

Отдельный blueprint, хотя §13.2 перечисляет восемь модулей blueprints и
одновременно говорит о девяти: файлы нужны и администратору, и
студенту, и куратору, а правило доступа одно, поэтому оно держится в
одном маршруте, а не дублируется в трёх.

Маршрут один, ``kind`` различает вид вложения: ``assignment`` — материалы
задания (Э-5), ``submission`` — файлы работы (Э-6), ``comment`` —
вложение к сообщению в переписке (Э-6). Запрос своего вида берётся из
своей таблицы, но право всегда считается по одной и той же работе:
работа видна автору, куратору её группы и администратору (FR-49),
и файл на диске не должен быть доступнее самой работы.

Три правила, которые этот модуль держит (AC-33, AC-34, FR-53, FR-54):

* право проверяется на каждое скачивание, а не на момент показа ссылки;
* имя файла в ответе — исходное, но путь строится только по
  ``stored_name`` из базы, поэтому подстановка ``../`` в исходном имени
  не ведёт ни к какому файлу;
* отсутствующий на диске файл — это отказ «файл не найден», а не 500.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, send_file

from app import access, action_log, attachments, config, db
from app.services import assignments as assignments_service
from app.services import submissions as submissions_service

bp = Blueprint("files", __name__)

#: Виды вложений, которые этот адрес умеет отдавать.
KINDS = ("assignment", "submission", "comment")

#: Один запрос на вид: своя таблица, но курс и автор берутся от работы,
#: чтобы право считалось по ней, а не по случайному совпадению номеров.
_ROW_SQL = {
    "assignment": """
        SELECT f.id, f.original_name, f.stored_name, f.size_bytes, f.mime,
               f.assignment_id, a.course_id, NULL AS student_id
          FROM assignment_files f
          JOIN assignments a ON a.id = f.assignment_id
         WHERE f.id = ?
    """,
    "submission": """
        SELECT f.id, f.original_name, f.stored_name, f.size_bytes, f.mime,
               s.assignment_id, a.course_id, s.student_id
          FROM submission_files f
          JOIN submissions s ON s.id = f.submission_id
          JOIN assignments a ON a.id = s.assignment_id
         WHERE f.id = ?
    """,
    "comment": """
        SELECT f.id, f.original_name, f.stored_name, f.size_bytes, f.mime,
               s.assignment_id, a.course_id, s.student_id
          FROM submission_comment_files f
          JOIN submissions s ON s.id = f.submission_id
          JOIN assignments a ON a.id = s.assignment_id
         WHERE f.id = ?
    """,
}


def _file_row(kind: str, file_id: int) -> tuple[dict[str, Any], int]:
    """Строка вложения вместе с его курсом.

    Отсутствующий вид или номер — 404 без утечки: текст отказа в §9.3
    один и тот же и для «нет такого номера», и для «нет доступа».
    """
    if kind not in KINDS:
        abort(404)
    row = db.get_db().execute(_ROW_SQL[kind], (file_id,)).fetchone()
    if row is None:
        abort(404)
    return dict(row), row["assignment_id"]


def _has_right(kind: str, item: dict[str, Any]) -> bool:
    """Право на конкретный файл.

    Материал задания открыт всем, кому открыт курс: таков порядок
    проверки, уже занятый в Э-5 (FR-53). Файлы работы и переписки
    шире не становятся — они наследуют доступ к самой работе (FR-49):
    студенту своя, куратору — работы своей группы, администратору — все.
    """
    if kind == "assignment":
        return attachments.can_access_assignment_file(item)
    return submissions_service.can_open(
        access.current_user(),
        {"course_id": item["course_id"], "student_id": item["student_id"]},
    )


@bp.get("/files/<kind>/<int:file_id>")
@access.login_required
def download(kind: str, file_id: int):
    """Отдать файл, если право на него есть (FR-36, FR-53).

    Порядок проверок совпадает с решением по ролям: сначала право, и
    лишь потом сам файл. Иначе по времени ответа можно было бы отличить
    «файл существует, но чужой» от «файла нет» — а §9.3 требует для
    обоих один и тот же текст «Объект не найден или был удалён».
    """
    item, assignment_id = _file_row(kind, file_id)

    if not _has_right(kind, item):
        abort(403, description=config.MESSAGES["no_file_access"])

    # Согласованность: задание могло быть удалено между показом ссылки
    # и кликом. Право на курс сохраняется, но файла уже нет.
    if assignments_service.fetch(assignment_id) is None:
        abort(404)

    target = attachments.stored_path(item["stored_name"])
    if not target.is_file():
        abort(404, description=config.MESSAGES["file_missing_on_disk"])

    with db.transaction() as connection:
        action_log.log_action(
            "file.downloaded",
            actor_id=access.current_user()["id"],
            actor_role=access.current_user()["role"],
            entity_type="file",
            entity_id=item["id"],
            course_id=item["course_id"],
            assignment_id=assignment_id,
            details={"file_name": item["original_name"]},
            connection=connection,
        )

    # ``download_name`` — исходное имя для сохранения пользователем;
    # ``as_attachment`` не даёт браузеру выполнить файл. Тип ответа
    # берётся из записанного при загрузке ``mime``, а не из пути.
    return send_file(
        target,
        as_attachment=True,
        download_name=item["original_name"],
        mimetype=item["mime"] or "application/octet-stream",
    )
