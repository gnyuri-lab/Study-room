"""Вложения: проверка файла, хранение, выдача и удаление (FR-35, FR-36,
FR-51…FR-54, NFR-06).

Требование FR-51 двойное: файл принимается «по расширению и по
фактическому типу содержимого». Поэтому одного ``filetype`` мало, и
это видно на той же версии библиотеки, что стоит в ``requirements.txt``
(``filetype==1.2.0``):

* ``png``, ``jpg``, ``pdf``, ``zip``, а также настоящие ``xlsx`` и
  ``docx`` определяются надёжно — zip-контейнеры ``filetype``
  различает по составу архива;
* ``xls`` и ``doc`` — это OLE2-контейнер, и на синтетическом файле из
  одних только магических байт определителя в библиотеке нет;
* ``txt``, ``md`` и ``csv`` магических байт не имеют вовсе: для них
  ``filetype`` возвращает ``None``, и так же он ведёт себя на настоящем
  тексте.

Поэтому содержимое проверяется тремя способами: ``filetype`` для
форматов с магией, разбор байтов как текста для текстовых и сравнение
с магическим числом OLE2 для ``.xls`` и ``.doc``. Проверка идёт по
совпадению: ``контракт.xlsx``, внутри которого лежит текст, отвергается
так же, как ``контракт.png``, собранный из чужого архива.

Путь хранения строится только из сгенерированного имени (FR-54,
NFR-06). Присланное имя в пути не участвует: в нём может быть
``../``, кириллица и длина больше предела в схеме. Исходное имя живёт
в колонке ``original_name`` и показывается при скачивании.

Право проверяется при каждом скачивании (FR-53), а не один раз при
выдаче ссылки: ``files.assignment_download`` зовёт
``can_view_course`` курса задания, поэтому студент чужого курса по
прямой ссылке получает отказ, а не файл.
"""

from __future__ import annotations

import codecs
import secrets
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Iterable

import filetype

from app import access, config, db
from app.services import ServiceError

#: Подкаталог для файлов, которые ещё не записаны в базу: материалы
#: заданий, ждущие подтверждения формы. Отделён от сохранённых
#: материалов, чтобы уборщик не путал их с тем, что уже в базе.
STAGING_SUBDIR = "staging"

#: Префикс имён файлов на проверке. По нему ``_sweep`` понимает, какие
#: файлы можно удалять: сохранённые материалы лежат в корне
#: ``UPLOADS_DIR`` и без префикса, файлы импорта — в ``import/``.
STAGING_PREFIX = "staged-"

#: Магическое число OLE2-контейнера: так устроены ``.xls`` и ``.doc``.
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Форматы, у которых нет магических байт: их содержимое проверяется
#: разбором как текст.
TEXT_EXTENSIONS = ("txt", "md", "csv")

#: OLE2-контейнеры: ``filetype`` на них не срабатывает.
OLE2_EXTENSIONS = ("xls", "doc")

#: Кодировки текстовых файлов в порядке предпочтения. Фактический
#: состав подбирает ``_candidate_encodings`` по BOM: UTF-16 без BOM
#: не декодируется, а cp1251 проверяется последней, потому что как
#: однобайтовая кодировка она принимает почти любой байтовый набор.
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1251")

#: Сколько байт в начале файла просматривается на предмет нулевых
#: байтов. Весь файл при этом декодируется целиком: обрезанный в
#: середине символ дал бы ложный отказ для русского текста.
TEXT_SNIFF_BYTES = 8192

#: После какого возраста невостребованный файл на проверке считается
#: брошенным. Ровно столько же живёт сессия
#: (``PERMANENT_SESSION_LIFETIME``): подтвердить просроченный
#: предпросмотр всё равно нельзя.
STALE_AFTER_SECONDS = config.PERMANENT_SESSION_LIFETIME

#: Файл может достигать 10 МБ, поэтому копируется по частям, и размер
#: считается по ходу: превышенный файл не доходит до конца.
CHUNK_SIZE = 64 * 1024


# --------------------------------------------------------------------------
# Имя файла
# --------------------------------------------------------------------------


def original_name(raw: str) -> str:
    """Имя файла из multipart-заголовка: только имя, без пути.

    ``Content-Disposition`` может содержать и полный путь, и чужой
    разделитель каталогов. Всё это отсекается здесь, а не при
    построении пути, — иначе пришлось бы чистить путь дважды.
    """
    cleaned = (raw or "").replace("\\", "/").strip()
    name = Path(cleaned).name
    # Управляющие и непечатаемые символы портят заголовок ответа при
    # скачивании, поэтому убираются здесь, а не при показе.
    name = "".join(
        character for character in name if character.isprintable()
    ).strip()
    if not name:
        _reject_extension("")
    return name[:255]


