import argparse
import sqlite3
import sys
from pathlib import Path

from app import config

EXIT_OK = 0
EXIT_ERROR = 1

READY_STAGES = {
    "init-key": "Э-1",
    "init-db": "Э-1",
    "setup": "Э-1",
    "seed-demo": "Э-9",
}



def command_init_key(args: argparse.Namespace | None = None) -> int:
    from app import security

    path, created = security.create_key()
    if created:
        print("Создан ключ шифрования паролей: {path}".format(path=path))
    else:
        print(
            "Ключ шифрования уже существует, оставлен без изменений: "
            "{path}".format(path=path)
        )
    print("Ключ не попадает в git: каталог var исключён в .gitignore.")
    return EXIT_OK


def ensure_default_admin(db_path: Path) -> str:
    """Создать учётную запись администратора по умолчанию (FR-02, AC-01).

    Возвращает ``created``, ``exists`` или ``no_key``.

    Проверяется наличие **любой** строки с ролью ``admin``, а не совпадение
    e-mail: как только администратор сменил логин, повторный ``setup`` не
    должен воскресить вторую учётную запись. Пароль по умолчанию перестаёт
    действовать сразу после первой смены учётных данных, потому что строка
    ``password_hash`` заменяется (FR-03), а ``must_change_password`` снимается.

    Без ключа ``var/secret.key`` пароль нечем зашифровать, поэтому учётная
    запись не создаётся и команда сообщает, что делать.
    """
    from app import action_log, passwords

    if not Path(config.SECRET_KEY_PATH).exists():
        return "no_key"

    connection = sqlite3.connect(db_path, isolation_level=None, timeout=15.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        existing = connection.execute(
            "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()
        if existing is not None:
            return "exists"
        encrypted, hashed = passwords.build_credentials(
            config.DEFAULT_ADMIN_PASSWORD, "admin"
        )
        connection.execute(
            """
            INSERT INTO users (
                course_id, email, full_name, role, password_enc, password_hash,
                must_change_password, is_active, created_at
            ) VALUES (NULL, ?, ?, 'admin', ?, ?, 1, 1, ?)
            """,
            (
                config.DEFAULT_ADMIN_EMAIL,
                config.DEFAULT_ADMIN_FULL_NAME,
                encrypted,
                hashed,
                action_log.utc_now_iso(),
            ),
        )
    finally:
        connection.close()
    return "created"


def command_init_db(args: argparse.Namespace | None = None) -> int:
    from app import db

    path, applied = db.init_database()
    print("База данных: {path}".format(path=path))
    if applied:
        print("Применены миграции: {names}".format(names=", ".join(applied)))
    else:
        print("Схема уже актуальна, новые миграции не применялись.")

    # Учётная запись по умолчанию создаётся здесь, а не в seed-demo: без неё
    # на чистом развёртывании невозможно войти под администратором (FR-02).
    state = ensure_default_admin(path)
    if state == "created":
        print(
            "Учётная запись администратора: {email} / {password}"
            " (обязательная смена логина и пароля при первом входе, FR-02).".format(
                email=config.DEFAULT_ADMIN_EMAIL,
                password=config.DEFAULT_ADMIN_PASSWORD,
            )
        )
    elif state == "exists":
        print("Учётная запись администратора уже есть, пароль не менялся.")
    else:
        print(
            "Учётная запись администратора не создана: нет ключа"
            " {path}. Выполните «python -m app.cli init-key» и повторите"
            " «init-db».".format(path=config.SECRET_KEY_PATH)
        )
    return EXIT_OK


def command_setup(args: argparse.Namespace | None = None) -> int:
    code = command_init_key()
    if code != EXIT_OK:
        return code
    code = command_init_db()
    if code != EXIT_OK:
        return code
    return command_seed_demo(args)


def command_seed_demo(args: argparse.Namespace | None = None) -> int:
    """Загрузить демо-данные (FR-65…FR-67, этап Э-9)."""
    from app import create_app, db, seed

    force = bool(getattr(args, "force", False))
    if not Path(config.SECRET_KEY_PATH).exists():
        print(
            "Нет ключа шифрования {path}: выполните"
            " «python -m app.cli init-key».".format(path=config.SECRET_KEY_PATH)
        )
        return EXIT_ERROR
    if not Path(config.DB_PATH).exists():
        print(
            "Нет базы данных {path}: выполните"
            " «python -m app.cli init-db».".format(path=config.DB_PATH)
        )
        return EXIT_ERROR

    application = create_app()
    with application.app_context():
        db.init_database()
        state = ensure_default_admin(config.DB_PATH)
        if state == "created":
            print(
                "Создана учётная запись администратора по умолчанию:"
                " {email} / {password} (обязательная смена при первом"
                " входе).".format(
                    email=config.DEFAULT_ADMIN_EMAIL,
                    password=config.DEFAULT_ADMIN_PASSWORD,
                )
            )
        result = seed.seed_demo(force=force)

    if result["status"] == "exists":
        print(
            "Демо-данные уже загружены: курс «{title}» (id {id})."
            " Повторная загрузка — «python -m app.cli seed-demo --force»"
            " (удалит демо-курс и создаст заново).".format(
                title=result["title"],
                id=result["course_id"],
            )
        )
    else:
        print(
            "Демо-курс создан: «{title}» (id {id}).".format(
                title=result["title"],
                id=result["course_id"],
            )
        )
    print(
        "Групп: {groups}; студентов: {students}; кураторов: {curators};"
        " заданий: {assignments}; сдач: {submissions}; пар без сдачи"
        " («Не сдано»): {not_submitted}.".format(**result)
    )
    print("")
    print("Учётные записи демо-курса (пароль у всех — {password}):".format(
        password=seed.DEMO_PASSWORD
    ))
    print(
        "  администратор: {email} / {password} (смена логина и пароля"
        " обязательна при первом входе)".format(
            email=config.DEFAULT_ADMIN_EMAIL,
            password=config.DEFAULT_ADMIN_PASSWORD,
        )
    )
    print("  куратор: {email} / {password}".format(
        email=seed.CURATOR[1], password=seed.DEMO_PASSWORD
    ))
    for _group, full_name, email in seed.STUDENTS:
        print("  студент: {email} / {password} ({name})".format(
            email=email, password=seed.DEMO_PASSWORD, name=full_name
        ))
    print("Файлы для ручной загрузки: «Inputs/Демо-данные».")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description=(
            "Служебные команды приложения «{title}». Схема данных и "
            "демо-данные создаются по разделам 12.2 и 13.1 ТЗ_MVP.md."
        ).format(title=config.APP_TITLE),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python -m app.cli setup       развернуть ключ, базу и демо-данные\n"
            "  python -m app.cli init-key    только сгенерировать ключ шифрования\n"
            "  python -m app.cli init-db     только создать и мигрировать базу,\n"
            "                                 создать администратора по умолчанию\n"
            "  python -m app.cli seed-demo   только загрузить демо-данные\n"
            "  python -m app.cli seed-demo --force\n"
            "                                пересоздать демо-данные\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="{title} {version} (этап {stage})".format(
            title=config.APP_TITLE,
            version=config.APP_VERSION,
            stage=config.STAGE,
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="команда")
    for name, help_text in (
        ("setup", "развернуть ключ, базу и демо-данные одной командой"),
        ("init-key", "сгенерировать ключ шифрования паролей"),
        ("init-db", "создать базу, применить миграции и учётную запись по умолчанию"),
        ("seed-demo", "загрузить демонстрационные данные"),
    ):
        subparser = subparsers.add_parser(name, help=help_text, description=help_text)
        if name == "seed-demo":
            subparser.add_argument(
                "--force",
                action="store_true",
                help="удалить существующий демо-курс и загрузить данные заново",
            )
    return parser


COMMANDS = {
    "init-key": command_init_key,
    "init-db": command_init_db,
    "setup": command_setup,
    "seed-demo": command_seed_demo,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_OK
    handler = COMMANDS[args.command]
    try:
        return handler(args)
    except Exception as error:
        print("Ошибка: {error}".format(error=error), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
