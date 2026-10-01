(function () {
  "use strict";

  // Иконки рисуются здесь же: внешних шрифтов и CDN в проекте нет (NFR-03).
  var SHOWN =
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"' +
    ' focusable="false" fill="none" stroke="currentColor" stroke-width="2"' +
    ' stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/>' +
    '<circle cx="12" cy="12" r="3"/></svg>';

  var HIDDEN =
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"' +
    ' focusable="false" fill="none" stroke="currentColor" stroke-width="2"' +
    ' stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8"' +
    ' a18.45 18.45 0 0 1 5.06-5.94"/>' +
    '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8"' +
    ' a18.5 18.5 0 0 1-2.16 3.19"/>' +
    '<path d="M14.12 14.12A3 3 0 1 1 9.88 9.88"/>' +
    '<line x1="1" y1="1" x2="23" y2="23"/></svg>';

  function wrap(input) {
    if (input.dataset.passwordToggle) {
      return;
    }
    input.dataset.passwordToggle = "1";

    var shell = document.createElement("span");
    shell.className = "password-wrap";
    input.parentNode.insertBefore(shell, input);
    shell.appendChild(input);

    var button = document.createElement("button");
    button.type = "button"; // не отправляет форму
    button.className = "password-toggle";
    shell.appendChild(button);

    function paint() {
      var hidden = input.type === "password";
      button.innerHTML = hidden ? SHOWN : HIDDEN;
      var label = hidden ? "Показать пароль" : "Скрыть пароль";
      button.setAttribute("aria-label", label);
      button.setAttribute("aria-pressed", hidden ? "false" : "true");
      button.title = label;
    }

    button.addEventListener("click", function () {
      var caret = input.selectionStart;
      input.type = input.type === "password" ? "text" : "password";
      paint();
      input.focus();
      if (caret !== null && input.type === "text") {
        try {
          input.setSelectionRange(caret, caret);
        } catch (error) {
          /* Safari на type=file такого не умеет — просто остаёмся в начале */
        }
      }
    });

    paint();
  }

  function init() {
    var inputs = document.querySelectorAll('input[type="password"]');
    for (var i = 0; i < inputs.length; i += 1) {
      wrap(inputs[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
