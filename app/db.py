import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, g, has_app_context

_MIGRATION_SUFFIXES = (".sql",)


def _resolve_db_path() -> Path:
    if has_app_context():
        configured = current_app.config.get("DB_PATH")
        if configured:
            return Path(configured)
    from app import config

    return config.DB_PATH


def _resolve_migrations_dir() -> Path:
    if has_app_context():
        configured = current_app.config.get("MIGRATIONS_DIR")
        if configured:
            return Path(configured)
    from app import config

    return config.MIGRATIONS_DIR


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = _resolve_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            db_path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,
            timeout=15.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        g.db = connection
    return g.db


def close_db(_exception: BaseException | None = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


@contextmanager
def transaction():
    connection = get_db()
    depth = getattr(g, "_transaction_depth", 0)
    if depth == 0:
        connection.execute("BEGIN")
    g._transaction_depth = depth + 1
    try:
        yield connection
    except BaseException:
        g._transaction_depth = depth
        if depth == 0:
            connection.execute("ROLLBACK")
        raise
    else:
        g._transaction_depth = depth
        if depth == 0:
            connection.execute("COMMIT")


def _strip_comments(script: str) -> str:
    lines = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line.split("--", 1)[0] if "--" in line else line)
    return "\n".join(lines)


def _split_statements(script: str) -> list[str]:
    statements = []
    for chunk in script.split(";"):
        statement = chunk.strip()
        if statement:
            statements.append(statement)
    return statements


def discover_migrations(migrations_dir: Path) -> list[Path]:
    if not migrations_dir.is_dir():
        raise RuntimeError(
            f"Каталог миграций {migrations_dir} не найден."
        )
    files = sorted(
        path
        for path in migrations_dir.iterdir()
        if path.is_file() and path.suffix in _MIGRATION_SUFFIXES
    )
    if not files:
        raise RuntimeError(
            f"В каталоге миграций {migrations_dir} нет файлов .sql."
        )
    return files


def applied_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def apply_migrations(
    connection: sqlite3.Connection,
    migrations_dir: Path | None = None,
) -> list[str]:
    directory = migrations_dir or _resolve_migrations_dir()
    files = discover_migrations(directory)
    current = applied_version(connection)
    if current > len(files):
        raise RuntimeError(
            f"В базе записана версия схемы {current}, а файлов миграций "
            f"всего {len(files)}. Схема новее установленных скриптов."
        )
    applied = []
    for number, path in enumerate(files, start=1):
        if number <= current:
            continue
        script = _strip_comments(path.read_text(encoding="utf-8"))
        statements = _split_statements(script)
        if not statements:
            raise RuntimeError(f"Файл миграции {path.name} не содержит SQL.")
        connection.execute("BEGIN")
        try:
            for statement in statements:
                connection.execute(statement)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        connection.execute(f"PRAGMA user_version = {number}")
        applied.append(path.name)
    return applied


def init_database(
    db_path: Path | None = None,
    migrations_dir: Path | None = None,
) -> tuple[Path, list[str]]:
    from app import config

    target = Path(db_path) if db_path else config.DB_PATH
    directory = Path(migrations_dir) if migrations_dir else config.MIGRATIONS_DIR
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, isolation_level=None, timeout=15.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        applied = apply_migrations(connection, directory)
    finally:
        connection.close()
    return target, applied


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
