"""Дашборд администратора и куратора (FR-55…FR-60, AC-16, AC-17, AC-41).

Проверяются решения этапа, а не «страница открылась»:

* работы — это пары «студент × задание», у каждой пары ровно один
  статус, поэтому сумма пяти статусов равна числу работ (FR-55, AC-41);
* диаграмма нарисована кодом приложения: в разметке нет ни одной
  ссылки наружу и ни одного ``xmlns`` (FR-57, NFR-03, NFR-04);
* куратор видит только свои группы — чужой студент, группа и курс не
  попадают ни в выборку, ни в опции фильтров, а чужое значение фильтра
  даёт пустую выборку целиком с сообщением (FR-56, FR-08, AC-16);
* таблица успеваемости не содержит e-mail и паролей (FR-59, AC-17,
  AC-22, BR-30);
* три фильтра-идентификатора применяются «и», студент ищется по имени
  подстрокой и подсказывается полным списком ФИО области, а сброс
  чистит только свой ключ (FR-58, AC-41);
* дашборд закрыт студенту и неавторизованному посетителю (BR-33, FR-07).
"""

from __future__ import annotations

import re

import pytest

from app import config, passwords
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
    FOREIGN_LOGIN,
    OTHER_LOGIN,
    STUDENT_LOGIN,
    insert_assignment,
    insert_submission,
)

SECOND_LOGIN = ("second@example.ru", "Пароль123")

ADMIN_URL = "/admin/dashboard"
CURATOR_URL = "/curator/dashboard"

SUMMARY_RE = re.compile(r'<p class="summary-value">(\d+)</p>')
DISTRIBUTION_RE = re.compile(
    r'<tr data-status="(?P<code>\w+)">.*?'
    r'<td data-label="Работ">(?P<value>\d+)</td>',
    re.DOTALL,
)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "HASH_ITERATIONS", 1_000)


@pytest.fixture
def world(connection, key_path):
    """Два курса: в первом группа с двумя студентами, во втором чужие.

    Пять работ по пяти разным статусам считаются так:

    * «Студент Первый» × ДЗ-1 — две попытки, последняя «сдано»;
    * «Студент Второй» × ДЗ-1 — «на доработку»;
    * оба студента × ДЗ-2 — сдач нет, значит «не сдано»;
    * «Студент Чужой» × чужое ДЗ — «на проверке».
    """
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
    second = add_user(
        connection,
        SECOND_LOGIN[0],
        SECOND_LOGIN[1],
        course_id=course,
        full_name="Студент Второй",
    )
    add_student_to_group(connection, group, second)
    admin = add_user(
        connection,
        ADMIN_LOGIN[0],
        ADMIN_LOGIN[1],
        role="admin",
        full_name="Администратор Системы",
    )

    other_course = insert_course(connection, title="Другой курс")
    other_group = insert_group(connection, name="ФК-9", course_id=other_course)
    foreign_curator = add_user(
        connection,
        FOREIGN_LOGIN[0],
        FOREIGN_LOGIN[1],
        role="curator",
        course_id=other_course,
        full_name="Куратор Чужой",
    )
    add_curator_to_group(connection, other_group, foreign_curator)
    other_student = add_user(
        connection,
        OTHER_LOGIN[0],
        OTHER_LOGIN[1],
        course_id=other_course,
        full_name="Студент Чужой",
    )
    add_student_to_group(connection, other_group, other_student)

    first_assignment = insert_assignment(
        connection,
        course_id=course,
        title="Домашняя работа 1",
        due_at="2026-10-01T23:59:00Z",
        order_index=1,
    )
    second_assignment = insert_assignment(
        connection, course_id=course, title="Домашняя работа 2", order_index=2
    )
    foreign_assignment = insert_assignment(
        connection, course_id=other_course, title="Чужое задание", order_index=1
    )

    insert_submission(
        connection, first_assignment, student, attempt_no=1, status="submitted"
    )
    insert_submission(
        connection, first_assignment, student, attempt_no=2, status="accepted"
    )
    insert_submission(
        connection, first_assignment, second, attempt_no=1, status="revision"
    )
    insert_submission(
        connection, foreign_assignment, other_student, attempt_no=1,
        status="submitted",
    )

    return {
        "course": course,
        "group": group,
        "admin": admin,
        "curator": curator,
        "student": student,
        "second": second,
        "assignment": first_assignment,
        "assignment_without_due": second_assignment,
        "other_course": other_course,
        "other_group": other_group,
        "other_student": other_student,
        "foreign_curator": foreign_curator,
        "foreign_assignment": foreign_assignment,
    }


