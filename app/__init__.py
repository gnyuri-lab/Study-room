"""Фабрика приложения.

Этап Э-2 добавил сюда три вещи: ключ подписи сессии из
``var/secret.key``, регистрацию ``auth``-blueprint и глобтный барьер на
``before_request``. Барьер нужен, чтобы правило «рабочие экраны
недоступны до смены логина и пароля» (FR-02) нельзя было обойти,
просто не повесив декоратор на конкретную функцию.
"""

from typing import Any

from flask import Flask, current_app, redirect, render_template, render_template_string, request, url_for
from jinja2 import TemplateNotFound
from werkzeug import exceptions as werkzeug_exceptions

from app import access
from app import config
from app import db
from app import security

#: Куда система обязана отпускать без сеанса: сам вход и статика.
#: Всё остальное закрыто.
PUBLIC_ENDPOINTS = frozenset(
    {
        "auth.login",
        "static",
        "favicon",
    }
)

#: Куда можно, пока `must_change_password = 1`. Выход здесь тоже
#: разрешён: иначе учётную запись, которую просят сменить пароль,
#: невозможно будет покинуть.
CREDENTIALS_ENDPOINTS = frozenset(
    {
        "auth.change_credentials",
        "auth.change_password",
        "auth.logout",
    }
)

CABINET_TEMPLATE = """{% extends "base.html" %}
{% block page_title %}Учебный кабинет{% endblock %}
{% block content %}
<section class="card">
  <h2>{{ user['full_name'] }}</h2>
  <p class="muted">
    Роль: {{ ROLES[user['role']].label }} · логин: {{ user['email'] }}
  </p>
  <div class="form-actions">
    <a class="button button-secondary" href="{{ url_for('auth.change_password') }}">
      Сменить пароль
    </a>
    {% if user['role'] == 'student' %}
      <a class="button" href="{{ url_for('student.courses_list') }}">
        Мои курсы
      </a>
    {% endif %}
    {% if user['role'] == 'admin' %}
      <a class="button" href="{{ url_for('admin_courses.dashboard') }}">
        Дашборд
      </a>
      <a class="button" href="{{ url_for('admin_courses.course_create') }}">
        Создать курс
      </a>
      <a class="button button-secondary" href="{{ url_for('admin_courses.courses_list') }}">
        Редактировать курс / группы студентов
      </a>
      <a class="button button-secondary" href="{{ url_for('admin_audit.index') }}">
        Журнал действий
      </a>
    {% endif %}
    {% if user['role'] == 'curator' %}
      <a class="button" href="{{ url_for('curator.dashboard') }}">
        Дашборд
      </a>
    {% endif %}  </div>
</section>

<section class="card">
  <h3>Доступные курсы</h3>
  {% if courses %}
    <ul class="plain-list card-list">
      {% for course in courses %}
        <li class="card-row">
          <div class="card-row-main">
            <p class="card-row-title">
              {% if user['role'] == 'student' %}
                <a class="button button-secondary"
                   href="{{ url_for('student.assignments_list', course_id=course.id) }}">
                  {{ course.title }}
                </a>
              {% else %}
                {# Куратору и администратору курс открыт списком групп:
                   там же дальше уходят участники и задания. #}
                <a class="button button-secondary"
                   href="{{ url_for('admin_groups.groups_list', course_id=course.id) }}">
                  {{ course.title }}
                </a>
              {% endif %}
            </p>
          </div>
          <div class="card-row-actions">
            {% if user['role'] != 'student' %}
              <a class="button"
                 href="{{ url_for('admin_assignments.assignments_list', course_id=course.id) }}">
                Задания
              </a>
            {% endif %}
          </div>
        </li>
      {% endfor %}
    </ul>
  {% else %}
    <p class="muted">{{ EMPTY_STATES['admin_courses'] if user['role'] == 'admin'
        else 'Курсы появятся после того, как администратор заведёт вас в список.' }}</p>
  {% endif %}
</section>

{% if user['role'] == 'admin' %}
<section class="card">
  <h3>Группы</h3>
  {% if groups %}
    <ul class="plain-list card-list">
      {% for group in groups %}
        <li class="card-row">
          <div class="card-row-main">
            <p class="card-row-title">{{ group.name }}</p>
            <p class="muted">{{ group.course_title }}</p>
          </div>
          <div class="card-row-actions">
            <a class="button button-secondary"
               href="{{ url_for('admin_groups.group_members', group_id=group.id) }}">
              Участники
            </a>
            {# Администратор смотрит работы на тех же экранах, что и
               куратор: отдельного раздела работ для него нет (Э-6). #}
            <a class="button"
               href="{{ url_for('curator.group_students', group_id=group.id) }}">
              Работы
            </a>
          </div>
        </li>
      {% endfor %}
    </ul>
  {% else %}
    <p class="muted">Групп пока нет. Создайте курс и группу в разделе курсов.</p>
  {% endif %}
</section>
{% else %}
<section class="card">
  <h3>Мои группы</h3>
  {% if groups %}
    <ul class="plain-list card-list">
      {% for group in groups %}
        <li class="card-row">
          <div class="card-row-main">
            <p class="card-row-title">
              {% if user['role'] == 'curator' %}
                <a class="button button-secondary"
                   href="{{ url_for('admin_groups.group_members', group_id=group.id) }}">
                  {{ group.name }}
                </a>
              {% else %}
                {{ group.name }}
              {% endif %}
            </p>
            <p class="muted">{{ group.course_title }}</p>
          </div>
          {% if user['role'] == 'curator' %}
            <div class="card-row-actions">
              <a class="button button-secondary"
                 href="{{ url_for('admin_groups.group_members', group_id=group.id) }}">
                Участники
              </a>
              <a class="button"
                 href="{{ url_for('curator.group_students', group_id=group.id) }}">
                Работы
              </a>
            </div>
          {% endif %}
        </li>
      {% endfor %}
    </ul>
  {% else %}
    <p class="muted">Вы пока не состоите ни в одной группе.</p>
  {% endif %}
</section>
{% endif %}

<section class="card">
  <h3>Пилот собран</h3>
  <p class="muted">Все этапы Э-0…Э-9 выполнены. Демо-данные создаёт
    команда <code>python -m app.cli seed-demo</code>.</p>
</section>
{% endblock %}
"""

