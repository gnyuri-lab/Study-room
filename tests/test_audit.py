"""Экран журнала действий и выгрузка CSV (FR-61…FR-64, AC-32).

Проверяются решения этапа Э-8, а не «страница открылась»:

* запись журнала уже работает с Э-2, здесь экран показывает все поля
  §7.11: действие с подписью и кодом §7.12, автора с ролью, объект,
  курс/группу/задание, прежний и новый статус, детали, дата и время
  (FR-61, AC-32);
* четыре фильтра применяются одновременно и сбрасываются независимо,
  неверное значение даёт пустую выборку с сообщением, а не молча
  игнорируется (FR-63);
* выгрузка CSV повторяет текущую выборку: те же строки, что на экране,
  с BOM для Excel (FR-63, AC-32);
* в журнале и в выгрузке нет паролей и IP-адресов (FR-64);
* журнал открыт только администратору (FR-63, BR-33), а после
  удаления курса автор остаётся обезличенной строкой (Д-11, §12
  чек-листа).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta

import pytest

from app import action_log, config, passwords
from tests.conftest import (
    add_curator_to_group,
    add_student_to_group,
    add_user,
    insert_course,
    insert_group,
)
from tests.test_submissions import (
    ADMIN_LOGIN,
    CURATOR_LOGIN,
    STUDENT_LOGIN,
    insert_assignment,
    login,
)

#: Словарь с подозрением на IP-адрес: FR-64 запрещает их и в экране,
#: и в выгрузке. Сюда попадают и «01.10.2026 10.0.0.0»-подобные
#: совпадения, поэтому строки журнала в тестах содержат только даты с
#: обычными разделителями.
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

TIME_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$")


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(connection, key_path):
    """Два курса с группой, куратором и студентом в каждом + админ."""
    course = insert_course(connection, title="Основной курс")
    group = insert_group(connection, name="ФК-1", course_id=course)
    curator = add_user(
        connection,
        CURATOR_LOGIN[0],
        CURATOR_LOGIN[1],
        role="curator",
        course_id=course,
        full_name="Куратор Первый",
    )
    add_curator_to_group(connection, group, curator)
    student = add_user(
        connection,
        STUDENT_LOGIN[0],
        STUDENT_LOGIN[1],
        course_id=course,
        full_name="Студент Первый",
    )
    add_student_to_group(connection, group, student)
    admin = add_user(
        connection,
        ADMIN_LOGIN[0],
        ADMIN_LOGIN[1],
        role="admin",
        full_name="Администратор Системы",
    )

    other_course = insert_course(connection, title="Другой курс")
    return {
        "course": course,
        "group": group,
        "admin": admin,
        "curator": curator,
        "student": student,
        "other_course": other_course,
    }


def journal(connection, action, *, role="admin", entity="course", marker=None,
            **kwargs):
    """Запись в журнал напрямую — экран, а не запись, здесь под проверкой."""
    details = kwargs.pop("details", None)
    if marker is not None:
        details = dict(details or {})
        details["метка"] = marker
    return action_log.log_action(
        action,
        actor_id=kwargs.pop("actor_id", None),
        actor_role=role,
        entity_type=entity,
        details=details,
        connection=connection,
        **kwargs,
    )


def set_date(connection, row_id, stored):
    """Перевести запись на известное время (внутри log_action оно своё)."""
    connection.execute(
        "UPDATE action_log SET created_at = ? WHERE id = ?", (stored, row_id)
    )


def as_admin(client):
    return login(client, ADMIN_LOGIN)


def audit(client, query=""):
    """Открыть экран журнала и вернуть HTML."""
    response = client.get("/admin/audit" + query)
    assert response.status_code == 200, response.status_code
    return response.text


def export(client, query=""):
    """Выгрузить журнал и вернуть строки CSV (без заголовка)."""
    response = client.get("/admin/audit/export" + query)
    assert response.status_code == 200, response.status_code
    assert response.mimetype == "text/csv"
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith("attachment")
    assert "action_log.csv" in disposition
    # BOM: без него Excel на Windows не открывает кириллицу.
    assert response.data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    assert rows, "Выгрузка пуста даже с заголовком"
    assert rows[0] == list(action_log.CSV_HEADERS)
    return rows[1:]


# --------------------------------------------------------------------------
# Справочник §7.12 и поля FR-61
# --------------------------------------------------------------------------


def test_every_action_dictionary_code_has_a_label():
    """Новое значение §7.12 обязано получить подпись на экране."""
    assert set(action_log.ACTION_LABELS) == set(config.ACTION_CODES)


def test_csv_headers_match_the_spec_fields():
    """FR-61: состав полей журнала отражён в выгрузке."""
    assert action_log.CSV_HEADERS == (
        "Дата и время",
        "Автор",
        "Роль",
        "Действие",
        "Код действия",
        "Тип объекта",
        "ID объекта",
        "Курс",
        "Группа",
        "Задание",
        "Статус (был)",
        "Статус (стал)",
        "Детали",
    )


def test_query_serializes_all_spec_fields(connection, world):
    """Строка выборки содержит все поля FR-61 в читаемом виде."""
    assignment = insert_assignment(
        connection, course_id=world["course"], title="Домашняя работа 1"
    )
    row_id = journal(
        connection,
        "submission.status_changed",
        role="curator",
        entity="submission",
        actor_id=world["admin"],
        entity_id=42,
        course_id=world["course"],
        group_id=world["group"],
        assignment_id=assignment,
        from_status="submitted",
        to_status="revision",
        details={"attempt_no": 2},
    )
    rows = {row["id"]: row for row in action_log.query({}, connection=connection)}
    row = rows[row_id]

    assert row["action"] == "submission.status_changed"
    assert row["action_label"] == "Смена статуса"
    assert row["role_label"] == "Куратор"
    assert row["actor_email"] == ADMIN_LOGIN[0]
    assert row["entity_type_label"] == "Сдача" and row["entity_id"] == 42
    assert row["course_label"] == "Основной курс"
    assert row["group_label"] == "ФК-1"
    assert row["assignment_label"] == "Домашняя работа 1"
    assert row["status_label"] == "На проверке → На доработку"
    assert row["details_label"] == "attempt_no: 2"
    assert row["anonymized"] is False
    assert TIME_RE.match(row["time_label"]), row["time_label"]


def test_deleted_objects_are_labelled_honestly(connection, world):
    """Приём Э-3: физическое удаление через ``*.updated`` не врёт
    на экране, а ссылка на удалённый объект остаётся читаемой (Д-11)."""
    group_row = journal(
        connection,
        "group.updated",
        entity="group",
        entity_id=991,
        course_id=world["course"],
        group_id=991,
        details={"name": "ФК-9", "deleted": True},
    )
    assignment_row = journal(
        connection,
        "assignment.updated",
        entity="assignment",
        entity_id=992,
        course_id=world["course"],
        assignment_id=992,
        details={"title": "ДЗ", "deleted": True},
    )
    rows = {row["id"]: row for row in action_log.query({}, connection=connection)}
    assert rows[group_row]["action_label"] == "Группа удалена"
    assert rows[group_row]["group_label"] == "№991 (удалён)"
    assert rows[assignment_row]["action_label"] == "Задание удалено"
    assert rows[assignment_row]["assignment_label"] == "№992 (удалён)"


def test_anonymized_author_is_marked(connection, world):
    """Д-11, §12 чек-листа: после удаления курса автор обезличен."""
    row_id = journal(
        connection,
        "course.deleted",
        entity="course",
        entity_id=world["course"],
        course_id=world["course"],
    )
    rows = {row["id"]: row for row in action_log.query({}, connection=connection)}
    assert rows[row_id]["anonymized"] is True
    assert rows[row_id]["actor_email"] is None
    assert rows[row_id]["role_label"] == "Администратор"


# --------------------------------------------------------------------------
# Фильтры (FR-63)
# --------------------------------------------------------------------------


def test_parse_filters_reset_and_validation():
    from werkzeug.datastructures import MultiDict

    applied, malformed = action_log.parse_filters(
        MultiDict([("role", "curator"), ("course", "2"), ("date", "2026-06-15")])
    )
    assert applied == {
        "date": "2026-06-15",
        "role": "curator",
        "course": 2,
        "entity": None,
    }
    assert not malformed

    # Независимость сбросов: reset чистит только свой ключ.
    applied, malformed = action_log.parse_filters(
        MultiDict([("role", "curator"), ("course", "2"), ("reset", "role")])
    )
    assert applied["role"] is None and applied["course"] == 2
    assert not malformed

    applied, malformed = action_log.parse_filters(
        MultiDict([("role", "curator"), ("reset", "all")])
    )
    assert all(value is None for value in applied.values())
    assert not malformed

    # Неверное значение — ошибка фильтра, а не молчаливое игнорирование.
    for key, value in (
        ("role", "hacker"),
        ("course", "abc"),
        ("course", "0"),
        ("entity", "galaxy"),
        ("date", "2026-13-40"),
    ):
        applied, malformed = action_log.parse_filters(MultiDict([(key, value)]))
        assert malformed, (key, value)
        assert applied[key] is None

    # Дата нормализуется к ГГГГ-ММ-ДД.
    applied, malformed = action_log.parse_filters(MultiDict([("date", "2026-6-5")]))
    assert applied["date"] == "2026-06-05" and not malformed


def test_filter_by_role(connection, world, client):
    journal(connection, "course.created", role="admin",
            entity="course", marker="маркер-админ")
    journal(connection, "group.created", role="curator",
            entity="group", marker="маркер-куратор")
    journal(connection, "submission.created", role="student",
            entity="submission", marker="маркер-студент")
    as_admin(client)

    text = audit(client, "?role=curator")
    assert "маркер-куратор" in text
    assert "маркер-админ" not in text
    assert "маркер-студент" not in text
    # Выбор в списке сохранён и выгрузка получает тот же фильтр.
    assert 'value="curator" selected' in text
    assert "/admin/audit/export?role=curator" in text


def test_filter_by_course(connection, world, client):
    journal(connection, "course.created", entity="course",
            course_id=world["course"], marker="маркер-основной")
    journal(connection, "course.created", entity="course",
            course_id=world["other_course"], marker="маркер-чужой")
    journal(connection, "auth.login", entity="user", marker="маркер-без-курса")
    as_admin(client)

    text = audit(client, "?course={0}".format(world["course"]))
    assert "маркер-основной" in text
    assert "маркер-чужой" not in text
    # Строки без курса (вход, выход) фильтр курса не показывает.
    assert "маркер-без-курса" not in text


def test_filter_by_entity(connection, world, client):
    journal(connection, "pwd.reissued_by_admin", entity="user",
            marker="маркер-пользователь")
    journal(connection, "import.committed", entity="import",
            course_id=world["course"], marker="маркер-импорт")
    as_admin(client)

    text = audit(client, "?entity=import")
    assert "маркер-импорт" in text
    assert "маркер-пользователь" not in text


def test_filter_by_date_matches_the_visible_day(connection, world, client):
    row_id = journal(connection, "file.downloaded", entity="file",
                     course_id=world["course"], marker="маркер-дата")
    stored = "2026-06-15T22:30:00Z"
    set_date(connection, row_id, stored)
    shown = action_log.local_date(stored)
    other = (date.fromisoformat(shown) + timedelta(days=1)).isoformat()
    as_admin(client)

    assert "маркер-дата" in audit(client, "?date={0}".format(shown))
    assert "маркер-дата" not in audit(client, "?date={0}".format(other))


def test_filters_apply_simultaneously(connection, world, client):
    journal(connection, "group.created", role="curator", entity="group",
            course_id=world["course"], marker="маркер-подходит")
    journal(connection, "group.created", role="admin", entity="group",
            course_id=world["course"], marker="маркер-другая-роль")
    journal(connection, "group.created", role="curator", entity="group",
            course_id=world["other_course"], marker="маркер-другой-курс")
    as_admin(client)

    text = audit(
        client,
        "?role=curator&course={0}&entity=group".format(world["course"]),
    )
    assert "маркер-подходит" in text
    assert "маркер-другая-роль" not in text
    assert "маркер-другой-курс" not in text


def test_reset_clears_only_its_own_key(connection, world, client):
    journal(connection, "submission.created", role="curator",
            entity="submission", course_id=world["course"],
            marker="маркер-куратор")
    journal(connection, "submission.created", role="admin",
            entity="submission", course_id=world["other_course"],
            marker="маркер-чужой-курс")
    as_admin(client)

    # role сброшен, курс остался: строка куратора своего курса видна,
    # строка другого курса — нет.
    text = audit(
        client,
        "?role=curator&course={0}&reset=role".format(world["course"])
    )
    assert "маркер-куратор" in text
    assert "маркер-чужой-курс" not in text

    # reset=all чистит всё: возвращаются все строки.
    text = audit(client, "?role=curator&reset=all")
    assert "маркер-куратор" in text and "маркер-чужой-курс" in text
    assert 'value="curator" selected' not in text


@pytest.mark.parametrize(
    "query",
    [
        "?role=hacker",
        "?course=abc",
        "?course=0",
        "?entity=galaxy",
        "?date=2026-13-40",
    ],
)
def test_invalid_filter_gives_empty_result_with_message(
    connection, world, client, query
):
    """Подставленное значение не может ничего изменить (как на
    дашборде): пустая выборка целиком и текст ошибки фильтра."""
    journal(connection, "course.created", entity="course",
            course_id=world["course"], marker="маркер-всегда")
    as_admin(client)

    text = audit(client, query)
    assert config.MESSAGES["audit_filter_invalid"] in text
    assert "маркер-всегда" not in text


def test_empty_state_uses_the_spec_phrase_and_offers_reset(connection, world, client):
    """§9.2: точная фраза пустого состояния и кнопка сброса фильтров."""
    as_admin(client)
    text = audit(client, "?role=student")
    assert config.EMPTY_STATES["audit_log"] in text
    assert "Сбросить фильтры" in text


# --------------------------------------------------------------------------
# Экран: поля, порядок, отсутствие пагинации (FR-61, §4.2 п. 15)
# --------------------------------------------------------------------------


def test_screen_shows_all_fields_of_a_record(connection, world, client):
    assignment = insert_assignment(
        connection, course_id=world["course"], title="Домашняя работа 1"
    )
    journal(
        connection,
        "submission.status_changed",
        role="curator",
        entity="submission",
        actor_id=world["curator"],
        entity_id=7,
        course_id=world["course"],
        group_id=world["group"],
        assignment_id=assignment,
        from_status="submitted",
        to_status="revision",
        details={"attempt_no": 2, "student": "Студент Первый"},
    )
    as_admin(client)
    text = audit(client)

    assert "Смена статуса" in text
    assert "submission.status_changed" in text
    assert CURATOR_LOGIN[0] in text
    assert "Куратор" in text
    assert "Сдача №7" in text
    assert "Курс: Основной курс" in text
    assert "Группа: ФК-1" in text
    assert "Задание: Домашняя работа 1" in text
    assert "На проверке → На доработку" in text
    assert "attempt_no: 2; student: Студент Первый" in text
    assert re.search(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", text)


def test_checklist_events_are_visible_on_screen(connection, world, client):
    """Чек-лист §14 п. 229: вход, смена учётных данных, CRUD курса,
    импорт, сдача, смена статуса, скачивание файла, выдача пароля."""
    for action, entity, kwargs in (
        ("auth.credentials_changed", "user", {}),
        ("course.created", "course", {"course_id": world["course"]}),
        ("course.updated", "course", {"course_id": world["course"]}),
        ("course.deleted", "course", {"course_id": world["course"]}),
        ("import.committed", "import", {"course_id": world["course"]}),
        ("submission.created", "submission",
         {"course_id": world["course"], "to_status": "submitted"}),
        ("submission.status_changed", "submission",
         {"course_id": world["course"], "from_status": "submitted",
          "to_status": "accepted"}),
        ("file.downloaded", "file", {"course_id": world["course"]}),
        ("pwd.reissued_by_admin", "user", {}),
    ):
        journal(connection, action, entity=entity, **kwargs)
    as_admin(client)
    text = audit(client)

    for phrase in (
        "Вход",  # сам вход администратора в эту сессию
        "Смена логина и пароля",
        "Курс создан",
        "Курс изменён",
        "Курс удалён",
        "Импорт подтверждён",
        "Сдача отправлена",
        "Смена статуса",
        "Файл скачан",
        "Повторная выдача пароля",
    ):
        assert phrase in text, phrase


def test_rows_are_newest_first_and_all_fit_one_page(connection, world, client):
    """Раздел 4.2, п. 15: пагинации нет — вся выборка на одном экране."""
    journal(connection, "course.created", entity="course",
            course_id=world["course"], marker="маркер-первый")
    journal(connection, "course.created", entity="course",
            course_id=world["course"], marker="маркер-второй")
    as_admin(client)
    text = audit(client)

    assert "маркер-первый" in text and "маркер-второй" in text
    assert text.index("маркер-второй") < text.index("маркер-первый")


def test_details_are_escaped(connection, world, client):
    """Детали — данные, а не разметка: журнал нельзя использовать
    как носитель HTML."""
    journal(connection, "course.created", entity="course",
            course_id=world["course"],
            details={"опасно": "<b>жирный</b>"})
    as_admin(client)
    text = audit(client)
    assert "<b>жирный</b>" not in text
    assert "&lt;b&gt;жирный&lt;/b&gt;" in text


def test_anonymized_author_is_shown_on_screen(connection, world, client):
    journal(connection, "course.deleted", entity="course",
            course_id=world["course"], marker="маркер-удаление")
    as_admin(client)
    text = audit(client)
    assert "маркер-удаление" in text
    assert "Автор обезличен" in text


# --------------------------------------------------------------------------
# Выгрузка CSV (FR-63, AC-32)
# --------------------------------------------------------------------------


def test_export_contains_the_same_rows_as_the_screen(connection, world, client):
    journal(connection, "course.created", entity="course",
            course_id=world["course"], marker="маркер-экспорт")
    journal(connection, "submission.created", role="student",
            entity="submission", course_id=world["course"],
            marker="маркер-сдача")
    as_admin(client)

    text = audit(client)
    rows = export(client)
    # Один заголовок <tr> в таблице экрана и одна строка данных на
    # запись журнала — экран и файл показывают одну и ту же выборку.
    assert text.count("<tr>") - 1 == len(rows)
    joined = "\n".join(";".join(row) for row in rows)
    assert "маркер-экспорт" in joined
    assert "маркер-сдача" in joined


def test_export_respects_filters(connection, world, client):
    journal(connection, "group.created", role="curator", entity="group",
            course_id=world["course"], marker="маркер-куратор")
    journal(connection, "course.created", role="admin", entity="course",
            course_id=world["course"], marker="маркер-админ")
    as_admin(client)

    rows = export(client, "?role=curator")
    joined = "\n".join(";".join(row) for row in rows)
    assert "маркер-куратор" in joined
    assert "маркер-админ" not in joined


def test_export_without_matches_has_only_the_header(connection, world, client):
    as_admin(client)
    rows = export(client, "?role=student")
    assert rows == []


def test_export_has_statuses_and_details(connection, world, client):
    journal(
        connection,
        "submission.status_changed",
        role="curator",
        entity="submission",
        course_id=world["course"],
        from_status="submitted",
        to_status="revision",
        details={"attempt_no": 1},
    )
    as_admin(client)
    rows = export(client, "?entity=submission")
    assert len(rows) == 1
    row = rows[0]
    # 0: дата, 2: роль, 3: подпись, 4: код, 5: тип, 10/11: статусы, 12: детали.
    assert row[2] == "Куратор"
    assert row[3] == "Смена статуса"
    assert row[4] == "submission.status_changed"
    assert row[5] == "Сдача"
    assert row[10] == "На проверке"
    assert row[11] == "На доработку"
    assert row[12] == "attempt_no: 1"


# --------------------------------------------------------------------------
# FR-64: ни паролей, ни IP-адресов
# --------------------------------------------------------------------------


def test_screen_and_export_contain_no_passwords_and_no_ip(connection, world, client):
    """FR-64, AC-32: известные пароли и IP-подобные строки отсутствуют."""
    journal(connection, "pwd.reissued_by_admin", entity="user",
            course_id=world["course"], marker="маркер-пароль")
    journal(connection, "file.downloaded", entity="file",
            course_id=world["course"], marker="маркер-файл")
    as_admin(client)

    text = audit(client)
    assert ADMIN_LOGIN[1] not in text
    assert CURATOR_LOGIN[1] not in text
    assert STUDENT_LOGIN[1] not in text
    assert not IP_RE.search(text)

    rows = export(client)
    joined = "\n".join(";".join(row) for row in rows)
    assert ADMIN_LOGIN[1] not in joined
    assert CURATOR_LOGIN[1] not in joined
    assert STUDENT_LOGIN[1] not in joined
    assert not IP_RE.search(joined)


# --------------------------------------------------------------------------
# Доступ (FR-63, BR-33)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/admin/audit", "/admin/audit/export"])
def test_screen_is_closed_to_other_roles(connection, world, client, path):
    response = client.get(path)
    assert "/login" in response.headers["Location"]

    login(client, CURATOR_LOGIN)
    assert client.get(path).status_code == 403

    login(client, STUDENT_LOGIN)
    assert client.get(path).status_code == 403


def test_cabinet_offers_the_journal_to_admin_only(connection, world, client):
    as_admin(client)
    cabinet = client.get("/")
    assert cabinet.status_code == 200
    assert "/admin/audit" in cabinet.text
    assert "Журнал действий" in cabinet.text

    login(client, STUDENT_LOGIN)
    cabinet = client.get("/")
    assert "/admin/audit" not in cabinet.text
    assert "Журнал действий" not in cabinet.text