def login(client, credentials):
    client.post("/logout")
    response = client.post(
        "/login",
        data={"email": credentials[0], "password": credentials[1]},
        follow_redirects=True,
    )
    assert "Выйти" in response.text
    return response


def as_admin(client):
    return login(client, ADMIN_LOGIN)


def as_curator(client):
    return login(client, CURATOR_LOGIN)


def as_student(client):
    return login(client, STUDENT_LOGIN)


def summary(html):
    """Сводные показатели в порядке вывода: студенты, задания, работы,
    сдано."""
    values = [int(value) for value in SUMMARY_RE.findall(html)]
    assert len(values) == 4, html
    return values


def has_summary(html) -> bool:
    """Показаны ли сводные показатели: без данных вместо них пустое
    состояние (§9.2), поэтому нули в разметке не встречаются."""
    return "Сводные показатели" in html


def distribution(html):
    """Числа строки распределения: ``код статуса → работ``."""
    found = DISTRIBUTION_RE.findall(html)
    assert len(found) == len(config.STATUS_ORDER), html
    return {code: int(value) for code, value in found}


def filter_value(html, key):
    """Что выбрано в списке ``key`` (пустая строка — ничего не выбрано)."""
    block = re.search(
        r'<select[^>]*name="{0}"[^>]*>(.*?)</select>'.format(re.escape(key)),
        html,
        re.DOTALL,
    )
    assert block is not None, html
    selected = re.search(r'<option value="(\d+)"[^>]*selected', block.group(1))
    return selected.group(1) if selected else ""


def filter_text(html, key):
    """Текст поискового поля ``key`` (пустая строка — пустое поле)."""
    tag = re.search(
        r'<input[^>]*name="{0}"[^>]*>'.format(re.escape(key)),
        html,
    )
    assert tag is not None, html
    value = re.search(r'value="([^"]*)"', tag.group(0))
    return value.group(1) if value else ""


# --------------------------------------------------------------------------
# Администратор: числа, диаграмма и разрезы (FR-55, FR-57, FR-60, AC-41)
# --------------------------------------------------------------------------


def test_admin_dashboard_counts_every_pair_once(client, world):
    as_admin(client)
    response = client.get(ADMIN_URL)

    assert response.status_code == 200
    html = response.text
    # Три студента, три задания, пять пар, одна принятая работа.
    assert summary(html) == [3, 3, 5, 1]
    # Каждая пара учтена ровно один раз: сумма статусов = работ.
    counts = distribution(html)
    assert counts == {
        "not_submitted": 2,
        "submitted": 1,
        "revision": 1,
        "accepted": 1,
        "accepted_late": 0,
    }
    assert sum(counts.values()) == 5
    assert "Все курсы" in html


def test_summary_counts_only_objects_with_works(client, world):
    as_admin(client)
    # Фильтр по группе сужает только студентов: задания области
    # видимости остаются все три, включая чужое задание другого
    # курса. Сводка обязана показать пары, а не сырой список опций —
    # иначе «Заданий» разойдётся с разрезом «Прогресс по заданиям».
    html = client.get(ADMIN_URL, query_string={"group": world["group"]}).text

    # Группа ФК-1: два студента × два задания своего курса.
    assert summary(html) == [2, 2, 4, 1]


