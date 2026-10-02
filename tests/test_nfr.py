"""Нефункциональные требования NFR-01…NFR-14 (этап Э-9).

Браузерные пункты (контраст текста 4.5:1, клавиатурная навигация,
проба на реальной ширине 360 px и четыре браузера NFR-02) закрываются
вручную по разделу 15 чек-листа; здесь — всё, что воспроизводится
текстом, исходниками, базой и замерами времени: адаптивная вёрстка
(NFR-01), отсутствие сетевых модулей и внешних адресов (NFR-03,
NFR-04, NFR-14), длительность отрисовки (NFR-05), отказы на выход
за каталог файлов (NFR-06), хранение паролей и ключа (NFR-07),
секреты в репозитории (NFR-08), чувствительные поля (NFR-09),
покрытие бизнес-правил (NFR-10), подписанные поля и заголовки
(NFR-11), команды в README (NFR-12) и русский язык (NFR-13).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from app import config, passwords
from app.services import ServiceError
from tests.test_schema import FORBIDDEN_COLUMN_FRAGMENTS

URL_RE = re.compile(r"https?://", re.I)
LOOPBACK_RE = re.compile(r"127\.0\.0\.1|localhost", re.I)
NETWORK_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+(?:requests|httpx|urllib|http|ftplib|smtplib|socket)\b",
    re.M,
)
CONTROL_RE = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.I)
MEDIA_WIDTH_RE = re.compile(r"@media\s*\(\s*max-width:\s*(\d+)px\s*\)")
H1_RE = re.compile(r"<h1[^>]*>\s*\S", re.I)


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


def _web_files() -> list[Path]:
    files: list[Path] = []
    for base in (config.APP_DIR / "templates", config.APP_DIR / "static"):
        files.extend(
            path
            for path in base.rglob("*")
            if path.suffix in {".html", ".css", ".js"}
        )
    return files


def _python_files() -> list[Path]:
    return sorted(config.APP_DIR.rglob("*.py"))


def _unlabeled_controls(html: str) -> list[str]:
    """Поля формы без подписи: aria, label[for] или вложенный label."""
    problems: list[str] = []
    label_for = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', html, re.I))
    for match in CONTROL_RE.finditer(html):
        tag, attrs = match.group(1).lower(), match.group(2)
        if tag == "input" and re.search(r'type="hidden"', attrs, re.I):
            continue
        lowered = attrs.lower()
        if "aria-label" in lowered or "aria-labelledby" in lowered:
            continue
        id_match = re.search(r'\bid="([^"]+)"', attrs, re.I)
        if id_match and id_match.group(1) in label_for:
            continue
        before = html[: match.start()]
        if before.rfind("<label") > before.rfind("</label>"):
            continue
        problems.append(f"<{tag} {attrs.strip()[:80]}>")
    return problems


class TestNfr01Responsive:
    """NFR-01: адаптив от 360 px — viewport и медиазапросы на малых ширинах."""

    def test_viewport_meta_present(self):
        base = (config.APP_DIR / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        assert 'width=device-width' in base

    def test_css_covers_small_screens(self):
        css = (config.APP_DIR / "static" / "css" / "app.css").read_text(
            encoding="utf-8"
        )
        widths = [int(value) for value in MEDIA_WIDTH_RE.findall(css)]
        assert widths, "в app.css нет медиазапросов max-width"
        assert min(widths) <= 600, "самый крупный порог не ниже 600 px"


class TestNfr03NoNetwork:
    """NFR-03: приложение не выполняет сетевых запросов."""

    def test_python_sources_import_no_network_clients(self):
        offenders: list[str] = []
        for path in _python_files():
            text = path.read_text(encoding="utf-8")
            if NETWORK_IMPORT_RE.search(text):
                offenders.append(path.name)
        assert offenders == []


class TestNfr04NoExternalResources:
    """NFR-04: ни одного внешнего адреса в шаблонах и статике."""

    def test_templates_and_static_have_no_external_urls(self):
        offenders: list[str] = []
        for path in _web_files():
            text = path.read_text(encoding="utf-8")
            if URL_RE.search(text):
                offenders.append(path.name)
        assert offenders == []


class TestNfr05Performance:
    """NFR-05: дашборд — до 2 с, карточка задания — до 1 с."""

    def test_curator_dashboard_renders_within_two_seconds(
        self, demo, client
    ):
        from app import seed

        client.post(
            "/login",
            data={"email": seed.CURATOR[1], "password": seed.DEMO_PASSWORD},
        )
        start = time.perf_counter()
        response = client.get("/curator/dashboard")
        elapsed = time.perf_counter() - start
        assert response.status_code == 200
        assert elapsed < 2.0, f"дашборд отрисован за {elapsed:.2f} с"

    def test_assignment_card_renders_within_one_second(
        self, demo, connection, client
    ):
        from app import seed

        assignment = connection.execute(
            "SELECT course_id, id FROM assignments ORDER BY id LIMIT 1"
        ).fetchone()
        client.post(
            "/login",
            data={"email": "vetrov@demo-study.ru", "password": seed.DEMO_PASSWORD},
        )
        start = time.perf_counter()
        response = client.get(
            "/student/courses/{0}/assignments/{1}".format(
                assignment["course_id"], assignment["id"]
            )
        )
        elapsed = time.perf_counter() - start
        assert response.status_code == 200
        assert elapsed < 1.0, f"карточка отрисована за {elapsed:.2f} с"


class TestNfr06PathSafety:
    """NFR-06: выход за пределы каталога файлов запрещён."""

    def test_staging_and_stored_paths_reject_traversal(self):
        from app import attachments

        with pytest.raises(ServiceError):
            attachments.staging_path("../escape.txt")
        with pytest.raises(ServiceError):
            attachments.stored_path("..\\escape.txt")

    def test_original_name_strips_directories(self):
        from app import attachments

        assert attachments.original_name("../../evil.txt") == "evil.txt"
        assert attachments.original_name("..\\evil.txt") == "evil.txt"

    def test_demo_stored_names_are_generated_tokens(self, demo, connection):
        rows = connection.execute(
            "SELECT stored_name, original_name FROM submission_files"
        ).fetchall()
        assert rows
        for row in rows:
            assert "/" not in row["stored_name"]
            assert "\\" not in row["stored_name"]
            assert ".." not in row["stored_name"]


class TestNfr07PasswordStorage:
    """NFR-07: пароли зашифрованы, ключ лежит в var/."""

    def test_demo_password_not_stored_in_clear_text(self, demo):
        data = config.DB_PATH.read_bytes()
        assert b"Demo1234" not in data

    def test_password_hash_differs_from_password(self, demo, connection):
        row = connection.execute(
            "SELECT password_hash, password_enc FROM users WHERE role = 'student'"
        ).fetchone()
        assert row["password_hash"] != "Demo1234"
        assert row["password_hash"].startswith("pbkdf2")
        assert row["password_enc"]

    def test_key_lives_in_var_outside_data(self, app):
        key = config.SECRET_KEY_PATH
        assert key.exists()
        assert key.is_relative_to(config.VAR_DIR)
        assert not key.is_relative_to(config.DATA_DIR)

    def test_action_log_has_no_passwords(self, demo, connection):
        rows = connection.execute("SELECT details_json FROM action_log").fetchall()
        for row in rows:
            assert "Demo1234" not in (row["details_json"] or "")


class TestNfr08NoSecretsInRepository:
    """NFR-08: var/, data/ и служебные файлы вне репозитория."""

    def test_gitignore_excludes_working_directories(self):
        text = (config.BASE_DIR / ".gitignore").read_text(encoding="utf-8")
        lines = {line.strip() for line in text.splitlines()}
        assert "var/" in lines
        assert "data/" in lines
        assert ".playwright-mcp/" in lines

    def test_no_secret_files_outside_var(self):
        ignored = {
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            ".pytest_cache",
            ".idea",
            ".vscode",
            "var",
            "data",
            ".playwright-mcp",
        }
        found: list[str] = []
        for path in config.BASE_DIR.rglob("*"):
            if not path.is_file():
                continue
            if ignored & set(path.relative_to(config.BASE_DIR).parts):
                continue
            if path.name in {"secret.key", ".env"}:
                found.append(str(path.relative_to(config.BASE_DIR)))
        assert found == []


class TestNfr09NoSensitiveFields:
    """NFR-09: в схеме нет паспортных, платёжных и иных чувствительных полей."""

    def test_schema_has_no_forbidden_column_fragments(self, connection):
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        assert tables
        checked = 0
        for table in tables:
            columns = connection.execute(
                "PRAGMA table_info({0})".format(table["name"])
            ).fetchall()
            for column in columns:
                lowered = column["name"].lower()
                for fragment in FORBIDDEN_COLUMN_FRAGMENTS:
                    assert fragment not in lowered, (
                        "{0}.{1} похоже на чувствительное поле".format(
                            table["name"], column["name"]
                        )
                    )
                checked += 1
        assert checked > 50


class TestNfr10RuleCoverage:
    """NFR-10: автотесты упоминают все BR-01…BR-38 и команду pytest."""

    def test_every_business_rule_is_referenced_in_tests(self):
        corpus = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((config.BASE_DIR / "tests").glob("*.py"))
        )
        missing = [
            "BR-{0:02d}".format(index)
            for index in range(1, 39)
            if "BR-{0:02d}".format(index) not in corpus
        ]
        assert missing == []

    def test_pytest_is_the_single_test_command(self):
        text = (config.BASE_DIR / "README.md").read_text(encoding="utf-8")
        assert "pytest -q" in text
        assert "tox" not in text


class TestNfr11AccessiblePages:
    """NFR-11: подписанные поля и заголовок на каждой странице.

    Контраст 4.5:1 и клавиатурная навигация проверяются вручную
    (раздел 15 чек-листа).
    """

    def test_every_control_has_a_label(self):
        problems: list[str] = []
        for path in sorted((config.APP_DIR / "templates").rglob("*.html")):
            html = path.read_text(encoding="utf-8")
            for item in _unlabeled_controls(html):
                problems.append(f"{path.name}: {item}")
        assert problems == []

    def test_key_pages_render_a_heading(self, demo, client):
        from app import seed

        response = client.get("/login")
        assert response.status_code == 200
        assert H1_RE.search(response.text), "на странице входа нет заголовка"

        client.post(
            "/login",
            data={"email": seed.CURATOR[1], "password": seed.DEMO_PASSWORD},
        )
        response = client.get("/curator/dashboard")
        assert H1_RE.search(response.text), "на дашборде нет заголовка"

        client.post("/logout")
        client.post(
            "/login",
            data={"email": "vetrov@demo-study.ru", "password": seed.DEMO_PASSWORD},
        )
        response = client.get("/student/courses")
        assert H1_RE.search(response.text), "на списке курсов нет заголовка"

    def test_base_has_skip_link(self):
        base = (config.APP_DIR / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        assert 'class="skip-link"' in base


class TestNfr12Documentation:
    """NFR-12: установка и запуск описаны в README без Docker и облаков."""

    def test_commands_from_section_12_2_are_documented(self):
        text = (config.BASE_DIR / "README.md").read_text(encoding="utf-8")
        for needle in (
            "python -m venv",
            "pip install -r requirements.txt",
            "python -m app.cli setup",
            "flask --app app run",
            "pytest -q",
        ):
            assert needle in text, f"в README нет команды: {needle}"
        assert "docker" not in text.lower()


class TestNfr13RussianLanguage:
    """NFR-13: интерфейс, сообщения и документация на русском."""

    def test_texts_contain_cyrillic(self):
        cyrillic = re.compile("[Ѐ-ӿ]")

        assert cyrillic.search(config.BASE_DIR.joinpath("README.md").read_text("utf-8"))
        for key, value in config.MESSAGES.items():
            assert cyrillic.search(value), f"сообщение {key} без кириллицы"
        for key, value in config.EMPTY_STATES.items():
            assert cyrillic.search(value), f"пустое состояние {key} без кириллицы"
        for key, status in config.STATUSES.items():
            assert cyrillic.search(status["label"]), f"статус {key} без кириллицы"
        for key, role in config.ROLES.items():
            assert cyrillic.search(role["label"]), f"роль {key} без кириллицы"

    def test_html_lang_is_russian(self):
        base = (config.APP_DIR / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        assert 'lang="ru"' in base


class TestNfr14NoIntegrations:
    """NFR-14: нет внешних сервисов — ни адресов, ни лишних пакетов."""

    def test_python_sources_have_no_external_addresses(self):
        offenders: list[str] = []
        for path in _python_files():
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if URL_RE.search(line) and not LOOPBACK_RE.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert offenders == []

    def test_requirements_hold_exactly_the_allowed_packages(self):
        allowed = {"flask", "openpyxl", "filetype", "cryptography", "pytest"}
        listed = {
            re.split(r"[=<>]", line.strip(), maxsplit=1)[0].lower()
            for line in (config.BASE_DIR / "requirements.txt").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip() and not line.startswith("#")
        }
        assert listed == allowed
