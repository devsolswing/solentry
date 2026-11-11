/* Live price ticks on the coin detail page: polls the tick endpoint
   once a second while the page is visible, updates the big price,
   appends the tick to the live chart (last ~120 ticks, 2 minutes),
   and shows when the price was actually fetched. Pauses while the
   tab is hidden so the feed is not hammered for a page nobody is
   looking at. Prices are never invented: on a failed tick the last
   real price stays on screen with its time, and the note says the
   feed is unavailable. The "last updated" time comes from the
   tick's own as_of — for a cached feed price that is when the
   price was fetched, not when this tick arrived.

   When the app WebSocket is up (live.js sets
   window.__solentryLiveUp), the socket owns the ticks: this poller
   stands down and live.js feeds ticks into the same applyTick
   below, so the price, chart and note update from the socket
   instead. When the socket drops, the flag clears and this poller
   resumes on its next second — the REST fallback, with no gap in
   the chart. */
(function () {
  'use strict';
  var chart = document.getElementById('live-chart');
  if (!chart) return;
  var url = chart.dataset.tickUrl;
  if (!url) return;
  var priceEl = document.getElementById('live-price');
  var updatedEl = document.getElementById('live-updated');
  var line = document.getElementById('live-chart-line');
  var note = document.getElementById('live-chart-note');
  var points = [];  /* last prices, one per second */
  var MAX_POINTS = 120;
  var lastOkAt = null;
  var timer = null;

  function fmtTime(d) {
    return d.toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }

  function draw() {
    if (!line || points.length === 0) return;
    var min = Math.min.apply(null, points);
    var max = Math.max.apply(null, points);
    var span = max - min;
    if (span === 0) span = Math.abs(max) * 0.001 || 1;
    var pts = points.map(function (p, i) {
      var x = points.length === 1 ? 50 : (i / (MAX_POINTS - 1)) * 100;
      var y = 38 - ((p - min) / span) * 36;
      return x.toFixed(2) + ',' + y.toFixed(2);
    });
    line.setAttribute('points', pts.join(' '));
  }

  /* One validated tick, from the REST poll or the live WebSocket:
     update the price, the "last updated" time, the chart and the
     note. asOf is a Date (the tick's own time); source names the
     feed the tick came from. */
  function applyTick(price, asOf, source) {
    if (typeof price !== 'number' || isNaN(price)) return;
    if (!(asOf instanceof Date) || isNaN(asOf.getTime())) {
      asOf = new Date();
    }
    lastOkAt = asOf;
    if (priceEl) priceEl.textContent = '$' + price.toFixed(4);
    if (updatedEl) updatedEl.textContent = fmtTime(asOf);
    points.push(price);
    if (points.length > MAX_POINTS) points.shift();
    draw();
    if (note) {
      note.textContent = 'Live · ' + points.length + ' tick'
        + (points.length === 1 ? '' : 's')
        + ' · source: ' + (source || 'feed');
    }
  }

  /* The live feed (live.js) calls this for every socket tick. */
  window.SolEntryTick = { apply: applyTick };

  function tick() {
    if (window.__solentryLiveUp) return;
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
