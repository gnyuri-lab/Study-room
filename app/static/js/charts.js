/* Связка диаграммы и таблицы распределения (FR-57, чек-лист §11:
   «Диаграммы по статусам отражают реальные числа»).

   Подписи и полосы лежат внутри SVG, а строки таблицы — рядом, поэтому
   CSS-селектор до них не дотянется: подсветка делается делегированием
   события на самой картинке. Цвета берутся из тех же классов
   status-<код>, что и у бейджей, — меняется только контейнер строки.
*/
(function () {
  "use strict";

  var marked = [];

  function clearHighlight() {
    for (var i = 0; i < marked.length; i += 1) {
      marked[i].classList.remove("is-highlighted");
    }
    marked = [];
  }

  function highlight(status) {
    clearHighlight();
    if (!status) {
      return;
    }
    var rows = document.querySelectorAll(
      '.status-table tr[data-status="' + status + '"]'
    );
    for (var i = 0; i < rows.length; i += 1) {
      rows[i].classList.add("is-highlighted");
      marked.push(rows[i]);
    }
  }

  function barOf(node) {
    while (node && node !== document) {
      if (
        node.nodeType === 1 &&
        node.classList.contains("chart-bar") &&
        node.getAttribute("data-status")
      ) {
        return node;
      }
      node = node.parentNode;
    }
    return null;
  }

  function init() {
    var chart = document.querySelector("svg.chart");
    if (!chart) {
      return;
    }
    chart.addEventListener("mouseover", function (event) {
      var bar = barOf(event.target);
      highlight(bar ? bar.getAttribute("data-status") : null);
    });
    // mouseleave не всплывает и не срабатывает при переходе с полосы
    // на подпись внутри той же картинки — подсветка держится, пока
    // курсор остаётся на диаграмме.
    chart.addEventListener("mouseleave", clearHighlight);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
