from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
MIGRATIONS_DIR = BASE_DIR / "migrations"
DATA_DIR = BASE_DIR / "data"
VAR_DIR = BASE_DIR / "var"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "study_room.db"
SECRET_KEY_PATH = VAR_DIR / "secret.key"

APP_NAME = "Study room"
APP_TITLE = "Study room / Учебный кабинет"
STAGE = "Э-11"
APP_VERSION = "0.11.0"

DEBUG = False
TESTING = False
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000

SESSION_COOKIE_NAME = "study_room_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
PERMANENT_SESSION_LIFETIME = 60 * 60 * 8

DEFAULT_ADMIN_EMAIL = "admin@admin.com"
DEFAULT_ADMIN_PASSWORD = "1234"
DEFAULT_ADMIN_FULL_NAME = "Администратор"

PASSWORD_MIN_LENGTH = {"admin": 8, "curator": 5, "student": 5}
PASSWORD_MIN_LENGTH_DEFAULT = 5

SUBMISSION_ATTEMPT_LIMIT = 3

MAX_FILE_SIZE_BYTES = 10_485_760

#: Резервная копия базы (Э-10). Файл — один JSON с таблицами и
#: файлами вложений, поэтому его предел больше лимита вложений
#: NFR-06: 256 МБ покрывает и базу, и файлы даже по максимуму
#: (10 МБ × 5 файлов × до 5 вложений на каждую строку трёх таблиц).
BACKUP_MAX_BYTES = 256 * 1024 * 1024

#: Подкаталог для незавершённой загрузки копии — внутри ``VAR_DIR``:
#: это состояние одной сессии, а не данные приложения (NFR-08).
#: Путь собирается при обращении, а не здесь, чтобы тесты,
#: подменяющие ``VAR_DIR``, получали своё значение.
BACKUP_STAGING_SUBDIR = "backup_staging"

MAX_FILES_PER_ASSIGNMENT = 5
MAX_FILES_PER_SUBMISSION = 5
MAX_FILES_PER_COMMENT = 5

#: Выгрузка ведомости успеваемости (Э-11, FR-72). Имя файла —
#: ``vedomost_[курс]_[группа]_[ФИО-подстрока]_[ДД.ММ.ГГГГ].xlsx`` либо
#: ``vedomost_full_ДД.ММ.ГГГГ.xlsx`` без фильтров; предел длины
#: удерживает имя в лимите Windows, сохраняя дату в конце.
GRADEBOOK_FILENAME_PREFIX = "vedomost"
GRADEBOOK_FILENAME_EMPTY = "full"
GRADEBOOK_FILENAME_MAX = 120
ALLOWED_FILE_EXTENSIONS = (
    "xlsx",
    "xls",
    "docx",
    "pdf",
    "csv",
    "txt",
    "md",
    "png",
    "jpg",
    "zip",
)

EMAIL_MAX_LENGTH = 254
FULL_NAME_MAX_LENGTH = 200
COURSE_TITLE_MAX_LENGTH = 200
COURSE_DESCRIPTION_MAX_LENGTH = 4000
GROUP_NAME_MAX_LENGTH = 100
ASSIGNMENT_TITLE_MAX_LENGTH = 200
ASSIGNMENT_DESCRIPTION_MAX_LENGTH = 20000
COMMENT_MAX_LENGTH = 4000
IMPORT_ROSTER_MAX_ROWS = 5000
#: Импорт списка принимает ровно два формата (FR-24) — не весь набор
#: ``ALLOWED_FILE_EXTENSIONS``: тот список относится к материалам
#: заданий и работам (FR-51…FR-54).
IMPORT_ROSTER_EXTENSIONS = ("xlsx", "csv")

#: Описание задания загружается из файла (FR-33). ТЗ не перечисляет
#: форматы для этого поля — он говорит только «вручную либо из файла», —
#: поэтому приняты только два однозначно текстовых формата из общего
#: списка FR-51. ``.docx`` сюда не входит: это zip-контейнер, и его
#: разбор текста здесь не предусмотрен.
ASSIGNMENT_DESCRIPTION_EXTENSIONS = ("txt", "md")

