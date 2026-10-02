-- Study room / Учебный кабинет
-- Миграция 004: справочник §7.12 пополняется двумя кодами этапа Э-10
-- (backup.exported и backup.restored) и сущностью backup. Это
-- требование FR-68 и FR-69: экспорт и восстановление обязаны попадать
-- в журнал действий, а журнал берёт коды только из справочника.
--
-- SQLite не умеет менять CHECK-ограничение существующей таблицы, поэтому
-- action_log пересобирается целиком: новая таблица с расширенным
-- справочником, перенос строк, удаление старой, переименование. Идентификаторы
-- записей сохраняются: они нужны экрану журнала и выгрузке CSV.
--
-- После удаления старой таблицы пропадают её индексы из 002_indexes.sql,
-- поэтому семь индексов action_log создаются здесь заново под теми же
-- именами.
--
-- PRAGMA user_version здесь не выставляется: версию записывает
-- apply_migrations после коммита файла.

CREATE TABLE action_log_new (
    id            INTEGER PRIMARY KEY,
    actor_id      INTEGER,
    actor_role    TEXT    NOT NULL,
    action        TEXT    NOT NULL,
    entity_type   TEXT    NOT NULL,
    entity_id     INTEGER,
    course_id     INTEGER,
    group_id      INTEGER,
    assignment_id INTEGER,
    from_status   TEXT,
    to_status     TEXT,
    details_json  TEXT,
    created_at    TEXT    NOT NULL,
    CHECK (actor_role IN ('admin', 'curator', 'student')),
    CHECK (action IN (
        'auth.login', 'auth.logout', 'auth.credentials_changed',
        'pwd.viewed_by_admin', 'pwd.reissued_by_admin', 'pwd.changed_by_user',
        'course.created', 'course.updated', 'course.deleted',
        'group.created', 'group.updated', 'group.archived',
        'group.member_added', 'group.member_updated', 'group.member_removed',
        'curator.attached', 'curator.detached',
        'import.previewed', 'import.committed',
        'assignment.created', 'assignment.updated', 'assignment.archived',
        'submission.created', 'submission.status_changed',
        'submission.review_reverted',
        'file.downloaded', 'file.deleted',
        'backup.exported', 'backup.restored'
    )),
    CHECK (entity_type IN (
        'course', 'group', 'assignment', 'submission',
        'user', 'file', 'import', 'backup'
    )),
    CHECK (from_status IS NULL OR from_status IN (
        'not_submitted', 'submitted', 'revision',
        'accepted', 'accepted_late'
    )),
    CHECK (to_status IS NULL OR to_status IN (
        'not_submitted', 'submitted', 'revision',
        'accepted', 'accepted_late'
    ))
);

INSERT INTO action_log_new (
    id, actor_id, actor_role, action, entity_type, entity_id,
    course_id, group_id, assignment_id,
    from_status, to_status, details_json, created_at
)
SELECT
    id, actor_id, actor_role, action, entity_type, entity_id,
    course_id, group_id, assignment_id,
    from_status, to_status, details_json, created_at
FROM action_log;

DROP TABLE action_log;

ALTER TABLE action_log_new RENAME TO action_log;

CREATE INDEX IF NOT EXISTS idx_action_log_created_at
    ON action_log (created_at);

CREATE INDEX IF NOT EXISTS idx_action_log_actor_id
    ON action_log (actor_id);

CREATE INDEX IF NOT EXISTS idx_action_log_action
    ON action_log (action);

CREATE INDEX IF NOT EXISTS idx_action_log_entity_type
    ON action_log (entity_type);

CREATE INDEX IF NOT EXISTS idx_action_log_course_id
    ON action_log (course_id);

CREATE INDEX IF NOT EXISTS idx_action_log_group_id
    ON action_log (group_id);

CREATE INDEX IF NOT EXISTS idx_action_log_assignment_id
    ON action_log (assignment_id);
