"""Работа с ключом шифрования паролей (Fernet).

Ключ лежит в ``var/secret.key`` и создаётся командой
``python -m app.cli init-key``. Ключ не пересоздаётся, если файл уже есть:
пароли шифруются им надолго, и смена ключа сделала бы их нечитаемыми.
"""

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app import config


class KeyError_(RuntimeError):
    pass


class SecretKeyMissing(KeyError_):
    def __init__(self, path: Path) -> None:
        super().__init__(
            f"Файл ключа {path} не найден. "
            f"Выполните команду: python -m app.cli init-key"
        )


def key_path() -> Path:
    return config.SECRET_KEY_PATH


def key_exists(path: Path | None = None) -> bool:
    return (path or key_path()).is_file()


def _restrict_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def read_key(path: Path | None = None) -> bytes:
    target = path or key_path()
    if not target.is_file():
        raise SecretKeyMissing(target)
    key = target.read_bytes().strip()
    if not key:
        raise SecretKeyMissing(target)
    try:
        Fernet(key)
    except (ValueError, TypeError) as error:
        raise KeyError_(
            f"Файл ключа {target} повреждён: это не ключ Fernet. "
            f"Восстановите его из резервной копии."
        ) from error
    return key


def write_key(key: bytes, path: Path | None = None) -> Path:
    target = path or key_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(key)
    _restrict_permissions(target)
    return target


def create_key(path: Path | None = None) -> tuple[Path, bool]:
    target = path or key_path()
    if target.is_file():
        read_key(target)
        return target, False
    return write_key(Fernet.generate_key(), target), True


def load_fernet(path: Path | None = None) -> Fernet:
    return Fernet(read_key(path))


def require_key(path: Path | None = None) -> bytes:
    return read_key(path)


def encrypt(value: str, fernet: Fernet | None = None) -> bytes:
    cipher = fernet or load_fernet()
    return cipher.encrypt(value.encode("utf-8"))


def decrypt(token: bytes, fernet: Fernet | None = None) -> str:
    cipher = fernet or load_fernet()
    try:
        return cipher.decrypt(token).decode("utf-8")
    except InvalidToken as error:
        raise KeyError_(
            "Не удалось расшифровать значение: ключ не совпадает с тем, "
            "которым значение было зашифровано."
        ) from error


def session_secret(path: Path | None = None) -> str:
    return read_key(path).decode("ascii")