def test_admin_dashboard_chart_is_local_svg(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL).text

    assert 'class="chart"' in html
    assert 'aria-label="Распределение работ по статусам' in html
    # Ни внешних ссылок, ни пространств имён: картинку рисует сам
    # код приложения (NFR-03, NFR-04).
    assert "http" not in html.lower()
    assert "xmlns" not in html
    # Каждая полоса связана со строкой таблицы того же статуса.
    for code in config.STATUS_ORDER:
        assert 'class="chart-bar chart-bar--{0}" data-status="{0}"'.format(code) in html


def test_admin_dashboard_breakdowns_show_four_views(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL).text

    for heading in (
        "Прогресс по курсам",
        "Прогресс по группам",
        "Прогресс по заданиям",
        "Прогресс по студентам",
    ):
        assert heading in html
    assert 'data-label="Курс">Основной курс<' in html
    assert 'data-label="Курс">Другой курс<' in html
    assert 'data-label="Группа">ФК-1<' in html
    assert 'data-label="Задание">Домашняя работа 1<' in html
    assert 'data-label="Студент">Студент Второй<' in html
    # Дедлайн есть только у первого задания, второй показывает «не задан».
    assert "не задан" in html


def test_admin_dashboard_performance_table_has_no_contacts(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL).text

    assert "Таблица успеваемости" in html
    assert "Студент Первый" in html
    assert "ФК-1" in html
    # Ни адресов, ни следов полей пароля (AC-17, AC-22, BR-30).
    for credentials in (
        ADMIN_LOGIN,
        CURATOR_LOGIN,
        STUDENT_LOGIN,
        SECOND_LOGIN,
        OTHER_LOGIN,
        FOREIGN_LOGIN,
    ):
        assert credentials[0] not in html
    assert "@" not in html
    # Имена файлов скриптов в подвале содержат слово password — это
    # разметка, а не данные учётной записи; важны поля и значения.
    assert 'type="password"' not in html
    assert "password_enc" not in html
    assert "password_hash" not in html
    assert "Пароль" not in html


def test_admin_dashboard_deadline_column_is_marked(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL).text

    # Дата дедлайна подсвечена красным через класс ячейки, шапка
    # колонки — тоже, а отсутствие дедлайна остаётся приглушённым —
    # это не дата (замечание приёмки 01.10.2026).
    assert '<td class="deadline" data-label="Дедлайн">' in html
    assert '<th scope="col" class="deadline">Дедлайн</th>' in html
    assert '<span class="muted">не задан</span>' in html


def test_admin_dashboard_links_to_work_and_scripts(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL).text

    # Работа открывается с дашборда, а не только из группы.
    assert "/curator/submissions/" in html
    assert "filters.js" in html
    assert "charts.js" in html


def test_admin_dashboard_marks_archived_assignment(client, world, connection):
    archived = insert_assignment(
        connection,
        course_id=world["course"],
        title="Архивное задание",
        order_index=3,
        archived_at="2026-09-01T00:00:00Z",
    )
    as_admin(client)
    html = client.get(ADMIN_URL).text

    # Архивные работы сохраняются и участвуют в распределении, но
    # помечены — и в опции фильтра, и в разрезе (чек-лист §12).
    assert "Архивное задание (архив)" in html
    # Появились две новые пары: по одной на каждого студента курса.
    assert summary(html)[2] == 7
    assert archived > 0


def test_groupless_student_gets_own_breakdown_row(client, world, connection):
    add_user(
        connection,
        "alone@example.ru",
        "Пароль123",
        course_id=world["course"],
        full_name="Студент Без Группы",
    )
    as_admin(client)
    html = client.get(ADMIN_URL).text

    # Работа студента без группы не пропадает из итогов: у неё своя
    # строка, а не молчаливое исключение.
    assert 'data-label="Группа">Без группы<' in html
    # Три студента курса × два задания + чужая пара.
    assert summary(html) == [4, 3, 7, 1]


# --------------------------------------------------------------------------
# Куратор: область видимости (FR-56, AC-16, BR-29)
# --------------------------------------------------------------------------


