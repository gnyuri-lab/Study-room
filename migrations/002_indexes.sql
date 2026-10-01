-- Study room / Учебный кабинет
-- Миграция 002: индексы по ТЗ_MVP.md §7.1—§7.14 и разделу 13.2.
--
-- Индексы нужны для типовых выборок приложения: вход по e-mail, списки по
-- курсу, статусы сдач, выборка последней попытки студента, выборка групп
-- куратора и студента, журнал действий по дате и курсу.
--
-- Отдельно про e-mail. Глобального уникального индекса по e-mail здесь
-- нет и быть не должно: один и тот же e-mail допустим на разных курсах
-- (БР-06, ФР-05 — при входе выбирается курс). Уникальность в пределах курса
-- обеспечивает составной UNIQUE (course_id, email) из 001_schema.sql,
-- а уникальность e-mail администратора — idx_users_admin_email ниже, потому
-- что course_id у него NULL, а NULL в UNIQUE считаются различными.

CREATE INDEX IF NOT EXISTS idx_users_email
    ON users (email);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_admin_email
    ON users (email)
    WHERE course_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_users_course_id
    ON users (course_id);
CREATE INDEX IF NOT EXISTS idx_users_created_at
    ON users (created_at);

CREATE INDEX IF NOT EXISTS idx_courses_created_at
    ON courses (created_at);

CREATE INDEX IF NOT EXISTS idx_groups_course_id
    ON groups (course_id);

CREATE INDEX IF NOT EXISTS idx_groups_archived_at
    ON groups (archived_at);

CREATE INDEX IF NOT EXISTS idx_group_members_user_id
    ON group_members (user_id);

CREATE INDEX IF NOT EXISTS idx_curator_groups_user_id
    ON curator_groups (user_id);

CREATE INDEX IF NOT EXISTS idx_assignments_course_id
    ON assignments (course_id);

CREATE INDEX IF NOT EXISTS idx_assignments_order_index
    ON assignments (course_id, order_index);

CREATE INDEX IF NOT EXISTS idx_assignments_created_at
    ON assignments (created_at);

CREATE INDEX IF NOT EXISTS idx_assignment_files_assignment_id
    ON assignment_files (assignment_id);

CREATE INDEX IF NOT EXISTS idx_submissions_assignment_id
    ON submissions (assignment_id);

CREATE INDEX IF NOT EXISTS idx_submissions_student_id
    ON submissions (student_id);

CREATE INDEX IF NOT EXISTS idx_submissions_status
    ON submissions (status);

CREATE INDEX IF NOT EXISTS idx_submissions_created_at
    ON submissions (created_at);

CREATE INDEX IF NOT EXISTS idx_submission_files_submission_id
    ON submission_files (submission_id);

CREATE INDEX IF NOT EXISTS idx_submission_comments_submission_id
    ON submission_comments (submission_id);

CREATE INDEX IF NOT EXISTS idx_submission_comments_author_id
    ON submission_comments (author_id);

CREATE INDEX IF NOT EXISTS idx_submission_comments_file_id
    ON submission_comments (file_id);

CREATE INDEX IF NOT EXISTS idx_submission_comment_files_submission_id
    ON submission_comment_files (submission_id);

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

CREATE INDEX IF NOT EXISTS idx_import_batches_course_id
    ON import_batches (course_id);

CREATE INDEX IF NOT EXISTS idx_import_batches_created_at
    ON import_batches (created_at);

CREATE INDEX IF NOT EXISTS idx_import_errors_batch_id
    ON import_errors (batch_id);