FALLBACK_ERROR_TEMPLATE = """{% extends "base.html" %}
{% block page_title %}{{ status_code }} — {{ title }}{% endblock %}
{% block content %}
<section class="card">
  <h2>{{ status_code }} — {{ title }}</h2>
  <p>{{ message }}</p>
  <p><a class="button" href="{{ home_url }}">На главную</a></p>
</section>
{% endblock %}
"""

ERROR_PAGES = {
    403: ("Отказ в правах", "У вас нет прав на это действие."),
    404: ("Объект не найден", "Объект не найден или был удалён."),
    500: ("Внутренняя ошибка", "Произошла ошибка. Попробуйте повторить действие."),
}


def create_app(overrides: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config)
    if overrides:
        app.config.update(overrides)

    _apply_session_settings(app)
    db.init_app(app)
    _register_template_globals(app)
    _register_context_processors(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    _register_access_gate(app)
    _register_cabinet(app)

    return app


# --------------------------------------------------------------------------
# Сессия
# --------------------------------------------------------------------------


def _load_secret_key() -> str:
    """Прочитать ключ подписи cookie из файла.

    Ключ шифрования паролей лежит в том же файле, но это разные вещи:
    подпись сессии нужна всегда, даже когда паролей ещё нет.
    """
    path = config.SECRET_KEY_PATH
    if not path.is_file():
        raise RuntimeError(
            "Файл ключа {path} не найден. Выполните "
            "`python -m app.cli init-key` перед запуском.".format(path=path)
        )
    return security.session_secret(path)


def _apply_session_settings(app: Flask) -> None:
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_secret_key()
    app.config["SESSION_COOKIE_NAME"] = config.SESSION_COOKIE_NAME
    app.config["SESSION_COOKIE_HTTPONLY"] = config.SESSION_COOKIE_HTTPONLY
    app.config["SESSION_COOKIE_SAMESITE"] = config.SESSION_COOKIE_SAMESITE
    app.config["PERMANENT_SESSION_LIFETIME"] = config.PERMANENT_SESSION_LIFETIME
    # Приложение слушает только 127.0.0.1, поэтому Secure выставить
    # нельзя: по http://127.0.0.1 такой cookie не вернётся.


# --------------------------------------------------------------------------
# Регистрация
# --------------------------------------------------------------------------


def _register_template_globals(app: Flask) -> None:
    app.jinja_env.globals.update(
        APP_NAME=config.APP_NAME,
        APP_TITLE=config.APP_TITLE,
        APP_VERSION=config.APP_VERSION,
        STAGE=config.STAGE,
        STATUSES=config.STATUSES,
        STATUS_ORDER=config.STATUS_ORDER,
        ROLES=config.ROLES,
        MESSAGES=config.MESSAGES,
        EMPTY_STATES=config.EMPTY_STATES,
        # Лимиты нужны шаблону поля выбора файлов: подсказка под полем
        # и подпись состояния «превышение лимита» (§9.1) должны
        # показывать те же числа, что проверяет сервер.
        MAX_FILES_PER_ASSIGNMENT=config.MAX_FILES_PER_ASSIGNMENT,
        MAX_FILE_SIZE_BYTES=config.MAX_FILE_SIZE_BYTES,
    )


def _register_context_processors(app: Flask) -> None:
    @app.context_processor
    def inject_current_user() -> dict[str, Any]:
        # Шаблон должен знать текущего пользователя, но получать его
        # из сессии вручную в каждом шаблоне нельзя — это расползлось бы.
        user = access.current_user()
        return {
            "current_user": user,
            # Одна проверка вместо повторов `current_user['role'] == 'admin'`
            # в каждом шаблоне: кнопки правки рисуются только администратору.
            "is_admin": user is not None and user["role"] == access.ADMIN_ROLE,
        }


def _register_blueprints(app: Flask) -> None:
    from app.blueprints import BLUEPRINTS

    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)


