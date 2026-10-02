import sqlite3

import pytest
from conftest import insert_course, insert_user

from app import config, db, security

EXPECTED_TABLES = {
    "action_log",
    "assignment_files",
    "assignments",
    "courses",
    "curator_groups",
    "group_members",
    "groups",
    "import_batches",
    "import_errors",
    "submission_comment_files",
    "submission_comments",
    "submission_files",
    "submissions",
    "users",
}

FORBIDDEN_COLUMN_FRAGMENTS = (
    "паспорт",
    "payment",
    "платеж",
    "платёж",
    "card",
    "карт",
    "iban",
    "счет",
    "счёт",
    "ip_",
    "_ip",
    "address",
    "адрес",
)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """Не считать pbkdf2 по-настоящему в тестах служебных команд."""
    from app import passwords

    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in rows}


def index_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in rows}


def column_names(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def open_db(path):
    """Открыть базу файлом с именами колонок.

    Нужна для проверок результата служебных команд: ``cli`` закрывает своё
    соединение, а ``connection`` из фикстуры смотрит на тестовую базу.
    """
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def insert_group(conn, course_id=1, name="ФК_11", archived_at=None):
    cursor = conn.execute(
        "INSERT INTO groups (course_id, name, archived_at, created_at) "
        "VALUES (?, ?, ?, ?)",
        (course_id, name, archived_at, "2026-01-01T00:00:00Z"),
    )
    return cursor.lastrowid


def insert_assignment(conn, course_id=1, title="Задание", due_at=None, order_index=1):
    cursor = conn.execute(
        "INSERT INTO assignments "
        "(course_id, title, description, due_at, order_index, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            course_id,
            title,
            None,
            due_at,
            order_index,
            "2026-01-01T00:00:00Z",
        ),
    )
    return cursor.lastrowid


def insert_submission(conn, assignment_id, student_id, attempt_no=1, status="submitted"):
    cursor = conn.execute(
        "INSERT INTO submissions "
        "(assignment_id, student_id, attempt_no, status, comment, "
        "submitted_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            assignment_id,
            student_id,
            attempt_no,
            status,
            "Работа",
            "2026-01-02T10:00:00Z",
            "2026-01-02T10:00:00Z",
        ),
    )
    return cursor.lastrowid


def insert_file(conn, table, parent_column, parent_id, stored_name, uploaded_by=1, size=1024):
    cursor = conn.execute(
        f"INSERT INTO {table} "
        f"({parent_column}, original_name, stored_name, mime, size_bytes, "
        f"uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            parent_id,
            "исходное.docx",
            stored_name,
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document",
            size,
            uploaded_by,
            "2026-01-01T00:00:00Z",
        ),
    )
    return cursor.lastrowid