def test_curator_dashboard_shows_only_own_groups(client, world):
    as_curator(client)
    response = client.get(CURATOR_URL)

    assert response.status_code == 200
    html = response.text
    assert "Мои группы" in html
    # Свои данные на месте.
    assert "Студент Первый" in html
    assert "Студент Второй" in html
    assert "ФК-1" in html
    assert summary(html) == [2, 2, 4, 1]
    # Чужие — не видны нигде: ни в выборке, ни в опциях фильтров.
    assert "Студент Чужой" not in html
    assert "ФК-9" not in html
    assert "Другой курс" not in html
    assert "Чужое задание" not in html


def test_curator_foreign_filter_value_gives_empty_result(client, world):
    as_curator(client)
    # Свой курс и чужая группа вместе: выборка пуста целиком, а не
    # «по своему курсу хотя бы что-то осталось».
    query = "?course={0}&group={1}".format(world["course"], world["other_group"])
    html = client.get(CURATOR_URL + query).text

    # Чужое значение не отличается от несуществующего: пустая выборка
    # целиком и сообщение без подтверждения, что объект существует
    # (FR-08, AC-16).
    assert config.MESSAGES["dashboard_filter_invalid"] in html
    assert config.EMPTY_STATES["dashboard_filtered"] in html
    assert "Студент Чужой" not in html
    assert not has_summary(html)


def test_curator_name_search_cannot_reach_foreign_students(client, world):
    as_curator(client)
    html = client.get(CURATOR_URL, query_string={"student": "Чужой"}).text

    # Поиск идёт только по студентам области видимости: имя чужого
    # не выдаётся ни в выборке, ни в подсказке, а совпадений нет —
    # это пустая выборка, а не сообщение о неверном фильтре.
    assert "Студент Чужой" not in html
    assert config.MESSAGES["dashboard_filter_invalid"] not in html
    assert config.EMPTY_STATES["dashboard_filtered"] in html
    assert not has_summary(html)


def test_admin_can_open_curator_dashboard(client, world):
    as_admin(client)
    response = client.get(CURATOR_URL)

    # §3: у администратора «все дашборды» — маршрут не отрезает его
    # от экрана куратора, а область видимости считает сервис.
    assert response.status_code == 200
    assert "Все курсы" in response.text


# --------------------------------------------------------------------------
# Фильтры (FR-58, AC-41)
# --------------------------------------------------------------------------


def test_student_field_is_name_search_with_full_name_suggestions(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL).text

    # Выпадающего списка-идентификатора больше нет — вместо него поле
    # поиска (замена по замечаниям заказчика 01.10.2026). Но поле не
    # слепое: под ним лежит список полных ФИО, из которого администратор
    # и куратор выбирают значение — при вводе букв показываются все
    # подходящие.
    assert re.search(r'<input[^>]*type="search"[^>]*name="student"', html)
    assert not re.search(r'<select[^>]*name="student"', html)
    assert 'placeholder="Часть ФИО"' in html
    assert 'data-suggest' in html
    assert 'data-value="Студент Первый">Студент Первый</li>' in html
    assert 'data-value="Студент Второй">Студент Второй</li>' in html
    # Администратор видит в подсказках всех студентов своих курсов.
    assert 'data-value="Студент Чужой">Студент Чужой</li>' in html
    # Три остальных поля остались списками.
    for key in ("course", "group", "assignment"):
        assert re.search(r'<select[^>]*name="{0}"'.format(key), html)


def test_suggestions_cover_whole_scope_not_filtered_rows(client, world):
    as_admin(client)
    # Фильтр по группе сужает выборку, но не подсказки: список ФИО
    # считается по всей области, как и опции остальных полей, — иначе
    # выбор в одном поле скрыл бы значение другого (FR-58).
    html = client.get(ADMIN_URL, query_string={"group": world["group"]}).text

    assert summary(html) == [2, 2, 4, 1]
    assert 'data-value="Студент Чужой">Студент Чужой</li>' in html


