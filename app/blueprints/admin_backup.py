"""Резервные копии базы: экспорт в JSON и восстановление (FR-68…FR-71).

Только администратор (BR-33): ``access.admin_required`` на каждом из
четырёх маршрутов. Экран не привязан к курсу: копия — это вся база
целиком, включая все курсы и журнал.

Маршруты и порядок между ними — часть сценария, а не деталь:

``index``    GET  — экран «Резервные копии»: кнопка экспорта и форма
                    загрузки копии; заодно сбрасывает незавершённую
                    загрузку, чтобы подтверждение показывало ровно тот
                    файл, который видели.
``export``   GET  — файл JSON; успешный экспорт записывается в журнал
                    (FR-68).
``preview``  POST — загрузка и валидация копии, счётчики по таблицам
                    (FR-70); в базу не пишется ничего.
``restore``  POST — полная замена данных (FR-71); после успеха сессия
                    чистится: текущая учётная запись могла в копии не
                    оказаться, и вход просят заново.

Предпросмотр и восстановление разнесены намеренно: восстановление —
POST без формы, с подтверждением ``data-confirm``, чтобы случайный
клик или открытая из закладки страница не заменили базу (§9.1).
"""

from __future__ import annotations

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app import access, action_log, config
from app.services import ServiceError, backup

bp = Blueprint("admin_backup", __name__, url_prefix="/admin")


def _actor():
    return access.current_user()


def _limit_mb() -> int:
    return config.BACKUP_MAX_BYTES // (1024 * 1024)


def _render_screen(*, error: str | None = None, status: int = 200):
    """Экран «Резервные копии»: форма загрузки и отказ уровня файла."""
    return render_template("admin/backup.html", error=error, limit_mb=_limit_mb()), status


@bp.get("/backup")
@access.admin_required
def index():
    """Экран резервных копий (FR-68, FR-70)."""
    # Открытие страницы сбрасывает незавершённую загрузку: без этого
    # кнопка подтверждения могла бы восстановить файл, которого
    # администратор уже не видит.
    backup.discard()
    return _render_screen()


@bp.get("/backup/export")
@access.admin_required
def export():
    """Скачать базу одним JSON-файлом (FR-68)."""
    try:
        payload = backup.export_payload()
        data = backup.serialize(payload)
    except ServiceError as failure:
        flash(str(failure), "error")
        return redirect(url_for("admin_backup.index"))

    summary = backup.summarise(payload)
    action_log.log_action(
        "backup.exported",
        actor_id=_actor()["id"],
        actor_role=_actor()["role"],
        entity_type="backup",
        details={
            "records": summary["records"],
            "files": summary["files"],
        },
    )
    stamp = payload[backup.EXPORTED_AT_KEY][:10].replace("-", "")
    name = "study_room_backup_{0}.json".format(stamp)
    return Response(
        data,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="{0}"'.format(name)},
    )


@bp.post("/backup/preview")
@access.admin_required
def preview():
    """Проверить загруженную копию и показать её состав (FR-70)."""
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        return _render_screen(error=config.MESSAGES["backup_no_file"], status=400)
    try:
        summary = backup.stage(upload)
    except ServiceError as failure:
        return _render_screen(error=str(failure), status=400)
    return render_template(
        "admin/backup_preview.html",
        summary=summary,
        table_labels=backup.TABLE_LABELS,
        table_order=backup.TABLES,
        limit_mb=_limit_mb(),
    )


@bp.post("/backup/restore")
@access.admin_required
def restore():
    """Заменить базу и файлы данными копии (FR-71)."""
    try:
        backup.restore(actor=_actor())
    except ServiceError as failure:
        flash(str(failure), "error")
        return redirect(url_for("admin_backup.index"))

    # Сессия чистится до flash: сообщение попадает в очищенную сессию
    # и переживает перенаправление на экран входа.
    session.clear()
    flash(config.MESSAGES["backup_restored"], "info")
    return redirect(url_for("auth.login"))
