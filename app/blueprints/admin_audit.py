"""Журнал действий: экран просмотра и выгрузка в CSV (FR-61…FR-64).

Только администратор (FR-63, BR-33): проверка роли стоит на каждом
маршруте через ``access.admin_required``.

Запись в журнал живёт в ``action_log.log_action`` с Этапа Э-2, поэтому
этот модуль только читает: выборка, подписи и формат выгрузки — работа
``action_log``, здесь лишь разбор строки запроса и ответ Flask.

Два правила экрана:

* четыре фильтра (дата, роль, курс, тип объекта) применяются
  одновременно и сбрасываются независимо, как на дашборде (FR-63);
  неверное значение фильтра даёт пустую выборку с сообщением, а не
  молча игнорируется;
* постраничного разбиения нет (раздел 4.2, п. 15): в выборке видны
  все строки, и выгрузка CSV повторяет ровно её же — ссылка
  собирается из применённых фильтров, поэтому файл и экран не могут
  разойтись (AC-32).
"""

from __future__ import annotations

from flask import Blueprint, Response, render_template, request, url_for

from app import access, action_log, config

bp = Blueprint("admin_audit", __name__, url_prefix="/admin")

#: Опции списков фильтров в порядке справочников §7.2 и §7.12 —
#: порядок и состав берутся из config, а не из шаблона.
ROLE_OPTIONS = [
    {"value": code, "label": config.ROLES[code]["label"]}
    for code in config.ROLE_ORDER
]
ENTITY_OPTIONS = [
    {"value": code, "label": config.ENTITY_TYPE_LABELS[code]}
    for code in config.ENTITY_TYPES
]


def _filters() -> tuple[dict, bool]:
    """Применённые значения фильтров и признак ошибки (FR-63)."""
    return action_log.parse_filters(request.args)


def _export_url(applied: dict) -> str:
    """Ссылка выгрузки с текущими фильтрами (без параметра ``reset``)."""
    active = {key: value for key, value in applied.items() if value is not None}
    return url_for("admin_audit.export", **active)


@bp.get("/audit")
@access.admin_required
def index():
    """Экран журнала: фильтры, выборка и пустое состояние (§9.2)."""
    applied, malformed = _filters()
    rows = [] if malformed else action_log.query(applied)
    return render_template(
        "admin/audit_log.html",
        rows=rows,
        applied=applied,
        filter_error=malformed,
        filters_active=any(value is not None for value in applied.values()),
        course_options=action_log.course_options(),
        role_options=ROLE_OPTIONS,
        entity_options=ENTITY_OPTIONS,
        export_url=_export_url(applied),
    )


@bp.get("/audit/export")
@access.admin_required
def export():
    """Выгрузка текущей выборки в CSV (FR-63, AC-32).

    BOM (``utf-8-sig``) добавляется для Excel: без него кириллица в
    Windows-Excel не открывается. Состав столбцов — ``CSV_HEADERS``
    из ``action_log``; паролей и IP-адресов в данных нет по построению
    (FR-64): выборка состоит только из полей §7.11.
    """
    applied, malformed = _filters()
    rows = [] if malformed else action_log.query(applied)
    payload = action_log.to_csv(rows).encode("utf-8-sig")
    return Response(
        payload,
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="action_log.csv"'},
    )
