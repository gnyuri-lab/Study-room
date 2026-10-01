"""SVG-диаграмма распределения работ по пяти статусам (FR-55, FR-57).

Диаграмма рисуется кодом приложения: внешних библиотек, шрифтов,
картинок и сервисов нет — требование закрыто одновременно как FR-57,
так и NFR-03/NFR-04 (О-05: «без внешних AI и API»). Проверяется это
отсутствием строк ``http`` и ``xmlns`` в разметке: ``xmlns`` здесь
намеренно нет — XHTML-имена пространств нужны только если страница
подгружается как XML, а в HTML5 он не требуется.

Пять решений, которые модуль фиксирует намеренно.

**Подпись статуса лежит над полосой, а не слева от неё.** Классическая
компоновка «метка слева, полоса справа» на ширине 360 px либо сжимает
полосу до нечитаемых размеров, либо обрезает длинную подпись («Сдано
после дедлайна»). Размещение подписи на своей строке даёт ей всю ширину
картины независимо от ширины экрана.

**Цвета задаются CSS-классами, а не атрибутом ``fill``.** Класс
``chart-bar--<код>`` связан с цветом статуса §9.4 теми же переменными,
что и бейдж ``status-<код>``: диаграмма и таблица рядом обязаны
совпадать по цвету, а мест смены одного — одно CSS-правило.

**Шрифт в SVG задаётся в единицах viewBox и поднимается медиазапросом.**
SVG целиком масштабируется вместе с картиной, поэтому 13 px превратились
бы на 360 px примерно в 6 px. Правило ``@media (max-width: 600px)``
увеличивает кегль в единицах картинки, и после масштабирования текст
остаётся около 11 px — без горизонтальной прокрутки (NFR-01).

**Пустое распределение — пустая строка, а не картинка с нулями.**
Ноль работ не является диаграммой: показывать пять нулевых полос
значит выдавать нулевую информацию в рамке. Пустое состояние экрана
описывает ``EMPTY_STATES``, а не графика.

**Числа есть и в картинке, и в таблице рядом.** Картинка даёт форму,
таблица — точные значения; обе части считаются из одного словаря
``counts``, поэтому разойтись между собой не могут (чек-лист §11:
«Диаграммы по статусам отражают реальные числа»).
"""

from __future__ import annotations

from markupsafe import Markup, escape

from app import config

#: Ширина картины в единицах viewBox. Ограничена сверху в CSS
#: (``max-width: 100%`` уже есть у всех ``svg``), поэтому на десктопе
#: карта не растягивается шире этой величины.
WIDTH = 680

#: Высота одной строки: подпись + полоса + отступ.
ROW_HEIGHT = 58

#: Отступ сверху и высота полосы.
TOP = 10
BAR_HEIGHT = 20

#: Максимальная длина полосы: до неё масштабируется максимальный
#: счётчик, чтобы самая большая категория занимала всю ширину.
BAR_MAX = 560

#: Пять строк §7.9 всегда: диаграмма показывает распределение по всем
#: статусам, даже когда какой-то из них не встречается.
HEIGHT = TOP * 2 + ROW_HEIGHT * len(config.STATUS_ORDER)


def status_distribution_svg(counts: dict[str, int]) -> Markup:
    """Карта распределения ``counts`` по пяти статусам либо пустая строка.

    ``counts`` — словарь ``код статуса → число``; отсутствующий ключ
    читается как ноль. Картинка всегда содержит все пять строк в порядке
    ``STATUS_ORDER``, чтобы набор категорий не зависел от данных.
    """
    values = {code: int(counts.get(code, 0)) for code in config.STATUS_ORDER}
    total = sum(values.values())
    if total <= 0:
        return Markup("")
    peak = max(values.values()) or 1

    parts = [
        '<svg class="chart" viewBox="0 0 {width} {height}" role="img"'
        ' aria-label="{aria}">'.format(
            width=WIDTH,
            height=HEIGHT,
            aria=escape(_aria_label(values)),
        )
    ]
    for index, code in enumerate(config.STATUS_ORDER):
        parts.append(_row(code, values[code], TOP + index * ROW_HEIGHT, peak))
    parts.append("</svg>")
    return Markup("".join(parts))


def _aria_label(values: dict[str, int]) -> str:
    """Полное описание картины для скринридера и тестов."""
    return "Распределение работ по статусам: {items}.".format(
        items=", ".join(
            "{0} — {1}".format(config.STATUSES[code]["label"], values[code])
            for code in config.STATUS_ORDER
        )
    )


def _row(code: str, value: int, top: int, peak: int) -> str:
    """Одна строка: подпись, полоса с всплывающим числом и число рядом."""
    label = config.STATUSES[code]["label"]
    width = int(round(BAR_MAX * value / peak))
    label_y = top + 18
    bar_y = top + 28
    value_y = bar_y + BAR_HEIGHT - 5
    return (
        # Подпись над полосой: вся ширина картины доступна тексту на
        # любом экране (см. строку модуля).
        '<text class="chart-label" x="0" y="{label_y}">{label}</text>'
        # data-status нужен charts.js для подсветки строки таблицы.
        '<rect class="chart-bar chart-bar--{code}" data-status="{code}"'
        ' x="0" y="{bar_y}" width="{width}" height="{bar_height}" rx="3">'
        "<title>{label}: {value}</title></rect>"
        '<text class="chart-value" x="{text_x}" y="{value_y}">{value}</text>'
    ).format(
        label_y=label_y,
        label=escape(label),
        code=code,
        bar_y=bar_y,
        width=width,
        bar_height=BAR_HEIGHT,
        value=escape(str(value)),
        text_x=width + 10,
        value_y=value_y,
    )