def _register_access_gate(app: Flask) -> None:
    @app.before_request
    def enforce_access():
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS:
            return None
        if not access.is_authenticated():
            return redirect(
                url_for("auth.login", next=request.full_path.rstrip("?"))
            )
        if access.must_change_password() and endpoint not in CREDENTIALS_ENDPOINTS:
            return redirect(url_for("auth.change_credentials"))
        return None


def _register_cabinet(app: Flask) -> None:
    @app.get("/")
    def index() -> str:
        user = access.current_user()
        return render_template_string(CABINET_TEMPLATE, **_cabinet_context(user))

    @app.get("/favicon.ico")
    def favicon() -> tuple[str, int]:
        return "", 204


def _cabinet_context(user) -> dict[str, Any]:
    """Курсы и группы, доступные текущему пользователю (BR-28, BR-29).

    Человек может быть заведён на двух курсах отдельными строками
    ``users`` (e-mail уникален в пределах курса), поэтому списки здесь
    собираются не по сеансной строке, а по всем его записям
    (``access.identity_rows``) — иначе студент, вошедший «первой»
    строкой, не увидел бы второй курс.
    """
    courses: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    connection = db.get_db()

    if user["role"] == "admin":
        rows = connection.execute(
            "SELECT id, title FROM courses ORDER BY title, id"
        ).fetchall()
        # Группы нужны администратору, чтобы дойти до работ (Э-6):
        # работы лежат в группе, а не в курсе. Архивные группы в кабинет
        # не выносятся — они открываются из списка групп курса.
        group_rows = connection.execute(
            """
            SELECT g.id, g.name, g.course_id, c.title AS course_title
              FROM groups g
              JOIN courses c ON c.id = g.course_id
             WHERE g.archived_at IS NULL
             ORDER BY c.title, g.name, g.id
            """
        ).fetchall()
        return {
            "user": user,
            "courses": [{"id": row["id"], "title": row["title"]} for row in rows],
            "groups": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "course_id": row["course_id"],
                    "course_title": row["course_title"],
                }
                for row in group_rows
            ],
        }

    siblings = access.identity_rows(user)
    marks = ",".join("?" for _ in siblings)
    membership_table = (
        "curator_groups" if user["role"] == "curator" else "group_members"
    )
    # Архивные группы исключены из активных списков (FR-23): работы в них
    # сохранены, но группа больше не участвует в работе курса.
    rows = connection.execute(
        """
        SELECT g.id, g.name, g.course_id, c.title AS course_title
          FROM groups g
          JOIN courses c ON c.id = g.course_id
          JOIN {table} m ON m.group_id = g.id
         WHERE m.user_id IN ({marks}) AND g.archived_at IS NULL
         ORDER BY c.title, g.name, g.id
        """.format(table=membership_table, marks=marks),
        [int(row["id"]) for row in siblings],
    ).fetchall()
    groups = [
        {
            "id": row["id"],
            "name": row["name"],
            "course_id": row["course_id"],
            "course_title": row["course_title"],
        }
        for row in rows
    ]

    # Студент состоит в группе не всегда, но его курс известен и без неё.
    # Куратор курсы видит только те, в которых у него есть группа.
    if user["role"] == "curator":
        seen = set()
        for row in rows:
            if row["course_id"] in seen:
                continue
            seen.add(row["course_id"])
            courses.append({"id": row["course_id"], "title": row["course_title"]})
    else:
        seen = set()
        for row in siblings:
            course_id = row["course_id"]
            if course_id is None or course_id in seen:
                continue
            seen.add(course_id)
            own = connection.execute(
                "SELECT id, title FROM courses WHERE id = ?", (course_id,)
            ).fetchone()
            if own is not None:
                courses.append({"id": own["id"], "title": own["title"]})
        for group in groups:
            if group["course_id"] in seen:
                continue
            seen.add(group["course_id"])
            courses.append(
                {"id": group["course_id"], "title": group["course_title"]}
            )
    return {"user": user, "courses": courses, "groups": groups}