def _token_name(extension: str) -> str:
    """Сгенерированное имя файла вместо присланного (FR-54, NFR-06)."""
    suffix = "." + extension if extension else ""
    return "{token}{suffix}".format(token=secrets.token_hex(16), suffix=suffix)


def allowed_list() -> str:
    """Список разрешённых расширений для текста §9.3 — через запятую."""
    return ", ".join(config.ALLOWED_FILE_EXTENSIONS)


def extension_of(original_name: str) -> str:
    """Расширение в нижнем регистре и без точки."""
    return Path(original_name).suffix.lower().lstrip(".")


def _reject_extension(extension: str) -> None:
    raise ServiceError(
        config.MESSAGES["file_type_invalid"].format(
            extension=extension or "без расширения",
            allowed=allowed_list(),
        )
    )


def _stream_of(upload) -> BinaryIO:
    """Поток загруженного файла.

    Тесты и обработчики передают ``werkzeug``-овский ``FileStorage``,
    у которого поток лежит в ``.stream``; на случай сырого объекта с
    ``read`` берётся сам объект.
    """
    stream = getattr(upload, "stream", None)
    if stream is not None and hasattr(stream, "read"):
        return stream
    return upload


# --------------------------------------------------------------------------
# Каталоги
# --------------------------------------------------------------------------


def uploads_dir() -> Path:
    return Path(config.UPLOADS_DIR)


def staging_dir() -> Path:
    return uploads_dir() / STAGING_SUBDIR


def _child_of(directory: Path, name: str) -> Path:
    """Путь внутри каталога, проверенный на выход за его пределы.

    Имя приходит из базы, а не от пользователя, поэтому проверка
    формальная: она защищает от строки, которая всё же оказалась в
    базе (правка руками, восстановление дампа), и не даёт отдачу
    файла уйти за пределы каталога.
    """
    root = directory.resolve()
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ServiceError(config.MESSAGES["not_found"])
    target = (root / name).resolve()
    if target.parent != root:
        raise ServiceError(config.MESSAGES["not_found"])
    return target


def stored_path(stored_name: str) -> Path:
    """Путь к сохранённому файлу по сгенерированному имени."""
    return _child_of(uploads_dir(), stored_name)


def staging_path(staged_name: str) -> Path:
    """Путь к файлу на проверке."""
    return _child_of(staging_dir(), staged_name)


def human_size(size_bytes: int) -> str:
    """Размер для показа: «12 КБ», «1,4 МБ».

    Разделитель дробной части — запятая: в русском интерфейсе точка
    в размере читается как точка в конце предложения (NFR-13).
    """
    if size_bytes < 1024:
        return "{0} Б".format(size_bytes)
    if size_bytes < 1024 * 1024:
        return "{0:.0f} КБ".format(size_bytes / 1024)
    return "{0:.1f} МБ".format(size_bytes / (1024 * 1024)).replace(".", ",")


# --------------------------------------------------------------------------
# Проверка типа содержимого
# --------------------------------------------------------------------------


def _candidate_encodings(data: bytes) -> tuple[str, ...]:
    """Кодировки, которые имеет смысл пробовать для этих байтов.

    Порядок и состав важны по двум причинам.

    ``utf-16`` без BOM нельзя пробовать наугад: русский текст в cp1251
    занимает нечётное число байт на символ, и любая пара соседних
    байтов при декодировании в UTF-16 даёт не ошибку, а молчаливый
    мусор из私用区. Поэтому UTF-16 проверяется только при наличии BOM —
    так он и пишется на практике.

    ``cp1251`` — однобайтовая кодировка, и она декодирует почти любую
    последовательность байтов без ошибки, поэтому идёт последней: иначе
    любой двоичный файл прошёл бы проверку «текстовый».
    """
    candidates = []
    if data.startswith(codecs.BOM_UTF8):
        candidates.append("utf-8-sig")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates.append("utf-16")
    candidates.extend(("utf-8", "cp1251"))
    return tuple(candidates)


def _as_text(data: bytes) -> str | None:
    """Текст файла, если содержимое текстовое, иначе ``None``.

    Текстовый файл отличается от двоичного не «числом нечитаемых
    байтов», а отсутствием нулевых байтов в начале: в UTF-8 и UTF-16
    без BOM нулей внутри текста нет, а в ``.png`` или ``.xls`` они
    идут с первой строки. Проверять именно начало файла обязательно:
    в конце большого двоичного файла нулей может не оказаться.

    Пустой файл текстом не считается: ``size_bytes > 0`` требует
    ограничение в схеме, и пустой файл должен быть отвергнут до
    записи, а не упасть на внешнем ключе.
    """
    if not data:
        return None
    head = data[:TEXT_SNIFF_BYTES]
    for encoding in _candidate_encodings(data):
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\x00" in head.decode(encoding, errors="ignore"):
            continue
        return text
    return None