STATUSES = {
    "not_submitted": {
        "label": "Не сдано",
        "terminal": False,
        "color": "grey",
    },
    "submitted": {
        "label": "На проверке",
        "terminal": False,
        "color": "blue",
    },
    "revision": {
        "label": "На доработку",
        "terminal": False,
        "color": "yellow",
    },
    "accepted": {
        "label": "Сдано",
        "terminal": True,
        "color": "green",
    },
    "accepted_late": {
        "label": "Сдано после дедлайна",
        "terminal": True,
        "color": "orange",
    },
}

STATUS_ORDER = (
    "not_submitted",
    "submitted",
    "revision",
    "accepted",
    "accepted_late",
)

TERMINAL_STATUSES = tuple(
    code for code in STATUS_ORDER if STATUSES[code]["terminal"]
)

ROLES = {
    "admin": {
        "label": "Администратор",
        "belongs_to_course": False,
    },
    "curator": {
        "label": "Куратор",
        "belongs_to_course": True,
    },
    "student": {
        "label": "Студент",
        "belongs_to_course": True,
    },
}

ROLE_ORDER = ("admin", "curator", "student")

ROSTER_ROLE_LABELS = {
    "студент": "student",
    "куратор": "curator",
}

ACTION_CODES = (
    "auth.login",
    "auth.logout",
    "auth.credentials_changed",
    "pwd.viewed_by_admin",
    "pwd.reissued_by_admin",
    "pwd.changed_by_user",
    "course.created",
    "course.updated",
    "course.deleted",
    "group.created",
    "group.updated",
    "group.archived",
    "group.member_added",
    "group.member_updated",
    "group.member_removed",
    "curator.attached",
    "curator.detached",
    "import.previewed",
    "import.committed",
    "assignment.created",
    "assignment.updated",
    "assignment.archived",
    "submission.created",
    "submission.status_changed",
    "submission.review_reverted",
    "file.downloaded",
    "file.deleted",
    # Этап Э-10 (FR-68, FR-69): экспорт и восстановление базы.
    "backup.exported",
    "backup.restored",
)

ENTITY_TYPES = (
    "course",
    "group",
    "assignment",
    "submission",
    "user",
    "file",
    "import",
    # Этап Э-10: объект журнала — сама база данных (FR-68, FR-69).
    "backup",
)

ENTITY_TYPE_LABELS = {
    "course": "Курс",
    "group": "Группа",
    "assignment": "Задание",
    "submission": "Сдача",
    "user": "Учётная запись",
    "file": "Файл",
    "import": "Импорт",
    "backup": "Резервная копия",
}

