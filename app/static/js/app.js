(function () {
  "use strict";

  // Блокировка формы откладывается на следующий такт. Нажатая кнопка
  // обязана остаться активной до самого конца отправки: браузер собирает
  // данные формы после события submit и исключает отключённые элементы,
  // поэтому немедленное disabled=true выбросило бы из запроса имя и
  // значение кнопки (например, reset=course в фильтрах дашборда —
  // FR-58, AC-41). Повторная отправка и так запрещена через
  // dataset.submitting, а при отменённом confirm.js подтверждении
  // класс и блокировка просто не применяются.
  function markLoading(form) {
    window.setTimeout(function () {
      if (form.dataset.submitting !== "1") {
        return;
      }
      form.classList.add("is-loading");
      var submit = form.querySelector('button[type="submit"], input[type="submit"]');
      if (!submit) {
        return;
      }
      submit.disabled = true;
      var pending = submit.dataset.loadingText;
      if (pending) {
        submit.dataset.idleText = submit.textContent;
        submit.textContent = pending;
      }
    }, 0);
  }

  function guardSubmit(event) {
    if (this.dataset.submitting === "1") {
      event.preventDefault();
      return;
    }
    this.dataset.submitting = "1";
    markLoading(this);
  }

  function init() {
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i += 1) {
      forms[i].addEventListener("submit", guardSubmit);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
