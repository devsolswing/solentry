/* Live price ticks on the coin detail page: polls the tick endpoint
   once a second while the page is visible, updates the big price
   and shows when the price was actually fetched. Pauses while the
   tab is hidden so the feed is not hammered for a page nobody is
   looking at. Prices are never invented: on a failed tick the last
   real price stays on screen with its time, and the note says the
   feed is unavailable. The "last updated" time comes from the
   tick's own as_of — for a cached feed price that is when the
   price was fetched, not when this tick arrived.

   The CHART does not redraw every second. The detail chart is a
   real candlestick chart, rendered server-side; live ticks only
   re-render it at most once per candle interval (a new fetch of
   the candles endpoint, which re-buckets the current candle from
   the latest data). Between renders the chart stands as-is and
   the caption says when it last refreshed. The price number above
   it still updates every second — that is the price, not the
   chart.

   When the app WebSocket is up (live.js sets
   window.__solentryLiveUp), the socket owns the ticks: this poller
   stands down while socket ticks are actually arriving, and live.js
   feeds ticks into the same applyTick below, so the price and note
   update from the socket instead. When the socket drops — or
   connects but never delivers a tick for 5 seconds — the flag
   clears (or the stall is detected) and this poller resumes on its
   next second — the REST fallback, with no gap in the price. */
(function () {
  'use strict';
  var chart = document.getElementById('live-chart');
  if (!chart) return;
  var url = chart.dataset.tickUrl;
  if (!url) return;
  var priceEl = document.getElementById('live-price');
  var updatedEl = document.getElementById('live-updated');
  var note = document.getElementById('live-chart-note');
  var detailChart = document.getElementById('detail-chart');
  var lastOkAt = null;
  var timer = null;
  /* The server-rendered chart is fresh on page load, so the first
     live re-render waits a full candle interval. */
  var lastChartRefresh = Date.now();

  function fmtTime(d) {
    return d.toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }

  /* Re-render the candlestick chart at most once per candle
     interval. Fetches fresh candle SVGs from the server (same data
     as the page load) and swaps them in; the toggle state is kept
     because the swap only replaces the view contents, not the
     buttons. A failed fetch leaves the last chart standing. */
  function maybeRefreshChart() {
    if (!detailChart) return;
    var interval = parseInt(
      detailChart.dataset.chartInterval || '0', 10);
    if (!interval || interval <= 0) return;
    var candlesUrl = detailChart.dataset.candlesUrl;
    if (!candlesUrl) return;
    var now = Date.now();
    if (now - lastChartRefresh < interval * 1000) return;
    lastChartRefresh = now;
    fetch(candlesUrl, { credentials: 'same-origin' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.ok) return;
        var candlesEl = detailChart.querySelector(
          '[data-chart-view="candles"]');
        var lineEl = detailChart.querySelector(
          '[data-chart-view="line"]');
        if (candlesEl && data.candles_svg) {
          candlesEl.innerHTML = data.candles_svg;
        }
        if (lineEl && data.line_svg) {
          lineEl.innerHTML = data.line_svg;
        }
        var chartUpdated = document.getElementById('chart-updated');
        if (chartUpdated && data.updated) {
          var d = new Date(data.updated);
          chartUpdated.textContent = isNaN(d.getTime())
            ? data.updated : fmtTime(d);
        }
      })
      .catch(function () { /* next tick retries; chart stands */ });
  }

  /* One validated tick, from the REST poll or the live WebSocket:
     update the price, the "last updated" time and the note, then
     let the chart re-render if a candle interval has passed.
     asOf is a Date (the tick's own time); source names the feed
     the tick came from. */
  function applyTick(price, asOf, source) {
    if (typeof price !== 'number' || isNaN(price)) return;
    if (!(asOf instanceof Date) || isNaN(asOf.getTime())) {
      asOf = new Date();
    }
    lastOkAt = asOf;
    if (priceEl) priceEl.textContent = '$' + price.toFixed(4);
    if (updatedEl) updatedEl.textContent = fmtTime(asOf);
    if (note) {
      note.textContent = 'Live · source: ' + (source || 'feed');
    }
    maybeRefreshChart();
  }

  /* The live feed (live.js) calls this for every socket tick. */
  var lastWsTickAt = null;
  window.SolEntryTick = { apply: function (price, asOf, source) {
    lastWsTickAt = Date.now();
    applyTick(price, asOf, source);
  } };

  function tick() {
    /* Stand down only while socket ticks are actually arriving: a
       socket that connects but never delivers must not silence this
       poller — the price box would sit on "Waiting for the first
       live tick…" forever while a working REST tick exists. */
    if (window.__solentryLiveUp && lastWsTickAt
        && Date.now() - lastWsTickAt < 5000) return;
    fetch(url, { credentials: 'same-origin' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.ok || typeof data.price !== 'number') {
          if (note) {
            note.textContent = (data && data.error
              ? data.error + ' ' : 'Price unavailable right now. ')
              + (lastOkAt
                ? 'Showing the last price from ' + fmtTime(lastOkAt)
                  + '.' : 'No live price yet.');
          }
          return;
        }
        var asOf = data.as_of ? new Date(data.as_of) : new Date();
        if (isNaN(asOf.getTime())) asOf = new Date();
        applyTick(data.price, asOf, data.source);
      })
      .catch(function () {
        if (note) {
          note.textContent = 'Feed unavailable — showing the last price'
            + (lastOkAt ? ' from ' + fmtTime(lastOkAt) : '') + '.';
        }
      });
  }

  function start() {
    if (!timer) { timer = setInterval(tick, 1000); tick(); }
  }
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop(); else start();
  });
  start();
})();
