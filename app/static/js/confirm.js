(function () {
  "use strict";

  // Подтверждение необратимых действий. Разметка: атрибут data-confirm
  // с текстом вопроса на форме, ссылке или кнопке.
  //
  // Согласие и отмена обрабатываются по-разному. При отмене форму нужно
  // разблокировать: защиту от двойной отправки вешает app.js, и к моменту
  // отмены он уже успел заблокировать кнопку. При согласии блокировку
  // снимать нельзя — форма уйдёт прямо сейчас, и повторный клик по
  // отключённой кнопке не создаст второй запрос.

  function unblockForm(form) {
    form.classList.remove("is-loading");
    delete form.dataset.submitting;
    var controls = form.querySelectorAll(
      'button[type="submit"], input[type="submit"]'
    );
    for (var i = 0; i < controls.length; i += 1) {
      controls[i].disabled = false;
      if (controls[i].dataset.idleText) {
        controls[i].textContent = controls[i].dataset.idleText;
        delete controls[i].dataset.idleText;
      }
    }
  }

  function askConfirmed(element) {
    var question = element.dataset.confirm;
    if (!question) {
      return true;
    }
    return window.confirm(question);
  }

  function guardSubmit(event) {
    if (!askConfirmed(this)) {
      event.preventDefault();
      unblockForm(this);
    }
  }

  function guardClick(event) {
    if (!askConfirmed(this)) {
      event.preventDefault();
    }
  }

  function init() {
    var confirmables = document.querySelectorAll("[data-confirm]");
    for (var i = 0; i < confirmables.length; i += 1) {
      var element = confirmables[i];
      var tag = element.tagName;
      if (tag === "FORM") {
        element.addEventListener("submit", guardSubmit);
      } else if (tag === "A") {
        element.addEventListener("click", guardClick);
      } else {
        element.addEventListener("click", guardClick);
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