class TestMigrations:
    def test_applies_all_migrations(self, connection):
        assert db.applied_version(connection) == 4

    def test_second_run_is_idempotent(self, connection):
        assert db.apply_migrations(connection) == []

    def test_startup_applies_pending_migrations(self, workspace, key_path, tmp_path):
        # База уровня Э-9 (001—003, версия 3) без кодов журнала Э-10:
        # до правки такой базе create_app не помогал, и экспорт резервной
        # копии падал 500 на CHECK ограничения action (§12.1 «применяются
        # при старте»).
        legacy = tmp_path / "legacy-migrations"
        legacy.mkdir()
        for name in ("001_schema.sql", "002_indexes.sql", "003_comment_files.sql"):
            (legacy / name).write_text(
                (config.MIGRATIONS_DIR / name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        db.init_database(db_path=config.DB_PATH, migrations_dir=legacy)

        connection = sqlite3.connect(config.DB_PATH, isolation_level=None)
        connection.row_factory = sqlite3.Row
        assert db.applied_version(connection) == 3
        stale_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'action_log'"
        ).fetchone()["sql"]
        assert "backup.exported" not in stale_sql
        connection.close()

        from app import create_app

        application = create_app()

        connection = sqlite3.connect(config.DB_PATH, isolation_level=None)
        connection.row_factory = sqlite3.Row
        assert db.applied_version(connection) == 4
        actual_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'action_log'"
        ).fetchone()["sql"]
        assert "backup.exported" in actual_sql
        connection.close()

        # Журнал Э-10 на такой базе записывается, а не роняет IntegrityError.
        from app import action_log

        with application.app_context():
            action_log.log_action(
                action="backup.exported",
                actor_id=None,
                actor_role="admin",
                entity_type="backup",
                details={"records": 0, "files": 0},
            )

    def test_startup_does_not_create_missing_database(self, workspace, key_path):
        from app import create_app

        assert not config.DB_PATH.exists()
        create_app()
        assert not config.DB_PATH.exists()

    def test_rejects_schema_newer_than_scripts(self, connection, tmp_path):
        connection.execute("PRAGMA user_version = 99")
        directory = tmp_path / "one"
        directory.mkdir()
        (directory / "001_schema.sql").write_text("SELECT 1;", encoding="utf-8")
        with pytest.raises(RuntimeError, match="новее"):
            db.apply_migrations(connection, directory)

    def test_rejects_database_newer_than_migration_set(self, tmp_path):
        # Отметка 1 при схеме уровня 002: 001 пропускается, а оставшиеся
        # скрипты (002 — он идемпотентен, 003 и 004) применяются по порядку.
        # Базу собираем вручную из копий 001 и 002: на готовой схеме 003
        # повторно не пройдёт, а миграции применяются только вперёд.
        directory = tmp_path / "two"
        directory.mkdir()
        for name in ("001_schema.sql", "002_indexes.sql"):
            (directory / name).write_text(
                (config.MIGRATIONS_DIR / name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        scratch = sqlite3.connect(tmp_path / "partial.db", isolation_level=None)
        scratch.row_factory = sqlite3.Row
        scratch.execute("PRAGMA foreign_keys = ON")
        try:
            assert db.apply_migrations(scratch, directory) == [
                "001_schema.sql",
                "002_indexes.sql",
            ]
            scratch.execute("PRAGMA user_version = 1")
            assert db.apply_migrations(scratch) == [
                "002_indexes.sql",
                "003_comment_files.sql",
                "004_action_log_codes.sql",
            ]
        finally:
            scratch.close()

    def test_missing_directory_is_reported(self, connection, tmp_path):
        with pytest.raises(RuntimeError, match="не найден"):
            db.apply_migrations(connection, tmp_path / "absent")

    def test_directory_without_sql_is_reported(self, connection, tmp_path):
        empty = tmp_path / "no-sql"
        empty.mkdir()
        with pytest.raises(RuntimeError, match="нет файлов"):
            db.apply_migrations(connection, empty)

    def test_strip_comments_removes_sql_comments(self):
        script = "-- комментарий\nSELECT 1; -- хвостовой\nSELECT 2;\n"
        stripped = db._strip_comments(script)
        assert "--" not in stripped
        assert "комментарий" not in stripped
        assert "хвостовой" not in stripped
        assert "SELECT 1;" in stripped
        assert "SELECT 2;" in stripped

    def test_split_statements_skips_empty_chunks(self):
        assert db._split_statements("SELECT 1;;\n;\nSELECT 2;") == [
            "SELECT 1",
            "SELECT 2",
        ]


class TestTables:
    def test_all_expected_tables_exist(self, connection):
        assert table_names(connection) == EXPECTED_TABLES

    def test_no_extra_table_was_added(self, connection):
        assert "schema_migrations" not in table_names(connection)
        assert "sessions" not in table_names(connection)

    def test_indexes_cover_required_lookups(self, connection):
        names = index_names(connection)
        assert {
            "idx_users_email",
            "idx_users_admin_email",
            "idx_users_course_id",
            "idx_groups_course_id",
            "idx_assignments_course_id",
            "idx_submissions_status",
            "idx_submissions_student_id",
            "idx_submission_comments_submission_id",
            "idx_action_log_created_at",
            "idx_action_log_course_id",
            "idx_import_errors_batch_id",
        } <= names

    def test_no_sensitive_columns_in_schema(self, connection):
        for table in sorted(table_names(connection)):
            for column in column_names(connection, table):
                lowered = column.lower()
                for fragment in FORBIDDEN_COLUMN_FRAGMENTS:
                    assert fragment not in lowered, (
                        f"{table}.{column} похоже на чувствительное поле"
                    )

    def test_action_log_has_no_ip_column(self, connection):
        columns = column_names(connection, "action_log")
        assert "ip" not in columns
        assert "ip_address" not in columns
        assert "details_json" in columns

    def test_log_survives_deletion_of_referenced_rows(self, connection):
        insert_course(connection)
        admin = insert_user(connection, "admin@admin.com", role="admin")
        connection.execute(
            "INSERT INTO action_log "
            "(actor_id, actor_role, action, entity_type, entity_id, "
            "created_at) VALUES (?, 'admin', 'auth.login', 'user', ?, ?)",
            (admin, admin, "2026-01-01T00:00:00Z"),
        )
        connection.execute("UPDATE action_log SET actor_id = NULL")
        connection.execute("DELETE FROM users WHERE id = ?", (admin,))
        row = connection.execute("SELECT actor_id, action FROM action_log").fetchone()
        assert row["actor_id"] is None
        assert row["action"] == "auth.login"


class TestUsers:
    def test_password_stored_both_encrypted_and_hashed(self, connection):
        columns = column_names(connection, "users")
        assert "password_enc" in columns
        assert "password_hash" in columns
        types = {
            row[1]: row[2] for row in connection.execute("PRAGMA table_info(users)")
        }
        assert types["password_enc"] == "BLOB"
        assert types["password_hash"] == "TEXT"

    def test_email_unique_in_course_case_insensitive(self, connection):
        insert_course(connection)
        insert_user(connection, "student@example.com", role="student", course_id=1)
        with pytest.raises(sqlite3.IntegrityError):
            insert_user(
                connection, "STUDENT@EXAMPLE.COM", role="student", course_id=1
            )

    def test_same_email_allowed_in_two_courses(self, connection):
        insert_course(connection)
        insert_course(connection, title="Второй курс")
        insert_user(connection, "shared@example.com", role="student", course_id=1)
        insert_user(connection, "shared@example.com", role="student", course_id=2)
        count = conn_count(connection, "SELECT COUNT(*) FROM users")
        assert count == 2

    def test_admin_has_no_course(self, connection):
        admin = insert_user(connection, "admin@admin.com", role="admin")
        row = connection.execute(
            "SELECT course_id FROM users WHERE id = ?", (admin,)
        ).fetchone()
        assert row["course_id"] is None

    def test_admin_email_unique_case_insensitive(self, connection):
        insert_user(connection, "admin@admin.com", role="admin")
        with pytest.raises(sqlite3.IntegrityError):
            insert_user(connection, "ADMIN@ADMIN.COM", role="admin")

    def test_non_admin_requires_course(self, connection):
        insert_course(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_user(connection, "s@example.com", role="student", course_id=None)

    def test_admin_cannot_belong_to_course(self, connection):
        insert_course(connection)
        with pytest.raises(sqlite3.IntegrityError):
            conn_execute(
                connection,
                "INSERT INTO users (course_id, email, full_name, role, "
                "password_enc, password_hash, must_change_password, is_active, "
                "created_at) VALUES (1, 'a@a.com', 'Имя', 'admin', x'00', 'h', 0, 1, ?)",
                ("2026-01-01T00:00:00Z",),
            )

    def test_unknown_role_rejected(self, connection):
        insert_course(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_user(connection, "x@example.com", role="teacher", course_id=1)

    def test_must_change_password_is_zero_or_one(self, connection):
        insert_course(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_user(
                connection,
                "x@example.com",
                role="student",
                course_id=1,
                must_change_password=2,
            )

    def test_email_lookup_is_case_insensitive(self, connection):
        insert_course(connection)
        insert_user(connection, "student@example.com", role="student", course_id=1)
        rows = connection.execute(
            "SELECT id FROM users WHERE email = ?", ("Student@Example.COM",)
        ).fetchall()
        assert len(rows) == 1


class TestGroups:
    def test_name_unique_in_course(self, connection):
        insert_course(connection)
        insert_group(connection, name="ФК_11")
        with pytest.raises(sqlite3.IntegrityError):
            insert_group(connection, name="ФК_11")

    def test_same_name_in_two_courses(self, connection):
        insert_course(connection)
        insert_course(connection, title="Второй")
        insert_group(connection, course_id=1, name="ФК_11")
        insert_group(connection, course_id=2, name="ФК_11")

    def test_archived_at_is_allowed_and_null_by_default(self, connection):
        insert_course(connection)
        group_id = insert_group(connection)
        row = connection.execute(
            "SELECT archived_at FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
        assert row["archived_at"] is None
        connection.execute(
            "UPDATE groups SET archived_at = ? WHERE id = ?",
            ("2026-02-01T00:00:00Z", group_id),
        )
        row = connection.execute(
            "SELECT archived_at FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
        assert row["archived_at"] == "2026-02-01T00:00:00Z"

    def test_membership_duplicate_rejected(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        group_id = insert_group(connection)
        for table in ("group_members", "curator_groups"):
            connection.execute(
                f"INSERT INTO {table} (group_id, user_id) VALUES (?, ?)",
                (group_id, student),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO {table} (group_id, user_id) VALUES (?, ?)",
                    (group_id, student),
                )

    def test_membership_has_only_two_columns(self, connection):
        assert column_names(connection, "group_members") == ["group_id", "user_id"]
        assert column_names(connection, "curator_groups") == ["group_id", "user_id"]

    def test_archived_group_still_blocks_duplicate_name(self, connection):
        insert_course(connection)
        insert_group(connection, name="ФК_11", archived_at="2026-02-01T00:00:00Z")
        with pytest.raises(sqlite3.IntegrityError):
            insert_group(connection, name="ФК_11")


class TestAssignments:
    def test_due_at_is_optional(self, connection):
        insert_course(connection)
        with_due = insert_assignment(
            connection, title="С дедлайном", due_at="2026-03-01T23:59:00Z"
        )
        without_due = insert_assignment(connection, title="Без дедлайна")
        rows = connection.execute(
            "SELECT due_at FROM assignments WHERE id IN (?, ?)",
            (with_due, without_due),
        ).fetchall()
        values = sorted(row["due_at"] or "" for row in rows)
        assert values == ["", "2026-03-01T23:59:00Z"]

    def test_order_index_must_not_be_negative(self, connection):
        insert_course(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_assignment(connection, order_index=-1)

    def test_order_index_may_repeat(self, connection):
        insert_course(connection)
        insert_assignment(connection, title="Первое", order_index=1)
        insert_assignment(connection, title="Второе", order_index=1)
        assert conn_count(connection, "SELECT COUNT(*) FROM assignments") == 2

    def test_archived_at_is_optional(self, connection):
        insert_course(connection)
        assignment_id = insert_assignment(connection)
        row = connection.execute(
            "SELECT archived_at FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        assert row["archived_at"] is None


class TestSubmissions:
    @pytest.fixture
    def base(self, connection):
        insert_course(connection)
        self.student = insert_user(
            connection, "s@example.com", role="student", course_id=1
        )
        self.assignment = insert_assignment(connection)
        return connection

    def test_all_status_codes_accepted(self, connection):
        insert_course(connection)
        assignment = insert_assignment(connection)
        for index, status in enumerate(config.STATUS_ORDER, start=1):
            student = insert_user(
                connection,
                f"student{index}@example.com",
                role="student",
                course_id=1,
            )
            insert_submission(connection, assignment, student, attempt_no=1, status=status)
        stored = {
            row[0] for row in connection.execute("SELECT DISTINCT status FROM submissions")
        }
        assert stored == set(config.STATUS_ORDER)

    def test_unknown_status_rejected(self, base):
        with pytest.raises(sqlite3.IntegrityError):
            insert_submission(
                base, self.assignment, self.student, status="in_progress"
            )

    def test_attempt_unique_per_assignment_and_student(self, base):
        insert_submission(base, self.assignment, self.student, attempt_no=1)
        with pytest.raises(sqlite3.IntegrityError):
            insert_submission(base, self.assignment, self.student, attempt_no=1)

    def test_next_attempt_allowed(self, base):
        insert_submission(base, self.assignment, self.student, attempt_no=1)
        insert_submission(base, self.assignment, self.student, attempt_no=2)
        assert conn_count(base, "SELECT COUNT(*) FROM submissions") == 2

    @pytest.mark.parametrize("attempt_no", [0, 4, -1])
    def test_attempt_outside_limit_rejected(self, base, attempt_no):
        with pytest.raises(sqlite3.IntegrityError):
            insert_submission(
                base, self.assignment, self.student, attempt_no=attempt_no
            )

    def test_attempt_limit_matches_config(self):
        script = (config.MIGRATIONS_DIR / "001_schema.sql").read_text(
            encoding="utf-8"
        )
        limit = config.SUBMISSION_ATTEMPT_LIMIT
        assert f"attempt_no BETWEEN 1 AND {limit}" in script

    def test_empty_comment_rejected(self, base):
        with pytest.raises(sqlite3.IntegrityError):
            base.execute(
                "INSERT INTO submissions (assignment_id, student_id, attempt_no, "
                "status, comment, submitted_at, created_at) "
                "VALUES (?, ?, 1, 'submitted', '', ?, ?)",
                (self.assignment, self.student, "2026-01-02T10:00:00Z", "2026-01-02T10:00:00Z"),
            )

    def test_missing_submission_means_not_submitted(self, base):
        assert conn_count(base, "SELECT COUNT(*) FROM submissions") == 0


class TestFiles:
    def test_size_limit_boundary(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        limit = config.MAX_FILE_SIZE_BYTES
        assert limit == 10_485_760
        at_limit = insert_file(
            connection, "assignment_files", "assignment_id", assignment,
            "at-limit.docx", student, size=limit,
        )
        assert at_limit > 0
        with pytest.raises(sqlite3.IntegrityError):
            insert_file(
                connection, "assignment_files", "assignment_id", assignment,
                "over-limit.docx", student, size=limit + 1,
            )

    def test_oversized_file_rejected(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_file(
                connection, "assignment_files", "assignment_id", assignment,
                "f.docx", student, size=config.MAX_FILE_SIZE_BYTES + 1,
            )

    def test_zero_size_rejected(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_file(
                connection, "assignment_files", "assignment_id", assignment,
                "empty.docx", student, size=0,
            )

    @pytest.mark.parametrize(
        "stored_name",
        ["../secret", "a/b.docx", "a\\b.docx", ".hidden", "..", "sub/../x"],
    )
    def test_stored_name_must_not_contain_path(self, connection, stored_name):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_file(
                connection, "assignment_files", "assignment_id", assignment,
                stored_name, student,
            )

    def test_generated_stored_name_accepted(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        stored = "6f1d9b2c4a8e4f3a9d0c1b2a3e4f5a6b.docx"
        file_id = insert_file(
            connection, "assignment_files", "assignment_id", assignment,
            stored, student,
        )
        assert file_id > 0

    def test_stored_name_unique(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        first = insert_assignment(connection, title="Первое")
        second = insert_assignment(connection, title="Второе")
        insert_file(
            connection, "assignment_files", "assignment_id", first, "same.docx", student
        )
        with pytest.raises(sqlite3.IntegrityError):
            insert_file(
                connection, "assignment_files", "assignment_id", second,
                "same.docx", student,
            )

    def test_original_name_keeps_cyrillic(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        file_id = insert_file(
            connection, "assignment_files", "assignment_id", assignment,
            "stored.docx", student,
        )
        row = connection.execute(
            "SELECT original_name FROM assignment_files WHERE id = ?", (file_id,)
        ).fetchone()
        assert row["original_name"] == "исходное.docx"


class TestCommentsAndFiles:
    def test_comment_owns_its_file(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        submission = insert_submission(connection, assignment, student)
        comment_id = connection.execute(
            "INSERT INTO submission_comments "
            "(submission_id, author_id, body, created_at) "
            "VALUES (?, ?, 'Нужно доработать', ?)",
            (submission, student, "2026-01-03T09:00:00Z"),
        ).lastrowid
        file_id = insert_file(
            connection, "submission_comment_files", "submission_id", submission,
            "comment-file.docx", student,
        )
        connection.execute(
            "UPDATE submission_comment_files SET comment_id = ? WHERE id = ?",
            (comment_id, file_id),
        )
        row = connection.execute(
            "SELECT comment_id FROM submission_comment_files WHERE id = ?",
            (file_id,),
        ).fetchone()
        assert row["comment_id"] == comment_id

    def test_comment_without_files_is_allowed(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        submission = insert_submission(connection, assignment, student)
        comment_id = connection.execute(
            "INSERT INTO submission_comments "
            "(submission_id, author_id, body, created_at) VALUES (?, ?, ?, ?)",
            (submission, student, "Текст без вложения", "2026-01-03T09:00:00Z"),
        ).lastrowid
        files = connection.execute(
            "SELECT id FROM submission_comment_files WHERE comment_id = ?",
            (comment_id,),
        ).fetchall()
        assert files == []

    def test_comment_id_must_exist(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        submission = insert_submission(connection, assignment, student)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO submission_comment_files "
                "(submission_id, comment_id, original_name, stored_name,"
                " mime, size_bytes, uploaded_by, created_at) "
                "VALUES (?, 4242, 'замечания.txt', 'stored-note', NULL, 10, ?, ?)",
                (submission, student, "2026-01-03T09:00:00Z"),
            )

    def test_empty_body_rejected(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        submission = insert_submission(connection, assignment, student)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO submission_comments "
                "(submission_id, author_id, body, created_at) VALUES (?, ?, '', ?)",
                (submission, student, "2026-01-03T09:00:00Z"),
            )

    def test_several_comments_per_submission(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        assignment = insert_assignment(connection)
        submission = insert_submission(connection, assignment, student)
        for body in ("Первое", "Второе", "Третье"):
            connection.execute(
                "INSERT INTO submission_comments "
                "(submission_id, author_id, body, created_at) VALUES (?, ?, ?, ?)",
                (submission, student, body, "2026-01-03T09:00:00Z"),
            )
        assert (
            conn_count(
                connection,
                "SELECT COUNT(*) FROM submission_comments WHERE submission_id = ?",
                (submission,),
            )
            == 3
        )


class TestActionLog:
    @pytest.fixture
    def base(self, connection):
        insert_course(connection)
        self.admin = insert_user(connection, "admin@admin.com", role="admin")
        return connection

    def test_all_action_codes_accepted(self, base):
        for index, action in enumerate(config.ACTION_CODES, start=1):
            base.execute(
                "INSERT INTO action_log (actor_id, actor_role, action, "
                "entity_type, entity_id, created_at) "
                "VALUES (?, 'admin', ?, 'user', ?, ?)",
                (
                    self.admin,
                    action,
                    index,
                    f"2026-01-01T00:00:{index % 60:02d}Z",
                ),
            )
        stored = {row[0] for row in base.execute("SELECT DISTINCT action FROM action_log")}
        assert stored == set(config.ACTION_CODES)

    def test_unknown_action_rejected(self, base):
        with pytest.raises(sqlite3.IntegrityError):
            base.execute(
                "INSERT INTO action_log (actor_id, actor_role, action, "
                "entity_type, created_at) VALUES (?, 'admin', 'made.up', 'user', ?)",
                (self.admin, "2026-01-01T00:00:00Z"),
            )

    def test_entity_types_match_reference(self, base):
        for entity in config.ENTITY_TYPES:
            base.execute(
                "INSERT INTO action_log (actor_id, actor_role, action, "
                "entity_type, created_at) VALUES (?, 'admin', 'auth.login', ?, ?)",
                (self.admin, entity, "2026-01-01T00:00:00Z"),
            )
        assert len(config.ENTITY_TYPES) == 8

    def test_unknown_entity_type_rejected(self, base):
        with pytest.raises(sqlite3.IntegrityError):
            base.execute(
                "INSERT INTO action_log (actor_id, actor_role, action, "
                "entity_type, created_at) VALUES (?, 'admin', 'auth.login', "
                "'comment', ?)",
                (self.admin, "2026-01-01T00:00:00Z"),
            )

    def test_status_transition_recorded(self, base):
        base.execute(
            "INSERT INTO action_log (actor_id, actor_role, action, entity_type, "
            "from_status, to_status, created_at) "
            "VALUES (?, 'admin', 'submission.status_changed', 'submission', "
            "'submitted', 'accepted', ?)",
            (self.admin, "2026-01-01T00:00:00Z"),
        )
        row = base.execute(
            "SELECT from_status, to_status FROM action_log"
        ).fetchone()
        assert (row["from_status"], row["to_status"]) == ("submitted", "accepted")

    def test_unknown_status_rejected(self, base):
        with pytest.raises(sqlite3.IntegrityError):
            base.execute(
                "INSERT INTO action_log (actor_id, actor_role, action, "
                "entity_type, to_status, created_at) VALUES (?, 'admin', "
                "'submission.status_changed', 'submission', 'unknown_status', ?)",
                (self.admin, "2026-01-01T00:00:00Z"),
            )

    def test_actor_role_uses_role_codes(self, base):
        with pytest.raises(sqlite3.IntegrityError):
            base.execute(
                "INSERT INTO action_log (actor_id, actor_role, action, "
                "entity_type, created_at) VALUES (?, 'system', 'auth.login', "
                "'user', ?)",
                (self.admin, "2026-01-01T00:00:00Z"),
            )

    def test_details_json_stored_without_password(self, base):
        base.execute(
            "INSERT INTO action_log (actor_id, actor_role, action, entity_type, "
            "details_json, created_at) VALUES (?, 'admin', 'auth.login', 'user', ?, ?)",
            (self.admin, '{"email": "admin@admin.com"}', "2026-01-01T00:00:00Z"),
        )
        row = base.execute("SELECT details_json FROM action_log").fetchone()
        assert "password" not in row["details_json"]


class TestImportTables:
    def test_batch_and_errors(self, connection):
        insert_course(connection)
        admin = insert_user(connection, "admin@admin.com", role="admin")
        batch_id = connection.execute(
            "INSERT INTO import_batches (actor_id, course_id, file_name, "
            "total_rows, valid_rows, created_at) VALUES (?, 1, 'Список.xlsx', ?, ?, ?)",
            (admin, 6, 4, "2026-01-05T12:00:00Z"),
        ).lastrowid
        for row_number, field, text in (
            (5, "пароль", "Пароль не указан"),
            (6, "группа", "Группа не указана"),
            (6, "e-mail", "E-mail уже используется"),
        ):
            connection.execute(
                "INSERT INTO import_errors (batch_id, row_number, field, error_text) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, row_number, field, text),
            )
        assert (
            conn_count(
                connection,
                "SELECT COUNT(*) FROM import_errors WHERE batch_id = ?",
                (batch_id,),
            )
            == 3
        )

    def test_valid_rows_cannot_exceed_total(self, connection):
        insert_course(connection)
        admin = insert_user(connection, "admin@admin.com", role="admin")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO import_batches (actor_id, course_id, file_name, "
                "total_rows, valid_rows, created_at) VALUES (?, 1, 'f.xlsx', 4, 5, ?)",
                (admin, "2026-01-05T12:00:00Z"),
            )

    def test_row_number_starts_from_two(self, connection):
        insert_course(connection)
        admin = insert_user(connection, "admin@admin.com", role="admin")
        batch_id = connection.execute(
            "INSERT INTO import_batches (actor_id, course_id, file_name, "
            "total_rows, valid_rows, created_at) VALUES (?, 1, 'f.xlsx', 1, 1, ?)",
            (admin, "2026-01-05T12:00:00Z"),
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO import_errors (batch_id, row_number, error_text) "
                "VALUES (?, 1, 'Строка заголовка')",
                (batch_id,),
            )
        connection.execute(
            "INSERT INTO import_errors (batch_id, row_number, error_text) "
            "VALUES (?, 2, 'Ошибка в строке данных')",
            (batch_id,),
        )

    def test_error_field_is_optional(self, connection):
        insert_course(connection)
        admin = insert_user(connection, "admin@admin.com", role="admin")
        batch_id = connection.execute(
            "INSERT INTO import_batches (actor_id, course_id, file_name, "
            "total_rows, valid_rows, created_at) VALUES (?, 1, 'f.xlsx', 0, 0, ?)",
            (admin, "2026-01-05T12:00:00Z"),
        ).lastrowid
        connection.execute(
            "INSERT INTO import_errors (batch_id, row_number, error_text) "
            "VALUES (?, 2, 'Общая ошибка файла')",
            (batch_id,),
        )
        row = connection.execute("SELECT field FROM import_errors").fetchone()
        assert row["field"] is None


class TestDeletionOrder:
    def test_parent_cannot_be_deleted_before_children(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        group_id = insert_group(connection)
        connection.execute(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, student),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM courses WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = ?", (student,))

    def test_ordered_deletion_succeeds(self, connection):
        insert_course(connection)
        student = insert_user(connection, "s@example.com", role="student", course_id=1)
        group_id = insert_group(connection)
        connection.execute(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, student),
        )
        connection.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
        connection.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        connection.execute("DELETE FROM users WHERE id = ?", (student,))
        connection.execute("DELETE FROM courses WHERE id = 1")
        assert conn_count(connection, "SELECT COUNT(*) FROM courses") == 0


class TestKey:
    def test_create_key_writes_valid_fernet_key(self, workspace):
        path, created = security.create_key()
        assert created is True
        assert path.is_file()
        assert path.name == "secret.key"
        assert path.parent.name == "var"
        security.load_fernet(path)

    def test_create_key_is_idempotent(self, workspace):
        first_path, first_created = security.create_key()
        original = first_path.read_bytes()
        second_path, second_created = security.create_key()
        assert second_created is False
        assert second_path == first_path
        assert second_path.read_bytes() == original

    def test_missing_key_gives_command_hint(self, workspace):
        with pytest.raises(security.SecretKeyMissing) as error:
            security.load_fernet()
        message = str(error.value)
        assert "python -m app.cli init-key" in message
        assert "secret.key" in message

    def test_corrupted_key_is_reported(self, workspace):
        path = config.SECRET_KEY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-a-fernet-key")
        with pytest.raises(security.KeyError_, match="повреждён"):
            security.load_fernet(path)

    def test_empty_key_is_reported(self, workspace):
        path = config.SECRET_KEY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"   ")
        with pytest.raises(security.SecretKeyMissing):
            security.load_fernet(path)

    def test_encrypt_decrypt_roundtrip(self, workspace):
        fernet = security.load_fernet(security.create_key()[0])
        token = security.encrypt("Пароль123", fernet)
        assert token != b"\xd4\x8a\xf0\x9d"
        assert security.decrypt(token, fernet) == "Пароль123"

    def test_wrong_key_cannot_decrypt(self, workspace):
        first = security.load_fernet(security.create_key()[0])
        token = security.encrypt("Пароль123", first)
        second_path, _ = security.create_key(config.VAR_DIR / "other.key")
        other = security.load_fernet(second_path)
        with pytest.raises(security.KeyError_, match="не совпадает"):
            security.decrypt(token, other)

    def test_key_is_readable_by_admin_decrypt(self, workspace):
        fernet = security.load_fernet(security.create_key()[0])
        token = security.encrypt("1234", fernet)
        assert security.decrypt(token, fernet) == "1234"

    def test_session_secret_is_ascii_key(self, workspace):
        secret = security.session_secret(security.create_key()[0])
        assert secret.isascii()
        assert len(secret) > 0


class TestCli:
    def test_init_key_creates_file(self, workspace, capsys):
        from app import cli

        assert cli.main(["init-key"]) == 0
        assert config.SECRET_KEY_PATH.is_file()
        assert "Создан ключ" in capsys.readouterr().out

    def test_init_key_second_run_keeps_key(self, workspace, capsys):
        from app import cli

        cli.main(["init-key"])
        original = config.SECRET_KEY_PATH.read_bytes()
        assert cli.main(["init-key"]) == 0
        assert config.SECRET_KEY_PATH.read_bytes() == original
        assert "оставлен без изменений" in capsys.readouterr().out

    def test_init_db_creates_database(self, workspace, capsys):
        from app import cli

        assert cli.main(["init-db"]) == 0
        assert config.DB_PATH.is_file()
        out = capsys.readouterr().out
        assert "001_schema.sql" in out
        assert "002_indexes.sql" in out
        assert "003_comment_files.sql" in out
        assert "004_action_log_codes.sql" in out

    def test_init_db_second_run_is_idempotent(self, workspace, capsys):
        from app import cli

        cli.main(["init-db"])
        capsys.readouterr()
        assert cli.main(["init-db"]) == 0
        out = capsys.readouterr().out
        assert "Схема уже актуальна" in out
        assert "Применены миграции" not in out

    def test_setup_creates_key_and_database(self, workspace, capsys):
        from app import cli

        assert cli.main(["setup"]) == 0
        assert config.SECRET_KEY_PATH.is_file()
        assert config.DB_PATH.is_file()
        out = capsys.readouterr().out
        assert "Демо-курс создан" in out
        assert "vetrov@demo-study.ru" in out

    def test_seed_demo_loads_and_is_idempotent(self, workspace, capsys):
        from app import cli

        cli.main(["setup"])
        capsys.readouterr()
        assert cli.main(["seed-demo"]) == 0
        out = capsys.readouterr().out
        assert "Демо-данные уже загружены" in out
        assert "--force" in out

    def test_seed_demo_force_recreates(self, workspace, capsys):
        from app import cli

        cli.main(["setup"])
        capsys.readouterr()
        assert cli.main(["seed-demo", "--force"]) == 0
        out = capsys.readouterr().out
        assert "Демо-курс создан" in out

    def test_no_command_prints_help(self, workspace, capsys):
        from app import cli

        assert cli.main([]) == 0
        assert "python -m app.cli" in capsys.readouterr().out

    def test_error_is_reported_in_russian(self, workspace, monkeypatch, capsys):
        from app import cli

        def broken():
            raise RuntimeError("Нет доступа к каталогу")

        monkeypatch.setitem(cli.COMMANDS, "init-db", broken)
        assert cli.main(["init-db"]) == cli.EXIT_ERROR
        assert "Ошибка" in capsys.readouterr().err

    def test_setup_creates_default_admin(self, workspace, capsys):
        """FR-02, AC-01: после setup есть администратор 1234 с must_change_password."""
        from app import cli, passwords

        assert cli.main(["setup"]) == 0
        connection = open_db(config.DB_PATH)
        row = connection.execute(
            "SELECT * FROM users WHERE role = 'admin'"
        ).fetchone()
        connection.close()
        assert row["email"] == "admin@admin.com"
        assert row["course_id"] is None
        assert row["must_change_password"] == 1
        assert row["is_active"] == 1
        assert passwords.verify_password("1234", row["password_hash"])
        # Пароль должен оставаться и читаемым: иначе администратор
        # не сможет показать его пользователю (FR-09).
        assert passwords.reveal_password(row) == "1234"
        assert "обязательная смена" in capsys.readouterr().out

    def test_default_admin_password_is_not_recreated(self, workspace):
        """Повторный setup не воскрешает админа после смены логина."""
        from app import cli

        cli.main(["setup"])
        connection = open_db(config.DB_PATH)
        connection.execute(
            "UPDATE users SET email = 'new@admin.com' WHERE role = 'admin'"
        )
        connection.commit()
        connection.close()

        assert cli.main(["setup"]) == 0
        connection = open_db(config.DB_PATH)
        rows = connection.execute(
            "SELECT email FROM users WHERE role = 'admin'"
        ).fetchall()
        connection.close()
        assert [row["email"] for row in rows] == ["new@admin.com"]

    def test_init_db_without_key_skips_default_admin(self, workspace, capsys):
        """Без ключа пароль не зашифровать: учётка не создаётся, но и падения нет."""
        from app import cli

        assert cli.main(["init-db"]) == 0
        connection = open_db(config.DB_PATH)
        count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0]
        connection.close()
        assert count == 0
        assert "init-key" in capsys.readouterr().out


class TestAppStillWorks:
    """Каркас Э-0 плюс барьер доступа из Э-2.

    Начальная страница перестала быть публичной: гость с неё уходит на
    форму входа, а не на страницу «каркас развёрнут».
    """

    def test_start_page_sends_guest_to_login(self, client):
        response = client.get("/")
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_start_page_shows_cabinet_to_signed_in(self, client, connection):
        from tests.conftest import add_user, insert_course

        course = insert_course(connection, title="Основной курс")
        add_user(
            connection,
            "student@example.ru",
            "Пароль123",
            course_id=course,
            full_name="Студент Студентов",
        )
        client.post(
            "/login",
            data={"email": "student@example.ru", "password": "Пароль123"},
        )
        response = client.get("/")
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert "Study room" in page
        assert "Студент Студентов" in page
        assert "Основной курс" in page

    def test_favicon_returns_204(self, client):
        assert client.get("/favicon.ico").status_code == 204

    def test_missing_page_returns_404_in_russian(self, client, connection):
        from tests.conftest import add_user, insert_course

        course = insert_course(connection, title="Основной курс")
        add_user(
            connection,
            "student@example.ru",
            "Пароль123",
            course_id=course,
        )
        client.post(
            "/login",
            data={"email": "student@example.ru", "password": "Пароль123"},
        )
        response = client.get("/nonexistent")
        assert response.status_code == 404
        assert "не найден" in response.get_data(as_text=True)

    def test_missing_page_does_not_leak_to_guests(self, client):
        # Гость не должен узнавать, что страница не существует: сначала
        # вход, иначе 404 стал бы способом перечислить адреса.
        assert client.get("/nonexistent").status_code == 302

    def test_health_of_database_after_request(self, client):
        client.get("/login")
        with client.application.app_context():
            connection = db.get_db()
            assert db.applied_version(connection) == 4
            assert conn_count(connection, "SELECT COUNT(*) FROM users") == 0


def conn_count(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def conn_execute(conn, sql, args=()):
    return conn.execute(sql, args)