def _content_matches(extension: str, path: Path) -> bool:
    """Совпадает ли содержимое файла с его расширением (FR-51)."""
    if extension in TEXT_EXTENSIONS:
        return _as_text(path.read_bytes()) is not None

    if extension in OLE2_EXTENSIONS:
        with path.open("rb") as handle:
            return handle.read(len(OLE2_MAGIC)) == OLE2_MAGIC

    # Остальные форматы (png, jpg, pdf, zip, xlsx, docx) опознаёт
    # filetype. Сравнение идёт с расширением в обе стороны: и «настоящий
    # docx с именем .zip», и «zip с именем .docx» — расхождение.
    try:
        guessed = filetype.guess(str(path))
    except (OSError, ValueError, zipfile.BadZipFile):
        # Битый архив — это «не тот тип», а не 500.
        return False
    if guessed is None:
        return False
    return guessed.extension == extension


# --------------------------------------------------------------------------
# Уборка
# --------------------------------------------------------------------------


def _sweep() -> None:
    """Удалить файлы, оставшиеся на проверке.

    Файл, принятый формой, но не подтверждённый, лежит на диске.
    Удалять его при следующей загрузке безусловно нельзя — открытый
    предпросмотр перестанет подтверждаться, — поэтому смотрится
    возраст (больше lifetime сессии) и только наш префикс.
    """
    directory = staging_dir()
    if not directory.is_dir():
        return
    limit = time.time() - STALE_AFTER_SECONDS
    for path in directory.glob(STAGING_PREFIX + "*"):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            # Файл занят или уже удалён — не повод срывать загрузку.
            continue


# --------------------------------------------------------------------------
# Приём файла
# --------------------------------------------------------------------------


def _copy_with_limit(source: BinaryIO, target: Path) -> int:
    """Скопировать поток в файл, остановившись на превышении размера.

    Читать файл целиком в память нельзя: лимит 10 МБ на файл и до
    пяти файлов — это 50 МБ на одну форму. Размер считается по ходу
    копирования, поэтому файл больше лимита не доходит до конца и не
    остаётся на диске (FR-52).
    """
    limit = config.MAX_FILE_SIZE_BYTES
    written = 0
    with target.open("wb") as handle:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                # Обрываем копирование, не дописывая файл: превышенный
                # файл не должен оставаться на диске даже на секунду.
                handle.close()
                target.unlink(missing_ok=True)
                raise ServiceError(config.MESSAGES["file_too_large"])
            handle.write(chunk)
    return written


def stage_upload(upload) -> dict[str, Any]:
    """Проверить и положить файл на проверку.

    Возвращает сведения, которые потом записываются в
    ``assignment_files`` и показываются в предпросмотре формы:
    ``staged_name``, ``original_name``, ``extension``, ``size_bytes``.

    Файл сначала копируется во временный каталог и только потом
    проверяется содержимое: проверка ``.xlsx`` и ``.docx`` обязана
    открыть архив, а читать присланный поток второй раз нельзя. Любой
    отказ удаляет временный файл — «файл не сохраняется» (FR-52).
    """
    original = original_name(getattr(upload, "filename", ""))
    extension = extension_of(original)
    if extension not in config.ALLOWED_FILE_EXTENSIONS:
        _reject_extension(extension)

    _sweep()
    directory = staging_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (STAGING_PREFIX + _token_name(extension))
    try:
        size = _copy_with_limit(_stream_of(upload), target)
        if not _content_matches(extension, target):
            _reject_extension(extension)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {
        "staged_name": target.name,
        "original_name": original,
        "extension": extension,
        "size_bytes": size,
    }


def check_count(existing: int, added: int) -> None:
    """Проверить итоговое число файлов (FR-52, AC-34).

    Считается уже присутствующее плюс добавляемое: у задания может
    быть три файла, и добавление трёх превысит лимит пять.
    """
    if existing + added > config.MAX_FILES_PER_ASSIGNMENT:
        raise ServiceError(config.MESSAGES["files_limit_assignment"])


