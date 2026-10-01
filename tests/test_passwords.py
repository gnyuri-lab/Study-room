"""Пароли: хеш, шифрование, квалификация (BR-08, BR-10, BR-11, BR-12, FR-09)."""

import pytest

from app import config, passwords

#: Читается до подмены: проверка ниже должна видеть боевые итерации,
#: а не уменьшенные ради скорости.
PRODUCTION_ITERATIONS = passwords.HASH_ITERATIONS


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """Уменьшить число итераций: 240 000 на каждый тест слишком дорого.

    Тесты формата и константы ниже проверяют, что в продакшене
    итераций по-прежнему много, — подмена их не касается.
    """
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


# --------------------------------------------------------------------------
# Хеширование
# --------------------------------------------------------------------------

def test_hash_and_verify_roundtrip():
    stored = passwords.hash_password("Пароль123")
    assert passwords.verify_password("Пароль123", stored)
    assert not passwords.verify_password("Пароль124", stored)


def test_hash_is_not_the_password():
    stored = passwords.hash_password("Пароль123")
    assert "Пароль123" not in stored


def test_salt_makes_hashes_differ():
    first = passwords.hash_password("Пароль123")
    second = passwords.hash_password("Пароль123")
    assert first != second
    assert passwords.verify_password("Пароль123", second)


def test_production_iteration_count_is_recorded():
    stored = passwords.hash_password("Пароль123", iterations=240_000)
    assert stored.startswith("pbkdf2_sha256$240000$")
    assert passwords.verify_password("Пароль123", stored)


def test_default_iterations_match_module_constant():
    assert PRODUCTION_ITERATIONS >= 100_000
    assert passwords.HASH_SCHEME == "pbkdf2_sha256"


def test_verify_reads_iterations_from_the_hash():
    slow = passwords.hash_password("Пароль123", iterations=2_000)
    assert passwords.verify_password("Пароль123", slow)


@pytest.mark.parametrize(
    "broken",
    [
        "",
        "нехеш",
        "pbkdf2_sha256$1000$zz$ff",
        "pbkdf2_sha256$1000$00",
        "pbkdf2_sha256$1000$00$00$00",
        "md5$1000$00$00",
    ],
)
def test_broken_hash_is_false_not_exception(broken):
    assert passwords.verify_password("Пароль123", broken) is False


def test_empty_password_cannot_be_hashed():
    with pytest.raises(ValueError):
        passwords.hash_password("")


# --------------------------------------------------------------------------
# Шифрование
# --------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip(key_path):
    token = passwords.encrypt_password("Пароль123")
    assert token != "Пароль123".encode("utf-8")
    assert passwords.decrypt_password(token) == "Пароль123"


def test_decrypt_requires_a_token():
    with pytest.raises(ValueError):
        passwords.decrypt_password(b"")


def test_reveal_password_uses_stored_value(key_path, connection, course):
    from tests.conftest import add_user

    user_id = add_user(connection, "s1@example.ru", "Пароль123", course_id=course)
    row = connection.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    assert passwords.reveal_password(row) == "Пароль123"


def test_build_credentials_fills_both_fields(key_path):
    encrypted, hashed = passwords.build_credentials("Пароль123", "student")
    assert passwords.decrypt_password(encrypted) == "Пароль123"
    assert passwords.verify_password("Пароль123", hashed)


# --------------------------------------------------------------------------
# Квалификация длины
# --------------------------------------------------------------------------


def test_min_length_per_role():
    assert passwords.min_length_for("admin") == 8
    assert passwords.min_length_for("curator") == 5
    assert passwords.min_length_for("student") == 5
    assert passwords.min_length_for("неизвестная") == (
        config.PASSWORD_MIN_LENGTH_DEFAULT
    )


def test_short_admin_password_gets_admin_text():
    error = passwords.validate_password("1234567", "admin")
    assert error == config.MESSAGES["admin_password_short"]


def test_short_student_password_gets_user_text():
    error = passwords.validate_password("1234", "student")
    assert error == config.MESSAGES["user_password_short"]


def test_password_of_exact_minimum_length_is_allowed():
    assert passwords.validate_password("12345", "student") is None
    assert passwords.validate_password("12345678", "admin") is None


def test_confirmation_must_match():
    error = passwords.validate_password_confirmation(
        "Пароль123", "Пароль124", "student"
    )
    assert error == config.MESSAGES["passwords_mismatch"]


def test_length_is_checked_before_matching():
    error = passwords.validate_password_confirmation("123", "124", "student")
    assert error == config.MESSAGES["user_password_short"]


# --------------------------------------------------------------------------
# Квалификация e-mail
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "student@example.ru",
        "a.b+c@sub.domain.co.uk",
        "пример@пример.рф",
    ],
)
def test_valid_emails(email):
    assert passwords.is_valid_email(email)
    assert passwords.validate_email(email) is None


@pytest.mark.parametrize(
    "email",
    [
        "",
        "   ",
        "student",
        "student@",
        "@example.ru",
        "student@example",
        "student@.ru",
        "student@example.",
        "student@example.r",
        "a@b@example.ru",
        "stu dent@example.ru",
        ("a" * 250) + "@example.ru",
    ],
)
def test_invalid_emails(email):
    assert passwords.is_valid_email(email) is False
    assert passwords.validate_email(email) == config.MESSAGES["email_invalid"]


def test_email_is_trimmed():
    assert passwords.normalise_email("  student@example.ru  ") == (
        "student@example.ru"
    )
    assert passwords.is_valid_email(" student@example.ru ")


# --------------------------------------------------------------------------
# Генерация и перевыпуск
# --------------------------------------------------------------------------


def test_generated_password_has_requested_length():
    assert len(passwords.generate_password(12)) == 12
    assert len(passwords.generate_password()) == (
        config.PASSWORD_MIN_LENGTH_DEFAULT
    )


def test_generated_password_passes_validation():
    for role in ("admin", "curator", "student"):
        generated = passwords.generate_password(passwords.min_length_for(role))
        assert passwords.validate_password(generated, role) is None


def test_reissue_password_changes_both_fields_and_lets_user_in(
    as_app, connection, course
):
    from tests.conftest import add_user

    user_id = add_user(
        connection,
        "s1@example.ru",
        "Старый123",
        course_id=course,
        must_change_password=1,
    )
    before = connection.execute(
        "SELECT password_enc, password_hash FROM users WHERE id = ?", (user_id,)
    ).fetchone()

    issued = passwords.reissue_password(user_id)

    after = connection.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    assert after["password_enc"] != before["password_enc"]
    assert after["password_hash"] != before["password_hash"]
    assert passwords.reveal_password(after) == issued
    assert passwords.verify_password(issued, after["password_hash"])
    assert not passwords.verify_password("Старый123", after["password_hash"])
    # AC-20: вход по новому паролю сразу, принудительной смены нет.
    assert after["must_change_password"] == 0


def test_reissued_admin_password_meets_admin_minimum(as_app, connection, course):
    from tests.conftest import add_user

    user_id = add_user(
        connection, "admin@example.ru", "Старый123", role="admin", course_id=course
    )
    issued = passwords.reissue_password(user_id)
    assert passwords.validate_password(issued, "admin") is None


def test_reissue_unknown_user_raises(as_app, connection):
    with pytest.raises(LookupError):
        passwords.reissue_password(9999)


def test_reissue_inactive_user_raises(as_app, connection, course):
    from tests.conftest import add_user

    user_id = add_user(
        connection, "s1@example.ru", "Пароль123", course_id=course, is_active=0
    )
    with pytest.raises(LookupError):
        passwords.reissue_password(user_id)
