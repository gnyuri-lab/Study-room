"""Демо-данные: состав курса, статусы и совместимость с папкой Inputs.

Проверяются требования этапа Э-9: FR-65 (состав демо-курса), FR-66
(дедлайн первого задания и материалы), FR-67 и AC-42 (вымышленные
студенты, не совпадающие с реестрами ``Inputs``), идемпотентность
``seed-demo`` и то, что ростеры из ``Inputs/Демо-данные`` загружаются
штатным импортом (задача ручной проверки заказчика).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import config, passwords
from app.services import roster_validation
from tests.conftest import insert_course

DEMO_ROSTER_DIR = config.BASE_DIR / "Inputs" / "Демо-данные"
FC_REGISTERS = (
    "Список студентов-ФК_11.xlsx",
    "Список студентов-ФК_21.xlsx",
)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def demo(app, connection):
    from app import seed
    from app.cli import ensure_default_admin

    ensure_default_admin(config.DB_PATH)
    with app.app_context():
        result = seed.seed_demo()
    return result


def _utc(moment: str | None) -> datetime | None:
    if moment is None:
        return None
    return datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


class TestDemoComposition:
    def test_summary_reports_created(self, demo):
        assert demo["status"] == "created"
        assert demo["groups"] == 2
        assert demo["students"] == 10
        assert demo["curators"] == 1
        assert demo["assignments"] == 4
        assert demo["submissions"] == 33
        assert demo["not_submitted"] == 9

    def test_course_groups_and_students(self, demo, connection):
        from app import seed

        course = connection.execute(
            "SELECT * FROM courses WHERE id = ?", (demo["course_id"],)
        ).fetchone()
        title, _description = seed._read_course_file()
        assert course["title"] == title
        assert course["description"]

        groups = connection.execute(
            "SELECT id, name FROM groups WHERE course_id = ? ORDER BY id",
            (demo["course_id"],),
        ).fetchall()
        assert [group["name"] for group in groups] == list(
            seed.DEMO_GROUP_NAMES
        )
        for group in groups:
            students = connection.execute(
                """
                SELECT u.email FROM group_members gm
                JOIN users u ON u.id = gm.user_id
                WHERE gm.group_id = ? AND u.role = 'student'
                ORDER BY u.id
                """,
                (group["id"],),
            ).fetchall()
            assert len(students) == 5

    def test_curator_attached_to_both_groups(self, demo, connection):
        curator = connection.execute(
            "SELECT id, email FROM users WHERE role = 'curator'"
        ).fetchone()
        attached = connection.execute(
            "SELECT group_id FROM curator_groups WHERE user_id = ?"
            " ORDER BY group_id",
            (curator["id"],),
        ).fetchall()
        assert [row["group_id"] for row in attached] == [1, 2]
        assert curator["email"] == "curator@demo-study.ru"

    def test_students_have_usable_passwords(self, demo, connection):
        students = connection.execute(
            "SELECT * FROM users WHERE role = 'student'"
        ).fetchall()
        assert len(students) == 10
        for student in students:
            assert student["must_change_password"] == 0
            assert passwords.verify_password("Demo1234", student["password_hash"])
            assert passwords.reveal_password(student) == "Demo1234"


class TestAssignmentMaterials:
    def test_assignments_in_order_with_files(self, demo, connection):
        rows = connection.execute(
            """
            SELECT id, title, due_at, archived_at, order_index
            FROM assignments
            WHERE course_id = ? ORDER BY order_index
            """,
            (demo["course_id"],),
        ).fetchall()
        assert len(rows) == 4
        assert all(row["archived_at"] is None for row in rows)

        files = connection.execute(
            """
            SELECT af.original_name, a.order_index FROM assignment_files af
            JOIN assignments a ON a.id = af.assignment_id
            ORDER BY a.order_index, af.id
            """
        ).fetchall()
        by_assignment: dict[int, list[str]] = {}
        for row in files:
            by_assignment.setdefault(row["order_index"], []).append(
                row["original_name"]
            )
        indexes = [row["order_index"] for row in rows]
        assert by_assignment.get(indexes[0]) == ["ДЗ1_DCF.xlsx"]
        assert by_assignment.get(indexes[1]) == ["ДЗ2_Multiples.xlsx"]
        assert by_assignment.get(indexes[2]) == ["ДЗ3_Capitalization.xlsx"]
        assert indexes[3] not in by_assignment

    def test_first_deadline_is_in_the_past(self, demo, connection):
        due_dates = [
            row["due_at"]
            for row in connection.execute(
                "SELECT due_at FROM assignments ORDER BY order_index"
            )
        ]
        now = datetime.now(timezone.utc)
        assert _utc(due_dates[0]) < now
        assert _utc(due_dates[1]) > now
        assert due_dates[2] is None
        assert _utc(due_dates[3]) > now

    def test_descriptions_loaded_from_demo_folder(self, demo, connection):
        descriptions = connection.execute(
            "SELECT description FROM assignments ORDER BY order_index"
        ).fetchall()
        assert all(row["description"] for row in descriptions)


class TestStatuses:
    def test_all_five_statuses_are_present(self, demo, connection):
        counts = {
            row["status"]: row["c"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS c FROM submissions GROUP BY status"
            )
        }
        assert set(counts) == {"submitted", "revision", "accepted", "accepted_late"}
        assert counts["submitted"] > 0
        assert counts["revision"] > 0
        assert counts["accepted"] > 0
        assert counts["accepted_late"] > 0
        assert demo["not_submitted"] > 0

    def test_late_status_only_on_the_past_deadline(self, demo, connection):
        rows = connection.execute(
            """
            SELECT a.due_at FROM submissions s
            JOIN assignments a ON a.id = s.assignment_id
            WHERE s.status = 'accepted_late'
            """
        ).fetchall()
        assert rows
        now = datetime.now(timezone.utc)
        assert all(_utc(row["due_at"]) < now for row in rows)

    def test_attempts_and_dialogue_exist(self, demo, connection):
        retried = connection.execute(
            """
            SELECT assignment_id, student_id, COUNT(*) AS attempts
            FROM submissions GROUP BY assignment_id, student_id
            HAVING attempts > 1
            """
        ).fetchall()
        assert len(retried) == 2
        comments = connection.execute(
            "SELECT COUNT(*) FROM submission_comments"
        ).fetchone()[0]
        assert comments >= 20
        student_comments = connection.execute(
            "SELECT COUNT(*) FROM submissions WHERE comment <> ''"
        ).fetchone()[0]
        assert student_comments == demo["submissions"]

    def test_journal_covers_demo_actions(self, demo, connection):
        actions = {
            row["action"]
            for row in connection.execute("SELECT DISTINCT action FROM action_log")
        }
        for action in (
            "course.created",
            "group.created",
            "curator.attached",
            "group.member_added",
            "assignment.created",
            "submission.created",
            "submission.status_changed",
        ):
            assert action in actions


class TestIdempotency:
    def test_second_run_reports_existing(self, demo, app, connection):
        from app import seed

        with app.app_context():
            again = seed.seed_demo()
        assert again["status"] == "exists"
        assert again["course_id"] == demo["course_id"]
        courses = connection.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
        assert courses == 1

    def test_force_recreates_demo_course(self, demo, app, connection):
        from app import seed

        with app.app_context():
            result = seed.seed_demo(force=True)
        assert result["status"] == "created"
        assert result["students"] == 10
        assert result["assignments"] == 4
        assert result["submissions"] == 33
        courses = connection.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
        assert courses == 1
        deleted = connection.execute(
            "SELECT COUNT(*) FROM action_log WHERE action = 'course.deleted'"
        ).fetchone()[0]
        assert deleted == 1


class TestDemoRosters:
    def _rosters(self):
        return sorted(DEMO_ROSTER_DIR.glob("Список студентов-Демо_*.xlsx"))

    def test_roster_files_exist(self):
        assert len(self._rosters()) == 2

    def test_roster_rows_all_qualify(self, demo, connection):
        course_id = insert_course(connection, title="Новый курс для импорта")
        for path in self._rosters():
            roster = roster_validation.read_roster(path, file_name=path.name)
            verdicts = roster_validation.qualify(
                roster, course_id=course_id, connection=connection
            )
            assert len(verdicts) == 5
            for verdict in verdicts:
                assert verdict["errors"] == []
                assert verdict["accepted"] is True
                assert verdict["values"]["password"] == "Demo1234"

    def test_roster_emails_match_seed_students(self, demo, connection):
        roster_emails = set()
        for path in self._rosters():
            roster = roster_validation.read_roster(path, file_name=path.name)
            email_column = roster["header"]["email"]
            for row in roster["rows"]:
                roster_emails.add(row["cells"][email_column])
        seed_emails = {
            row["email"]
            for row in connection.execute(
                "SELECT email FROM users WHERE role = 'student'"
            )
        }
        assert roster_emails == seed_emails


class TestDemoAccounts:
    def test_students_and_curator_can_log_in(self, demo, client):
        for email in ("vetrov@demo-study.ru", "curator@demo-study.ru"):
            response = client.post(
                "/login",
                data={"email": email, "password": "Demo1234"},
                follow_redirects=True,
            )
            assert response.status_code == 200
            assert "Неверный пароль" not in response.text
            assert "кабинет" in response.text.lower()
            client.post("/logout")

    def test_default_admin_is_forced_to_change_credentials(
        self, demo, client
    ):
        response = client.post(
            "/login",
            data={
                "email": config.DEFAULT_ADMIN_EMAIL,
                "password": config.DEFAULT_ADMIN_PASSWORD,
            },
            follow_redirects=True,
        )
        assert "Смените логин и пароль" in response.text


class TestDemoNotFromRealRegisters:
    def test_demo_students_absent_from_fc_registers(self, demo, connection):
        from openpyxl import load_workbook

        registered_names: set[str] = set()
        registered_emails: set[str] = set()
        for name in FC_REGISTERS:
            path = config.BASE_DIR / "Inputs" / name
            sheet = load_workbook(path, data_only=True).active
            rows = list(sheet.iter_rows(values_only=True))
            header = [str(cell or "").strip().lower() for cell in rows[0]]
            name_index = header.index("фио")
            email_index = header.index("e-mail")
            for row in rows[1:]:
                if row[name_index]:
                    registered_names.add(str(row[name_index]).strip())
                if row[email_index]:
                    registered_emails.add(str(row[email_index]).strip().lower())

        demo_names = {
            row["full_name"]
            for row in connection.execute(
                "SELECT full_name FROM users WHERE role = 'student'"
            )
        }
        demo_emails = {
            row["email"]
            for row in connection.execute(
                "SELECT email FROM users WHERE role = 'student'"
            )
        }
        assert demo_names.isdisjoint(registered_names)
        assert {email.lower() for email in demo_emails}.isdisjoint(
            registered_emails
        )
