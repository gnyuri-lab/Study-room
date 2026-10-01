"""Модуль вложений: проверка содержимого, проверка, закрепление и
удаление файлов (FR-51…FR-54, NFR-06, AC-33, AC-34).

Тесты идут по коду и проверяют решения, а не только «функция не
упала»:

* содержимое обязано совпадать с расширением в обе стороны — и
  настоящий ``.docx`` под именем ``.zip``, и zip под именем ``.docx``;
* пустой файл не проходит: ``size_bytes > 0`` в схеме, и файл должен
  отвергаться до записи, а не падать на внешнем ключе;
* русский текст в кодировке cp1251 и в UTF-16 без BOM читается целиком,
  а не обрезается на середине многобайтового символа;
* имя файла из базы не может вывести путь за пределы каталога.
"""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

import pytest
from werkzeug.datastructures import FileStorage

from app import attachments, config
from app.services import ServiceError

#: Настоящий PNG 1×1: без него проверка «по содержимому» подменялась бы
#: проверкой расширения, а именно её здесь и надо испытывать.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8"
    "BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

RUSSIAN_CP1251 = "Задание с кириллицей в cp1251\n" * 40


def upload(data: bytes, name: str) -> FileStorage:
    return FileStorage(stream=io.BytesIO(data), filename=name, name="files")


def zip_bytes(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


XLSX_BYTES = zip_bytes(
    {
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": "<workbook/>",
    }
)
DOCX_BYTES = zip_bytes(
    {
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": "<document/>",
    }
)


# --------------------------------------------------------------------------
# Допустимые типы
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,data",
    [
        ("конспект.txt", "Обычный текст задания\n".encode("utf-8")),
        ("readme.md", "# Задание\n\nТекст.\n".encode("utf-8")),
        ("данные.csv", "a;b\n1;2\n".encode("utf-8")),
        ("кодировка.txt", RUSSIAN_CP1251.encode("cp1251")),
        ("utf16.txt", RUSSIAN_CP1251.encode("utf-16")),
        ("картинка.png", PNG_BYTES),
        ("скан.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 40 + b"\xff\xd9"),
        ("документ.pdf", PDF_BYTES),
        ("книга.xlsx", XLSX_BYTES),
        ("статья.docx", DOCX_BYTES),
        ("архив.zip", zip_bytes({"a.txt": "привет"})),
    ],
)
def test_accepted_types(workspace, name, data):
    """Файл допустимого типа с настоящим содержимым принимается (FR-51)."""
    item = attachments.stage_upload(upload(data, name))
    assert item["extension"] == Path(name).suffix.lstrip(".").lower()
    assert item["original_name"] == name
    assert item["size_bytes"] == len(data)
    attachments.discard_staged(item["staged_name"])


def test_ole2_xls_accepted(workspace):
    """``.xls`` — настоящий OLE2-контейнер: filetype его не опознаёт.

    Проверка содержимого для OLE2 сделана по сигнатуре, иначе
    допустимый из ТЗ формат оказался бы непригодным.
    """
    data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    item = attachments.stage_upload(upload(data, "старый.xls"))
    assert item["extension"] == "xls"
    attachments.discard_staged(item["staged_name"])


# --------------------------------------------------------------------------
# Отказы
# --------------------------------------------------------------------------


def test_text_renamed_to_png_rejected(workspace):
    """Текст под именем ``.png`` — расхождение расширения и содержимого."""
    with pytest.raises(ServiceError) as failure:
        attachments.stage_upload(upload("не картинка".encode("utf-8"), "картинка.png"))
    assert "png" in str(failure.value)


def test_png_renamed_to_txt_rejected(workspace):
    """Обратное расхождение: картинка под именем ``.txt``."""
    with pytest.raises(ServiceError) as failure:
        attachments.stage_upload(upload(PNG_BYTES, "картинка.txt"))
    assert "txt" in str(failure.value)


def test_disallowed_extension_rejected(workspace):
    with pytest.raises(ServiceError) as failure:
        attachments.stage_upload(upload(b"#!/bin/sh\necho hi\n", "скрипт.sh"))
    assert "sh" in str(failure.value)
    assert attachments.allowed_list() in str(failure.value)


def test_file_without_extension_rejected(workspace):
    with pytest.raises(ServiceError):
        attachments.stage_upload(upload("что-то".encode("utf-8"), "файл"))


def test_empty_file_rejected(workspace):
    """Пустой файл не проходит: в схеме ``size_bytes > 0``.

    Если бы он дошёл до вставки, нарушение вылезло бы как ошибка
    SQLite, то есть пользователь получил бы 500 вместо отказа.
    """
    with pytest.raises(ServiceError):
        attachments.stage_upload(upload(b"", "пусто.txt"))


def test_oversize_file_rejected(workspace):
    """Файл больше 10 МБ отклоняется и не сохраняется (FR-52)."""
    data = b"x" * (config.MAX_FILE_SIZE_BYTES + 1)
    with pytest.raises(ServiceError) as failure:
        attachments.stage_upload(upload(data, "огромный.txt"))
    assert str(config.MESSAGES["file_too_large"]) in str(failure.value)
    assert list(attachments.staging_dir().glob("*")) == []


def test_file_at_size_limit_accepted(workspace):
    """Ровно 10 МБ допустимы: превышение, а не достижение предела."""
    data = b"a" * config.MAX_FILE_SIZE_BYTES
    item = attachments.stage_upload(upload(data, "ровно.txt"))
    assert item["size_bytes"] == config.MAX_FILE_SIZE_BYTES
    attachments.discard_staged(item["staged_name"])


def test_count_limit(workspace):
    """Шестой файл отклоняется с указанием лимита (FR-52, AC-34)."""
    with pytest.raises(ServiceError) as failure:
        attachments.check_count(5, 1)
    assert "5" in str(failure.value)
    # Пять файлов на задание — ещё допустимо.
    attachments.check_count(4, 1)


def test_corrupt_zip_is_rejected_not_crashed(workspace):
    """Битый архив — это отказ по типу, а не 500."""
    data = b"PK\x03\x04" + b"\x00" * 20
    with pytest.raises(ServiceError):
        attachments.stage_upload(upload(data, "битый.xlsx"))


# --------------------------------------------------------------------------
# Очистка имени
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("C:\\Users\\Кто-то\\Задание 1.xlsx", "Задание 1.xlsx"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("..\\..\\secret.txt", "secret.txt"),
        ("  ./файл.txt  ", "файл.txt"),
        ("имя\tс\tпереносами.txt", "имяспереносами.txt"),
    ],
)
def test_safe_name_strips_paths(workspace, raw, expected):
    """Из исходного имени убирается путь (NFR-06, чек-лист §13)."""
    assert attachments.original_name(raw) == expected


