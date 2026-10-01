/* Список выбранных файлов и подсказка о превышении лимита.
 *
 * Это только подсказка до отправки формы: превышение числа файлов и
 * размера проверяет сервер (FR-52), и его текст берётся из §9.3.
 * Клиентская проверка нужна для того, чтобы администратор узнал о
 * проблеме сразу, а не после ожидания загрузки десяти мегабайт.
 *
 * Файл приходит как FileList, у которого нет ни forEach, ни length в
 * старых сборках, поэтому перебираем через FileList.item и никогда не
 * полагаемся на поддержку того или иного метода. Внешних библиотек
 * нет: всё в пределах одного файла и без обращений к сети (NFR-03).
 */
(function () {
  "use strict";

  function humanSize(bytes) {
    if (bytes < 1024) {
      return bytes + " Б";
    }
    if (bytes < 1048576) {
      return Math.round(bytes / 1024) + " КБ";
    }
    return (Math.round((bytes / 1048576) * 10) / 10) + " МБ";
  }

  function toArray(list) {
    var out = [];
    if (!list) {
      return out;
    }
    for (var i = 0; i < list.length; i += 1) {
      out.push(list.item(i));
    }
    return out;
  }

  function render(input) {
    var summary = document.querySelector(
      '[data-file-summary][data-for="' + input.id + '"]'
    );
    if (!summary) {
      return;
    }
    var files = toArray(input.files);
    if (files.length === 0) {
      summary.hidden = true;
      summary.textContent = "";
      return;
    }

    var maxFiles = parseInt(input.getAttribute("data-max-files"), 10) || 5;
    var maxBytes = parseInt(input.getAttribute("data-max-bytes"), 10) || 10485760;
    var names = [];
    var oversize = [];

    for (var i = 0; i < files.length; i += 1) {
      names.push(files[i].name + " (" + humanSize(files[i].size) + ")");
      if (files[i].size > maxBytes) {
        oversize.push(files[i].name);
      }
    }

    var text = "Выбрано файлов: " + files.length + " из " + maxFiles + ".";
    if (files.length > maxFiles) {
      text += " Превышен лимит — лишние файлы не будут сохранены.";
    }
    if (oversize.length > 0) {
      text += " Больше 10 МБ: " + oversize.join(", ") + " — такие файлы не сохраняются.";
    }

    summary.hidden = false;
    summary.textContent = text;
    // Имя перечислено в подписи для голосового доступа, но десяток
    // длинных имён на узком экране переносит весь блок.
    summary.title = names.join("\n");
  }

  function init() {
    var inputs = document.querySelectorAll('input[type="file"]');
    for (var i = 0; i < inputs.length; i += 1) {
      var input = inputs[i];
      var summary = input.parentNode.querySelector("[data-file-summary]");
      if (summary) {
        summary.setAttribute("data-for", input.id);
      }
      input.addEventListener("change", function (event) {
        render(event.target);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
