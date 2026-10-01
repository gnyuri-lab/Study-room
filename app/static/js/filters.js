/* Фильтры дашборда (FR-58): форма отправляется сразу при смене
   выпадающего списка. Сервер считает выборку заново, и человек видит
   результат, не нажимая отдельную кнопку.

   Поиск по имени студента (поле type="search") так не отправляется:
   текст применяется по Enter или кнопкой «Применить». Живой ввод на
   каждое нажатие клавиши означал бы перезагрузку страницы и потерю
   фокуса посреди слова, а отправка по уходу из поля (change)
   конфликтовала бы с нажатой рядом кнопкой «Сбросить»: событие
   change наступает раньше клика и отправило бы форму без параметра
   reset. Кнопки же ведут себя как обычно: нажатая кнопка попадает
   в запрос вместе с текущим текстом поля.

   При вводе букв под полем показывается список полных ФИО области
   видимости (замечания заказчика 01.10.2026): совпадения ищутся
   подстрокой без учёта регистра — как и на сервере, — выбор кликом
   или Enter сразу применяет фильтр. Список рисует шаблон, скрипт
   только показывает и прячет подходящие строки: без скрипта поле
   остаётся обычным текстовым поиском.

   Важно: вызывается form.submit(), а не requestSubmit(), — этот метод
   не порождает событие submit, поэтому общий обработчик guardSubmit из
   app.js не срабатывает и не блокирует повторную отправку: каждая смена
   поля — новая навигация, а не вторая попытка той же отправки.
   Без скрипта форма остаётся рабочей: кнопка «Применить» отправляет её
   обычным способом.
*/
(function () {
  "use strict";

  function submitOnChange(event) {
    var field = event.target;
    if (!field || String(field.tagName).toUpperCase() !== "SELECT") {
      return;
    }
    var form = field.form;
    if (!form) {
      return;
    }
    form.submit();
  }

  function initSuggest(form) {
    var wrap = form.querySelector("[data-suggest]");
    if (!wrap) {
      return;
    }
    var input = wrap.querySelector("input");
    var list = wrap.querySelector(".filters-suggest-list");
    if (!input || !list) {
      return;
    }
    var options = Array.prototype.slice.call(
      list.querySelectorAll(".filters-suggest-option")
    );
    var visible = [];
    var active = null;

    function close() {
      list.hidden = true;
      visible = [];
      active = null;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function markActive(next) {
      if (active) {
        active.classList.remove("is-active");
      }
      active = next;
      if (active) {
        active.classList.add("is-active");
        input.setAttribute("aria-activedescendant", active.id);
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function choose(option) {
      if (!option) {
        return;
      }
      input.value = option.getAttribute("data-value");
      close();
      form.submit();
    }

    function update() {
      var query = input.value.trim().toLowerCase();
      visible = [];
      for (var i = 0; i < options.length; i += 1) {
        var option = options[i];
        var name = (option.getAttribute("data-value") || "").toLowerCase();
        var matched = query !== "" && name.indexOf(query) !== -1;
        option.hidden = !matched;
        option.classList.remove("is-active");
        if (matched) {
          visible.push(option);
        }
      }
      if (visible.length === 0) {
        close();
        return;
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      markActive(visible[0]);
    }

    input.addEventListener("input", update);
    input.addEventListener("blur", close);

    // mousedown до click и blur: иначе список закроется раньше выбора.
    list.addEventListener("mousedown", function (event) {
      event.preventDefault();
    });
    list.addEventListener("click", function (event) {
      var target = event.target;
      if (target && target.classList.contains("filters-suggest-option")) {
        choose(target);
      }
    });

    input.addEventListener("keydown", function (event) {
      var key = event.key;
      if (key === "Escape") {
        if (!list.hidden) {
          event.preventDefault();
          close();
        }
        return;
      }
      if (list.hidden) {
        return;
      }
      if (key === "ArrowDown" || key === "ArrowUp") {
        event.preventDefault();
        var index = visible.indexOf(active);
        if (key === "ArrowDown") {
          index = (index + 1) % visible.length;
        } else {
          index = (index - 1 + visible.length) % visible.length;
        }
        markActive(visible[index]);
        return;
      }
      if (key === "Enter") {
        // Список открыт — Enter выбирает подсвеченный пункт, а не
        // отправляет форму с незаконченным текстом.
        event.preventDefault();
        choose(active || visible[0]);
      }
    });
  }

  function init() {
    var forms = document.querySelectorAll("form[data-filters]");
    for (var i = 0; i < forms.length; i += 1) {
      forms[i].addEventListener("change", submitOnChange);
      initSuggest(forms[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
