/* Chart view toggle (candlestick ↔ regular line) for the coin
   detail chart (Page 5) and the homepage balance-area chart
   (Page 3). Both views are rendered server-side from the same
   candles, so the two views can never disagree about the price;
   this only switches which one is visible. The button sits in the
   corner of the chart and shows the OTHER view ("📈 Line" while
   candles are showing, "🕯 Candles" while the line is showing).
   The choice is remembered in sessionStorage, so it survives
   range changes and reloads for the session. No-ops on pages with
   no chart block, and on blocks whose candles are still building
   (no views to switch between). */
(function () {
  'use strict';
  var KEY = 'solentry-chart-view';

  function stored() {
    try { return sessionStorage.getItem(KEY); } catch (e) { return null; }
  }
  function store(view) {
    try { sessionStorage.setItem(KEY, view); } catch (e) { /* private mode */ }
  }

  function apply(block, view) {
    var views = block.querySelectorAll('[data-chart-view]');
    views.forEach(function (el) {
      el.classList.toggle('hidden',
        el.getAttribute('data-chart-view') !== view);
    });
    var btn = block.querySelector('[data-chart-toggle]');
    if (btn) {
      btn.textContent = view === 'candles' ? '📈 Line' : '🕯 Candles';
      btn.setAttribute('aria-label', view === 'candles'
        ? 'Switch to the regular line chart'
        : 'Switch to the candlestick chart');
    }
    block.setAttribute('data-chart-view-now', view);
  }

  function init() {
    var view = stored();
    if (view !== 'candles' && view !== 'line') view = 'candles';
    document.querySelectorAll('[data-chart-block]').forEach(
      function (block) {
        if (!block.querySelector('[data-chart-view="candles"]')
            || !block.querySelector('[data-chart-view="line"]')) return;
        apply(block, view);
        var btn = block.querySelector('[data-chart-toggle]');
        if (btn) btn.addEventListener('click', function () {
          view = block.getAttribute('data-chart-view-now') === 'candles'
            ? 'line' : 'candles';
          store(view);
          apply(block, view);
        });
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else { init(); }
})();
