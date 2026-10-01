"""Пароли: хеширование, шифрование и квалификация.

Пароль хранится в двух видах (ТЗ §7.3, FR-09, риск Р-01):

* ``password_hash`` — ``pbkdf2_hmac`` в формате
  ``pbkdf2_sha256$<итерации>$<соль_hex>$<хеш_hex>``. По нему пароль
  сверяется быстро и не восстанавливается.
* ``password_enc`` — обратимое шифрование Fernet ключом из
  ``var/secret.key``, чтобы администратор мог увидеть текущий пароль
  учётной записи (FR-09).

Оба поля обязательны, и схема это требует. Хранить только хеш нельзя:
администратор перестанет видеть пароль, а это прямое требование FR-09.
Это осознанный риск Р-01, а не недоработка.

Число итераций не задано ТЗ и выбрано здесь. Сверка читает количество
итераций из самого хеша, поэтому стоимость можно менять для тестов,
не ломая ранее сохранённые пароли.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3

from app import config
from app import security

HASH_SCHEME = "pbkdf2_sha256"
HASH_ALGORITHM = "sha256"
HASH_ITERATIONS = 240_000
HASH_SALT_BYTES = 16

HASH_SEPARATOR = "$"
_HASH_RE = re.compile(
    r"^pbkdf2_sha256\$(?P<iterations>\d+)\$(?P<salt>[0-9a-f]+)\$(?P<digest>[0-9a-f]+)$"
)

#: Длина e-mail не может превышать 254 символа (§7.3, config.EMAIL_MAX_LENGTH).
_EMAIL_MAX_LENGTH = 254


class PasswordFormatError(ValueError):
    """Хранимый хеш не соответствует ожидаемому формату."""


# --------------------------------------------------------------------------
# Хеширование (быстрая сверка при входе)
# --------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int | None = None) -> str:
    """Вернуть pbkdf2-хеш пароля строкой."""
    if not isinstance(password, str) or password == "":
        raise ValueError("Пароль не может быть пустым.")
    rounds = iterations or HASH_ITERATIONS
    salt = secrets.token_bytes(HASH_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        HASH_ALGORITHM, password.encode("utf-8"), salt, rounds
    )
    return HASH_SEPARATOR.join(
        (HASH_SCHEME, str(rounds), salt.hex(), digest.hex())
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """Сверить пароль с хешем за постоянное время.

    Возвращает ``False``, а не исключение, если хеш повреждён: битый
    хеш не должен превращаться в ошибку 500 на странице входа.
    """
    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False
    match = _HASH_RE.match(stored_hash)
    if match is None:
        return False
    try:
        rounds = int(match.group("iterations"))
        salt = bytes.fromhex(match.group("salt"))
        expected = bytes.fromhex(match.group("digest"))
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        HASH_ALGORITHM, password.encode("utf-8"), salt, rounds
    )
    return hmac.compare_digest(candidate, expected)


# --------------------------------------------------------------------------
# Шифрование (видимость пароля администратором)
# --------------------------------------------------------------------------


def encrypt_password(password: str) -> bytes:
    """Зашифровать пароль ключом приложения."""
    if not isinstance(password, str) or password == "":
        raise ValueError("Пароль не может быть пустым.")
    return security.encrypt(password)


def decrypt_password(token: bytes) -> str:
    """Расшифровать пароль; нужен только администратору (FR-09)."""
    if not token:
        raise ValueError("Учётная запись не содержит зашифрованного пароля.")
    return security.decrypt(bytes(token))


def reveal_password(user) -> str:
    """Показать текущий пароль учётной записи.

    Право вызова проверяет слой доступа: ``access.serialize_user``
    отдаёт поле ``password`` только администратору (FR-09, BR-30).
    """
    return decrypt_password(user["password_enc"])


# --------------------------------------------------------------------------
# Формирование и смена учётных данных
# --------------------------------------------------------------------------


def min_length_for(role: str) -> int:
    """Минимальная длина пароля для роли (§7.2, BR-10, BR-11)."""
    return config.PASSWORD_MIN_LENGTH.get(
        role, config.PASSWORD_MIN_LENGTH_DEFAULT
    )


def build_credentials(password: str, role: str) -> tuple[bytes, str]:
    """Вернуть пару ``(password_enc, password_hash)`` для нового пароля."""
    return encrypt_password(password), hash_password(password)


def reissue_password(
    user_id: int, *, connection: sqlite3.Connection | None = None
) -> str:
    """Назначить учётной записи новый пароль и вернуть его открытым текстом.

    Нужен администратору, потерявшему пароль (FR-10, AC-20). Повторная
    выдача не требует принудительной смены при следующем входе: по
    AC-20 пользователь входит по новому паролю сразу, поэтому признак
    ``must_change_password`` снимается. Запись в журнал делает вызывающий
    код — ``action_log.log_action(..., action="pwd.reissued_by_admin")``.

    ``connection`` добавлен на Э-3: выдача пароля и запись в журнал
    должны подтвердиться вместе. Раньше обновление шло отдельным
    autocommit-оператором, и падение журнала оставляло новый пароль без
    записи об аудите.
    """
    from app import db

    if connection is None:
        connection = db.get_db()
    row = connection.execute(
        "SELECT id, role FROM users WHERE id = ? AND is_active = 1",
        (user_id,),
    ).fetchone()
    if row is None:
        raise LookupError("Учётная запись не найдена или отключена.")
    new_password = generate_password(min_length_for(row["role"]))
    encrypted, hashed = build_credentials(new_password, row["role"])
    connection.execute(
        """
        UPDATE users
           SET password_enc = ?, password_hash = ?, must_change_password = 0
         WHERE id = ?
        """,
        (encrypted, hashed, user_id),
    )
    return new_password


def generate_password(length: int | None = None) -> str:
    """Сгенерировать пароль, который проходит квалификацию по длине.

    Алфавит без ``0``/``O`` и ``1``/``l`` — пароль диктуется вслух
    и переносится на бумагу.
    """
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    size = length or config.PASSWORD_MIN_LENGTH_DEFAULT
    return "".join(secrets.choice(alphabet) for _ in range(max(size, 1)))


# --------------------------------------------------------------------------
# Квалификация (BR-10, BR-11, BR-12 и §9.3)
# --------------------------------------------------------------------------


def short_password_message(role: str) -> str:
    """Текст отказа по длине пароля — разный для администратора и остальных."""
    if role == "admin":
        return config.MESSAGES["admin_password_short"]
    return config.MESSAGES["user_password_short"]


def validate_password(password: str, role: str) -> str | None:
    """Проверить длину пароля. ``None`` — пароль допустим."""
    if not isinstance(password, str) or len(password) < min_length_for(role):
        return short_password_message(role)
    return None


def validate_password_confirmation(
    password: str, confirmation: str, role: str
) -> str | None:
    """Проверить пароль и его повтор. ``None`` — пара допустима.

    Сначала проверяется длина, затем совпадение: при неверных обоих полях
    показывается причина, которую пользователь может исправить сразу.
    """
    length_error = validate_password(password, role)
    if length_error is not None:
        return length_error
    if not isinstance(confirmation, str) or confirmation != password:
        return config.MESSAGES["passwords_mismatch"]
    return None


def is_valid_email(email: str) -> bool:
    """Проверить e-mail по §7.3: есть ``@``, после него домен, длина ≤ 254."""
    if not isinstance(email, str):
        return False
    candidate = email.strip()
    if not candidate or len(candidate) > _EMAIL_MAX_LENGTH:
        return False
    if candidate.count("@") != 1:
        return False
    local, _, domain = candidate.partition("@")
    if not local or not domain:
        return False
    if domain.startswith(".") or domain.endswith(".") or "." not in domain:
        return False
    last_label = domain.rsplit(".", 1)[-1]
    return len(last_label) >= 2 and not any(c.isspace() for c in candidate)


def validate_email(email: str) -> str | None:
    """Проверить логин. ``None`` — логин допустим."""
    if not is_valid_email(email):
        return config.MESSAGES["email_invalid"]
    return None


def normalise_email(email: str) -> str:
    """Привести логин к виду для хранения: обрезать края."""
    return email.strip()
