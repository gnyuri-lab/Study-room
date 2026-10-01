-- Study room / Учебный кабинет
-- Миграция 001: схема данных по ТЗ_MVP.md §7.1—§7.14.
--
-- Правила, принятые при написании схемы:
--   * даты и время хранятся текстом в формате ISO 8601 (UTC, с суффиксом Z);
--   * коды статусов, ролей и действий хранятся только кодами из §7.1, §7.2, §7.11, §7.12;
--   * ON DELETE CASCADE не используется нигде: каскад по курсу (BR-22…BR-27)
--     выполняет сервисный слой в явном порядке, а база не даёт удалить
--     родителя раньше потомков — неверный порядок приводит к ошибке, а не
--     к тихой потере данных;
--   * в action_log внешние ключи не объявляются: журнал переживает удаление
--     курса, автора и сущностей (Д-11);
--   * в файловых таблицах stored_name не содержит путей и расширений (FR-54, NFR-06);
--   * вложение комментария связано односторонне: submission_comments.file_id
--     ссылается на submission_comment_files (ТЗ §7.10, поле file_id — одно,
--     то есть у комментария ноль или один файл). Обратной ссылки и цикла
--     нет, поэтому схема создаётся одним проходом CREATE TABLE и повторное
--     применение 001 не требует ALTER TABLE.

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL,
    description TEXT,
    created_at  TEXT    NOT NULL,
    CHECK (length(title) BETWEEN 1 AND 200)
);

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY,
    course_id            INTEGER REFERENCES courses (id),
    email                TEXT    NOT NULL COLLATE NOCASE,
    full_name            TEXT    NOT NULL,
    role                 TEXT    NOT NULL,
    password_enc         BLOB    NOT NULL,
    password_hash        TEXT    NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    is_active            INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT    NOT NULL,
    CHECK (role IN ('admin', 'curator', 'student')),
    CHECK (must_change_password IN (0, 1)),
    CHECK (is_active IN (0, 1)),
    CHECK (length(email) BETWEEN 3 AND 254),
    CHECK (length(full_name) BETWEEN 1 AND 200),
    CHECK (
        (role = 'admin' AND course_id IS NULL)
        OR (role IN ('curator', 'student') AND course_id IS NOT NULL)
    ),
    UNIQUE (course_id, email)
);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY,
    course_id   INTEGER NOT NULL REFERENCES courses (id),
    name        TEXT    NOT NULL,
    archived_at TEXT,
    created_at  TEXT    NOT NULL,
    CHECK (length(name) BETWEEN 1 AND 100),
    UNIQUE (course_id, name)
);

-- §7.6: у таблиц членства ровно два поля — group_id и user_id. created_at
-- здесь не добавляется: ни одно требование его не использует, а порядок
-- действий хранится в action_log со своим created_at.

CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES groups (id),
    user_id  INTEGER NOT NULL REFERENCES users (id),
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS curator_groups (
    group_id INTEGER NOT NULL REFERENCES groups (id),
    user_id  INTEGER NOT NULL REFERENCES users (id),
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS assignments (
    id          INTEGER PRIMARY KEY,
    course_id   INTEGER NOT NULL REFERENCES courses (id),
    title       TEXT    NOT NULL,
    description TEXT,
    due_at      TEXT,
    order_index INTEGER NOT NULL,
    archived_at TEXT,
    created_at  TEXT    NOT NULL,
    CHECK (length(title) BETWEEN 1 AND 200),
    CHECK (order_index >= 0)
);

CREATE TABLE IF NOT EXISTS assignment_files (
    id            INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments (id),
    original_name TEXT    NOT NULL,
    stored_name   TEXT    NOT NULL,
    mime          TEXT,
    size_bytes    INTEGER NOT NULL,
    uploaded_by   INTEGER NOT NULL REFERENCES users (id),
    created_at    TEXT    NOT NULL,
    CHECK (size_bytes > 0 AND size_bytes <= 10485760),
    CHECK (length(original_name) BETWEEN 1 AND 255),
    CHECK (
        stored_name NOT LIKE '%/%'
        AND stored_name NOT LIKE '%\%'
        AND stored_name NOT LIKE '..%'
        AND stored_name NOT LIKE '.%'
    ),
    UNIQUE (stored_name)
);

CREATE TABLE IF NOT EXISTS submissions (
    id           INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments (id),
    student_id   INTEGER NOT NULL REFERENCES users (id),
    attempt_no   INTEGER NOT NULL,
    status       TEXT    NOT NULL,
    comment      TEXT    NOT NULL,
    submitted_at TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    CHECK (status IN (
        'not_submitted', 'submitted', 'revision',
        'accepted', 'accepted_late'
    )),
    CHECK (length(comment) BETWEEN 1 AND 4000),
    CHECK (attempt_no BETWEEN 1 AND 3),
    UNIQUE (assignment_id, student_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS submission_files (
    id            INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions (id),
    original_name TEXT    NOT NULL,
    stored_name   TEXT    NOT NULL,
    mime          TEXT,
    size_bytes    INTEGER NOT NULL,
    uploaded_by   INTEGER NOT NULL REFERENCES users (id),
    created_at    TEXT    NOT NULL,
    CHECK (size_bytes > 0 AND size_bytes <= 10485760),
    CHECK (length(original_name) BETWEEN 1 AND 255),
    CHECK (
        stored_name NOT LIKE '%/%'
        AND stored_name NOT LIKE '%\%'
        AND stored_name NOT LIKE '..%'
        AND stored_name NOT LIKE '.%'
    ),
    UNIQUE (stored_name)
);

CREATE TABLE IF NOT EXISTS submission_comment_files (
    id             INTEGER PRIMARY KEY,
    submission_id  INTEGER NOT NULL REFERENCES submissions (id),
    original_name  TEXT    NOT NULL,
    stored_name    TEXT    NOT NULL,
    mime           TEXT,
    size_bytes     INTEGER NOT NULL,
    uploaded_by    INTEGER NOT NULL REFERENCES users (id),
    created_at     TEXT    NOT NULL,
    CHECK (size_bytes > 0 AND size_bytes <= 10485760),
    CHECK (length(original_name) BETWEEN 1 AND 255),
    CHECK (
        stored_name NOT LIKE '%/%'
        AND stored_name NOT LIKE '%\%'
        AND stored_name NOT LIKE '..%'
        AND stored_name NOT LIKE '.%'
    ),
    UNIQUE (stored_name)
);

CREATE TABLE IF NOT EXISTS submission_comments (
    id            INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions (id),
    author_id     INTEGER NOT NULL REFERENCES users (id),
    body          TEXT    NOT NULL,
    file_id       INTEGER REFERENCES submission_comment_files (id),
    created_at    TEXT    NOT NULL,
    CHECK (length(body) BETWEEN 1 AND 4000)
);

CREATE TABLE IF NOT EXISTS action_log (
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
        'file.downloaded', 'file.deleted'
    )),
    CHECK (entity_type IN (
        'course', 'group', 'assignment', 'submission',
        'user', 'file', 'import'
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

CREATE TABLE IF NOT EXISTS import_batches (
    id          INTEGER PRIMARY KEY,
    actor_id    INTEGER NOT NULL REFERENCES users (id),
    course_id   INTEGER NOT NULL REFERENCES courses (id),
    file_name   TEXT    NOT NULL,
    total_rows  INTEGER NOT NULL,
    valid_rows  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    CHECK (length(file_name) BETWEEN 1 AND 255),
    CHECK (total_rows >= 0),
    CHECK (valid_rows >= 0 AND valid_rows <= total_rows)
);

CREATE TABLE IF NOT EXISTS import_errors (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES import_batches (id),
    row_number  INTEGER NOT NULL,
    field       TEXT,
    error_text  TEXT    NOT NULL,
    CHECK (row_number >= 2),
    CHECK (field IS NULL OR length(field) <= 64)
);