MESSAGES = {
    "forbidden": "У вас нет прав на это действие.",
    "not_found": "Объект не найден или был удалён.",
    "unauthenticated": "Войдите, чтобы продолжить работу.",
    "credentials_required": "Смените логин и пароль, чтобы продолжить работу.",
    "admin_password_short": "Пароль должен содержать не менее 8 символов.",
    "user_password_short": "Пароль должен содержать не менее 5 символов.",
    "passwords_mismatch": "Пароли не совпадают.",
    "email_taken": "Этот логин уже используется в системе.",
    "email_invalid": "Укажите корректный e-mail: символ @ и домен.",
    "role_invalid": "Допустимые роли: студент или куратор.",
    "group_required": "Укажите группу.",
    "file_too_large": "Файл превышает допустимые 10 МБ.",
    "no_file_access": "У вас нет прав на скачивание этого файла.",
    "no_roster_rows": "В файле нет строк данных.",
    "no_roster_header": "В файле отсутствует строка заголовка с названиями столбцов.",
    "import_report": (
        "Импортировано строк: {корректных} из {всего}. "
        "Строк с ошибками: {ошибок}."
    ),
    # Тексты ниже добавлены на этапе Э-3. §9.3 перечисляет «ключевые»
    # тексты и не содержит сообщений форм администратора, кроме
    # удаления курса и непустой группы. Формулировки предложены в §9.3
    # отдельной правкой ТЗ — так же, как INVALID_CREDENTIALS на Э-2.
    "course_title_required": "Укажите название курса.",
    "course_title_too_long": (
        "Название курса не должно быть длиннее 200 символов."
    ),
    "course_confirm_mismatch": (
        "Название курса введено неверно. Проверьте его и повторите удаление."
    ),
    "group_name_required": "Укажите название группы.",
    "group_name_too_long": (
        "Название группы не должно быть длиннее 100 символов."
    ),
    "group_name_taken": "Группа с таким названием уже есть в курсе.",
    "group_not_empty": (
        "Нельзя удалить группу: в ней {count} участников. "
        "Сначала исключите студентов и открепите кураторов."
    ),
    "full_name_required": "Укажите ФИО.",
    "full_name_too_long": "ФИО не должно быть длиннее 200 символов.",
    "curator_already_attached": "Куратор уже прикреплён к этой группе.",
    "curator_required": "Выберите куратора из списка.",
    "curator_inactive": (
        "Учётная запись куратора отключена. Сначала включите её на курсе."
    ),
    # Тексты ниже добавлены на этапе Э-4 (импорт списка). §9.3
    # перечисляет сообщения о ключевых ситуациях импорта, но не
    # описывает отказ на уровне файла: неверное расширение, нечитаемый
    # файл, слишком много строк и устаревшая загрузка. Формулировки
    # предложены здесь по образцу Э-2 и Э-3 — так же, как INVALID_CREDENTIALS
    # и тексты форм администратора; отдельной правкой ТЗ не оформлялись.
    "roster_extension": "Поддерживаются файлы .xlsx и .csv.",
    "roster_no_file": "Выберите файл со списком.",
    "roster_unreadable": (
        "Не удалось прочитать файл. Проверьте, что это таблица .xlsx или .csv."
    ),
    "roster_too_many_rows": (
        "В файле больше {limit} строк данных. Разделите список на части."
    ),
    "import_session_expired": (
        "Загрузка файла устарела. Выберите файл со списком заново."
    ),
    # Тексты ниже добавлены на этапе Э-5 (задания и вложения). Три из них
    # взяты из §9.3 дословно: «Недопустимый тип файла», «Превышен размер
    # файла» (уже был — «file_too_large») и «Нет прав на файл». Остальные
    # §9.3 не перечисляет: у него нет текстов для полей формы задания и
    # для лимита файлов на задание (в §9.3 он сформулирован для сдачи
    # работы: «Не более 5 файлов на одну работу»). Формулировки
    # предложены здесь по образцу Э-2, Э-3 и Э-4 — отдельной правкой ТЗ не
    # оформлялись.
    "assignment_title_required": "Укажите название задания.",
    "assignment_title_too_long": (
        "Название задания не должно быть длиннее 200 символов."
    ),
    "assignment_due_invalid": (
        "Проверьте дату и время дедлайна: ожидается ГГГГ-ММ-ДД и ЧЧ:ММ."
    ),
    "file_type_invalid": (
        "Недопустимый тип файла: {extension}. Разрешены: {allowed}."
    ),
    "files_limit_assignment": "Не более 5 файлов на задание.",
    "assignment_preview_expired": (
        "Предпросмотр устарел. Заполните форму заново."
    ),
    "description_file_type": (
        "Описание загружается из текстового файла .txt или .md."
    ),
    "description_file_unreadable": (
        "Не удалось прочитать описание из файла. Выберите текстовый файл "
        ".txt или .md."
    ),
    "file_missing_on_disk": (
        "Файл не найден на диске. Обратитесь к администратору."
    ),
    # Тексты ниже добавлены на этапе Э-6 (сдача, статусы, комментарии).
    # Шесть из них взяты из §9.3 дословно: «Пересдача с «на проверке»»,
    # «Исчерпан лимит попыток», «Превышено число файлов», «Статус без
    # комментария», «Терминальный статус у куратора» и текст ошибки
    # типа файла. Остальные §9.3 не перечисляла до 01.10.2026; тогда же
    # решением Э-6 (вариант A) все девять текстов внесены в §9.3
    # дословно, а число файлов у комментария расширено до пяти
    # (вариант B, §7.10 и FR-46). Держать тексты здесь же — чтобы
    # сообщение на экране и формулировка в ТЗ совпадали слово в слово.
    "submission_comment_required": (
        "Напишите комментарий к работе: он обязателен при сдаче."
    ),
    "comment_body_required": "Напишите текст комментария.",
    "submission_comment_too_long": (
        "Комментарий не должен быть длиннее 4000 символов."
    ),
    "submission_on_check": (
        "Пересдача недоступна: работа на проверке. "
        "Дождитесь комментария куратора."
    ),
    "submission_accepted": (
        "Работа уже принята. Пересдача невозможна."
    ),
    "submission_limit_reached": (
        "Лимит попыток исчерпан ({used} из {limit}). "
        "Дальнейшая сдача недоступна."
    ),
    "files_limit_submission": "Не более 5 файлов на одну работу.",
    "files_limit_comment": "Не более 5 файлов на один комментарий.",
    "status_comment_required": (
        "Укажите комментарий: он обязателен при статусе «на доработку»."
    ),
    "status_terminal": (
        "Эта работа уже принята. Изменение статуса доступно только "
        "администратору."
    ),
    "status_not_on_review": (
        "Статус меняется только у работы на проверке. Сейчас работа в "
        "статусе «{status}»."
    ),
    "status_value_invalid": (
        "Выберите статус: «сдано» или «на доработку»."
    ),
    "review_revert_only_terminal": (
        "Возвращать в доработку можно только принятую работу."
    ),
    "submission_archived": (
        "Задание в архиве: приём работ по нему закрыт."
    ),
    # Текст ниже добавлен на этапе Э-7 (дашборд, FR-58). §9.3 не
    # описывает отказ фильтра дашборда: непустое значение, не относящееся
    # к области куратора, обязано дать пустой результат без утечки того,
    # существует ли объект. Формулировка предложена здесь по образцу
    # Э-2…Э-6 — отдельной правкой ТЗ не оформлялась и вынесена в отчёт
    # этапа.
    "dashboard_filter_invalid": (
        "Фильтр не относится к вашим группам: показан пустой результат."
    ),
    # Текст ниже добавлен на этапе Э-8 (журнал действий, FR-63).
    # §9.3 не описывает отказ фильтра журнала; формулировка предложена
    # по образцу Э-7 (dashboard_filter_invalid) и вынесена в отчёт этапа.
    "audit_filter_invalid": (
        "Некорректное значение фильтра: показан пустой результат."
    ),
    # Тексты ниже добавлены на этапе Э-10 (резервные копии, FR-68…FR-71).
    # Все внесены в §9.3 отдельной правкой ТЗ вместе с текстом
    # предупреждения «Текущие данные будут полностью заменены» — так же,
    # как тексты Э-6. Держать их здесь же — чтобы сообщение на экране и
    # формулировка в ТЗ совпадали слово в слово.
    "backup_no_file": "Выберите файл резервной копии (.json).",
    "backup_invalid": (
        "Файл не является резервной копией: повреждён или имеет "
        "неверную структуру."
    ),
    "backup_version": (
        "Версия схемы в копии ({file}) не совпадает с текущей ({current})."
    ),
    "backup_too_large": (
        "Файл резервной копии превышает допустимые {limit} МБ."
    ),
    "backup_session_expired": (
        "Файл резервной копии устарел. Загрузите его заново."
    ),
    "backup_restored": "Резервная копия восстановлена. Войдите заново.",
    "backup_failed": "Не удалось восстановить копию: данные не изменены.",
}

EMPTY_STATES = {
    "admin_courses": "Курсов пока нет. Создайте первый курс.",
    "assignments": "В курсе пока нет заданий. Добавьте первое.",
    "group_members": (
        "В группе пока нет участников. Добавьте вручную или импортируйте "
        "список из файла."
    ),
    "student_assignment": "Работы ещё не сданы.",
    "audit_log": "Записей, удовлетворяющих фильтру, нет.",
    "course_groups": "В курсе пока нет групп.",
    # Тексты ниже добавлены на этапе Э-7. §9.2 не перечисляет дашборд:
    # в таблице пустых состояний нет ни сводных показателей, ни
    # распределения по статусам. Формулировки предложены здесь по
    # образцу §9.2 — отдельной правкой ТЗ не оформлялись и вынесены в
    # отчёт этапа.
    "dashboard": "Показывать пока нечего: данных по работам нет.",
    "dashboard_filtered": "Нет данных для выбранных фильтров.",
}