def commit_staged(staged_name: str) -> str:
    """Перенести проверенный файл из каталога проверки в хранение.

    Возвращает имя, под которым файл записывается в базу. Префикс
    ``staged-`` снимается: он означает «ещё не записано» и в базе, где
    его быть не должно, только шумит.
    """
    source = staging_path(staged_name)
    if not source.is_file():
        raise ServiceError(config.MESSAGES["assignment_preview_expired"])
    stored_name = source.name[len(STAGING_PREFIX):]
    uploads_dir().mkdir(parents=True, exist_ok=True)
    source.replace(uploads_dir() / stored_name)
    return stored_name


def discard_staged(staged_name: str) -> None:
    """Убрать файл с проверки, не записав его в базу."""
    try:
        staging_path(staged_name).unlink()
    except (OSError, ServiceError):
        # Файла уже нет — это не ошибка отмены.
        pass


def discard_all(staged_names: Iterable[str]) -> None:
    for name in staged_names:
        discard_staged(name)


def delete_stored(stored_name: str) -> None:
    """Удалить сохранённый файл с диска.

    Вызывается после коммита: сначала запись в базе, потом файл.
    Отсутствующий файл ошибкой не считается — база была источником
    истины, и счётчики в интерфейсе опираются на неё.
    """
    try:
        stored_path(stored_name).unlink()
    except (FileNotFoundError, ServiceError, OSError):
        return


# --------------------------------------------------------------------------
# Описание из файла (FR-33)
# --------------------------------------------------------------------------


def read_description(upload) -> str:
    """Прочитать описание задания из загруженного файла (FR-33).

    Файл не сохраняется: описание попадает в форму текстом, поэтому на
    диске ему не место. Проверяются расширение, размер и то, что
    содержимое читается как текст, — тем же способом, что и у
    материалов, но для меньшего набора форматов
    (``config.ASSIGNMENT_DESCRIPTION_EXTENSIONS``).
    """
    original = original_name(getattr(upload, "filename", ""))
    extension = extension_of(original)
    if extension not in config.ASSIGNMENT_DESCRIPTION_EXTENSIONS:
        raise ServiceError(config.MESSAGES["description_file_type"])

    _sweep()
    directory = staging_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (STAGING_PREFIX + _token_name(extension))
    try:
        _copy_with_limit(_stream_of(upload), target)
        text = _as_text(target.read_bytes())
        if text is None:
            raise ServiceError(config.MESSAGES["description_file_unreadable"])
        return text.strip()
    finally:
        # Описание в базу не пишется, и в ``uploads`` ему не место.
        target.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Чтение вложений
# --------------------------------------------------------------------------


def download_url(file_id: int, kind: str = "assignment") -> str:
    """Адрес скачивания вложения.

    Строится через ``url_for``: подставлять путь руками нельзя, маршрут
    изменится, а Э-6 добавит сюда же работы и файлы комментариев.
    """
    from flask import url_for

    return url_for("files.download", kind=kind, file_id=file_id)


def list_assignment_files(assignment_id: int) -> list[dict[str, Any]]:
    """Материалы задания в порядке загрузки (FR-35)."""
    rows = db.get_db().execute(
        """
        SELECT f.id, f.original_name, f.mime, f.size_bytes, f.created_at,
               f.stored_name, u.full_name AS uploaded_by_name
          FROM assignment_files f
          JOIN users u ON u.id = f.uploaded_by
         WHERE f.assignment_id = ?
         ORDER BY f.id
        """,
        (assignment_id,),
    ).fetchall()
    payload = [dict(row) for row in rows]
    for item in payload:
        item["size_label"] = human_size(item["size_bytes"])
        item["download_url"] = download_url(item["id"])
    return payload


def fetch_assignment_file(file_id: int) -> dict[str, Any] | None:
    """Вложение задания вместе с его курсом — для проверки права."""
    row = db.get_db().execute(
        """
        SELECT f.id, f.assignment_id, f.original_name, f.stored_name,
               f.mime, f.size_bytes, a.course_id, a.title AS assignment_title
          FROM assignment_files f
          JOIN assignments a ON a.id = f.assignment_id
         WHERE f.id = ?
        """,
        (file_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def fetch_assignment_file_row(file_id: int) -> sqlite3.Row | None:
    """Строка ``assignment_files`` — для удаления файла администратором."""
    return db.get_db().execute(
        "SELECT * FROM assignment_files WHERE id = ?", (file_id,)
    ).fetchone()


def can_access_assignment_file(item: dict[str, Any]) -> bool:
    """Есть ли у пользователя право на этот файл (FR-53).

    Право выводится из доступа к курсу задания: администратор видит
    всё, студент — свой курс, куратор — курсы своих групп. Отдельной
    таблицы прав на файлы в схеме нет, и заводить её не на чем.
    """
    return access.can_view_course(item["course_id"])