def test_curator_suggestions_exclude_foreign_names(client, world):
    as_curator(client)
    html = client.get(CURATOR_URL).text

    # Список подсказок наследует область видимости: чужое ФИО не
    # показывается ни в выборке, ни в предлагаемых именах (BR-29).
    assert 'data-value="Студент Первый">Студент Первый</li>' in html
    assert 'data-value="Студент Второй">Студент Второй</li>' in html
    assert 'data-value="Студент Чужой"' not in html
    assert "ФК-9" not in html


def test_filters_combine_with_and(client, world):
    as_admin(client)
    query = {"course": world["course"], "student": "Студент Первый"}
    html = client.get(ADMIN_URL, query_string=query).text

    # Оба значения применены одновременно: студент и оба задания его
    # курса, то есть две пары.
    assert summary(html) == [1, 2, 2, 1]
    assert filter_value(html, "course") == str(world["course"])
    assert filter_text(html, "student") == "Студент Первый"


def test_name_search_matches_part_of_name_ignoring_case(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL, query_string={"student": "первый"}).text

    # Подстрока в нижнем регистре находит «Студент Первый» — и только
    # его: «Студент Второй» в выборку не попадает.
    assert summary(html) == [1, 2, 2, 1]
    assert filter_text(html, "student") == "первый"


def test_name_search_without_matches_is_empty_not_error(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL, query_string={"student": "Никого"}).text

    # Поиск — не ссылка на объект: совпадений нет, значит это
    # обычная пустая выборка подставленным фильтром, а не сообщение
    # о неверном фильтре (FR-08 остаётся для курса, группы и задания).
    assert config.MESSAGES["dashboard_filter_invalid"] not in html
    assert config.EMPTY_STATES["dashboard_filtered"] in html
    assert not has_summary(html)


def test_reset_clears_only_its_own_filter(client, world):
    as_admin(client)
    both = {"course": world["course"], "student": "Студент Первый"}
    combined = client.get(ADMIN_URL, query_string=both).text
    assert summary(combined) == [1, 2, 2, 1]

    # Сброс поиска оставляет выбранный курс.
    one = client.get(ADMIN_URL, query_string=dict(both, reset="student")).text
    assert summary(one) == [2, 2, 4, 1]
    assert filter_value(one, "course") == str(world["course"])
    assert filter_text(one, "student") == ""

    # Сброс «всех» возвращает исходную выборку.
    none = client.get(ADMIN_URL, query_string=dict(both, reset="all")).text
    assert summary(none) == [3, 3, 5, 1]
    assert filter_value(none, "course") == ""


def test_unknown_filter_value_empties_selection(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL + "?course=9999").text

    assert config.MESSAGES["dashboard_filter_invalid"] in html
    assert config.EMPTY_STATES["dashboard_filtered"] in html
    assert not has_summary(html)


def test_non_numeric_filter_value_is_rejected(client, world):
    as_admin(client)
    html = client.get(ADMIN_URL + "?course=abc").text

    # Подставленное значение не может ничего изменить: оно считается
    # неверным фильтром, а не молча отбрасывается.
    assert config.MESSAGES["dashboard_filter_invalid"] in html
    assert not has_summary(html)


# --------------------------------------------------------------------------
# Доступ (BR-33, FR-07) и пустое состояние (§9.2)
# --------------------------------------------------------------------------


def test_student_cannot_open_dashboards(client, world):
    as_student(client)

    assert client.get(ADMIN_URL).status_code == 403
    assert client.get(CURATOR_URL).status_code == 403


def test_anonymous_is_redirected_to_login(client, world):
    response = client.get(ADMIN_URL)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_empty_database_shows_empty_state(connection, key_path, client):
    # Вход по-прежнему нужен: дашборд закрыт анонимам (BR-33).
    add_user(
        connection,
        ADMIN_LOGIN[0],
        ADMIN_LOGIN[1],
        role="admin",
        full_name="Администратор Системы",
    )
    as_admin(client)
    html = client.get(ADMIN_URL).text

    assert config.EMPTY_STATES["dashboard"] in html
    assert not has_summary(html)
    assert 'class="chart"' not in html