def _register_error_handlers(app: Flask) -> None:
    def custom_description(error: Exception, status_code: int) -> str | None:
        """Текст, который отказавший маршрут задал сам, иначе ``None``.

        У ``abort(403)`` описание всегда есть — это стандартная
        английская подпись HTTP из Werkzeug, а не текст приложения.
        Без проверки на подставной она молча вытеснила бы русские
        сообщения на всех страницах отказа. Поэтому своё описание
        отличается от стандартного, и только такое описание берётся.
        """
        description = getattr(error, "description", None)
        if not description:
            return None
        default = getattr(
            werkzeug_exceptions.default_exceptions.get(status_code), "description", None
        )
        return None if description == default else description

    def make_handler(status_code: int):
        def handler(error: Exception) -> tuple[str, int]:
            title, message = ERROR_PAGES[status_code]
            # Маршрут может уточнить отказ своим текстом
            # (``abort(403, description=...)``): например, файл существует,
            # но текущему пользователю не виден. Уточнение важнее
            # общего текста, но берётся только из описания самого
            # исключения и никогда из пользовательского ввода.
            message = custom_description(error, status_code) or message
            context = {
                "status_code": status_code,
                "title": title,
                "message": message,
                "home_url": "/",
            }
            template_name = f"errors/{status_code}.html"
            if _template_exists(template_name):
                return render_template(template_name, **context), status_code
            return render_template_string(
                FALLBACK_ERROR_TEMPLATE, **context
            ), status_code

        return handler

    for status_code in ERROR_PAGES:
        app.register_error_handler(status_code, make_handler(status_code))


def _template_exists(template_name: str) -> bool:
    try:
        current_app.jinja_env.get_template(template_name)
    except TemplateNotFound:
        return False
    return True
