"""Blueprints приложения.

Этап Э-2 создал ``auth``, Э-3 добавил ``admin_courses`` и
``admin_groups``, Э-4 — ``admin_import``, Э-5 — ``admin_assignments``,
``files`` и ``student``, Э-6 — ``curator`` и записи студента,
Э-8 — ``admin_audit``, Э-10 — ``admin_backup``. Остальные
подключаются на своих этапах и регистрируются в ``app/__init__.py``.
"""

from app.blueprints import (
    admin_assignments,
    admin_audit,
    admin_backup,
    admin_courses,
    admin_groups,
    admin_import,
    auth,
    curator,
    files,
    student,
)

BLUEPRINTS = (
    auth.bp,
    admin_courses.bp,
    admin_groups.bp,
    admin_import.bp,
    admin_assignments.bp,
    admin_audit.bp,
    admin_backup.bp,
    files.bp,
    student.bp,
    curator.bp,
)

__all__ = [
    "BLUEPRINTS",
    "admin_assignments",
    "admin_audit",
    "admin_backup",
    "admin_courses",
    "admin_groups",
    "admin_import",
    "auth",
    "curator",
    "files",
    "student",
]