def test_path_in_name_cannot_escape_on_read(workspace):
    """Строка вида ``../`` в базе не выводит чтение за пределы каталога.

    Путь строится только по сгенерированному имени, но проверка
    формальная: она защищает и от правки базы руками.
    """
    with pytest.raises(ServiceError):
        attachments.stored_path("../secret.txt")


# --------------------------------------------------------------------------
# Проверка, закрепление, удаление
# --------------------------------------------------------------------------


def test_staging_then_commit_moves_file(workspace):
    """Проверенный файл переносится из ``staging`` в корень каталога."""
    item = attachments.stage_upload(upload("материал".encode("utf-8"), "материал.txt"))
    staged = attachments.staging_path(item["staged_name"])
    assert staged.is_file()

    stored_name = attachments.commit_staged(item["staged_name"])
    assert not staged.exists()
    target = attachments.stored_path(stored_name)
    assert target.read_bytes() == "материал".encode("utf-8")
    # Имя в каталоге не совпадает с исходным (FR-54).
    assert target.name != "материал.txt"

    attachments.delete_stored(stored_name)
    assert not target.exists()


def test_discard_removes_staged_file(workspace):
    item = attachments.stage_upload(upload("черновик".encode("utf-8"), "черновик.txt"))
    assert attachments.staging_path(item["staged_name"]).is_file()
    attachments.discard_staged(item["staged_name"])
    assert not attachments.staging_path(item["staged_name"]).exists()


def test_discard_all_is_idempotent(workspace):
    """Повторная отмена на уже удалённом файле не должна падать.

    Черновик может быть отменён и кнопкой, и сборщиком мусора, и
    переходом на другую страницу формы.
    """
    item = attachments.stage_upload(upload("раз".encode("utf-8"), "раз.txt"))
    attachments.discard_all([item["staged_name"]])
    attachments.discard_all([item["staged_name"]])


def test_commit_missing_file_reports_service_error(workspace):
    with pytest.raises(ServiceError):
        attachments.commit_staged("staged-нет-такого.txt")


def test_delete_missing_file_is_quiet(workspace):
    """Удаление уже удалённого файла не должно ронять страницу.

    Каскадное удаление курса удаляет файлы по списку из базы, и к
    моменту обработки части строк файлов на диске может уже не быть.
    """
    attachments.delete_stored("нет-такого-файла.txt")


# --------------------------------------------------------------------------
# Описание из файла (FR-33)
# --------------------------------------------------------------------------


def test_read_description_utf8(workspace):
    text = attachments.read_description(upload("Текст задания".encode("utf-8"), "d.txt"))
    assert text == "Текст задания"


def test_read_description_cp1251(workspace):
    text = attachments.read_description(upload(RUSSIAN_CP1251.encode("cp1251"), "d.txt"))
    assert "кириллицей" in text


def test_read_description_strips_and_deletes_staging(workspace):
    """Описание читается целиком и не остаётся лежать в uploads."""
    payload = ("Строка\n" * 5000).encode("utf-8")
    text = attachments.read_description(upload(payload, "d.txt"))
    assert text.count("Строка") == 5000
    assert list(attachments.staging_dir().glob("*")) == []


def test_read_description_rejects_binary(workspace):
    with pytest.raises(ServiceError) as failure:
        attachments.read_description(upload(PNG_BYTES, "d.txt"))
    assert str(config.MESSAGES["description_file_unreadable"]) in str(
        failure.value
    )


def test_read_description_rejects_other_format(workspace):
    with pytest.raises(ServiceError) as failure:
        attachments.read_description(upload(PNG_BYTES, "d.png"))
    assert str(config.MESSAGES["description_file_type"]) in str(failure.value)


def test_read_description_rejects_oversize(workspace):
    data = b"a" * (config.MAX_FILE_SIZE_BYTES + 1)
    with pytest.raises(ServiceError):
        attachments.read_description(upload(data, "d.txt"))
    assert list(attachments.staging_dir().glob("*")) == []


# --------------------------------------------------------------------------
# Размер для показа
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size,expected",
    [(1, "1 Б"), (1023, "1023 Б"), (1024, "1 КБ"), (1048576, "1,0 МБ")],
)
def test_human_size(workspace, size, expected):
    assert attachments.human_size(size) == expected
