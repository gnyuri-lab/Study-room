-- Study room / Учебный кабинет
-- Миграция 003: файлы переписки привязываются к комментарию, а не
-- комментарий к одиночному файлу (решение этапа Э-6 «вариант B»:
-- до пяти файлов на комментарий, §7.10 и FR-46 приведены к этому коду).
--
-- Раньше связь шла через submission_comments.file_id — одиночная ссылка
-- на строку submission_comment_files (001_schema.sql, §7.10). Теперь
-- файл ссылается на комментарий: submission_comment_files.comment_id.
-- Комментариев без файлов остаётся сколько угодно, у файла может быть
-- владелец-комментарий либо NULL у строк, созданных до миграции и не
-- привязанных ни к чему (таких быть не должно — backfill их и выявляет).
--
-- Порядок шагов важен и проверен на рабочей копии базы:
--   1. новая колонка появляется до любого обновления данных;
--   2. backfill идёт из ещё существующего file_id;
--   3. индекс по file_id снимается до удаления колонки;
--   4. удаление колонки — последним, integrity_check и
--      foreign_key_check после миграции проходят без замечаний.
-- ON DELETE CASCADE не используется: как и в 001_schema.sql, каскад по
-- курсу собирается явным порядком в services/courses.py (CASCADE_ORDER),
-- и там же файлы теперь удаляются раньше комментариев.

ALTER TABLE submission_comment_files
    ADD COLUMN comment_id INTEGER REFERENCES submission_comments (id);

UPDATE submission_comment_files
   SET comment_id = (
           SELECT c.id
             FROM submission_comments c
            WHERE c.file_id = submission_comment_files.id
       );

DROP INDEX IF EXISTS idx_submission_comments_file_id;

ALTER TABLE submission_comments DROP COLUMN file_id;

CREATE INDEX IF NOT EXISTS idx_submission_comment_files_comment_id
    ON submission_comment_files (comment_id);
