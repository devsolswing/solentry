/* Live prices over the app's own WebSocket (/ws/live), with a REST
   fallback. Pages mark the numbers they want kept live with data
   attributes — live.js subscribes to exactly those coins, then
   updates prices, holding values, P&L and 1h/24h % as validated
   ticks arrive. Every tick carries its source and time: the status
   pill in the topbar shows both, and each updated node gets them
   as a tooltip. A tick the server rejected never arrives here.

   If the socket drops, the page says so plainly (status pill) and
   falls back to polling /api/live every 10 seconds, while the
   socket reconnects with backoff. The coin detail page's own
   1-second REST poller (coin_tick.js) stands down while the socket
   is up and resumes when it drops, so the live chart never gaps.
   A stale tick (older than 30s at the server) is shown dimmed,
   never as fresh. Pages with no live nodes do nothing at all. */
(function () {
  'use strict';

  function collect(attr) {
    var nodes = document.querySelectorAll('[' + attr + ']');
    var out = [];
    for (var i = 0; i < nodes.length; i++) out.push(nodes[i]);
    return out;
  }

  var priceNodes = collect('[data-live-price]');
  var valueNodes = collect('[data-live-value]');
  var pnlNodes = collect('[data-live-pnl]');
  var ch1hNodes = collect('[data-live-change-1h]');
  var ch24hNodes = collect('[data-live-change-24h]');
  var totalValueEls = collect('[data-live-total-value]');
  var totalPnlEls = collect('[data-live-total-pnl]');
  var portfolioEls = collect('[data-live-total-portfolio]');

  /* Holdings 1hr sparklines (home page): each coin row's small
     chart refreshes every 60 seconds from /api/sparklines, so the
     1hr chart keeps moving with the live feed instead of sitting
     static from page load. A coin with fewer than 2 real points
     in the last hour returns null -- its sparkline is left alone
     (chart building), never faked. The poll stops when the page
     is hidden or closed, so it never runs in the background. */
  var sparkEls = collect('[data-sparkline-coin]');
  var SPARK_POLL_MS = 60000;
  var sparkTimer = null;
  function fetchSparklines() {
    if (!sparkEls.length) return;
    var ids = [];
    sparkEls.forEach(function (el) {
      ids.push(el.getAttribute('data-sparkline-coin'));
    });
    fetch('/api/sparklines?coins=' + encodeURIComponent(ids.join(',')),
      { credentials: 'same-origin' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.ok || !data.sparklines) return;
        sparkEls.forEach(function (el) {
          var svg = data.sparklines[el.getAttribute('data-sparkline-coin')];
          if (typeof svg === 'string' && svg) el.innerHTML = svg;
        });
      })
      .catch(function () { /* next poll retries; last chart stands */ });
  }
  function startSparkPoll() {
    if (sparkTimer) return;
    fetchSparklines();
    sparkTimer = setInterval(fetchSparklines, SPARK_POLL_MS);
  }
  function stopSparkPoll() {
    if (sparkTimer) { clearInterval(sparkTimer); sparkTimer = null; }
  }
  if (sparkEls.length) {
    startSparkPoll();
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stopSparkPoll();
      else startSparkPoll();
    });
    window.addEventListener('pagehide', stopSparkPoll);
  }
  var chart = document.getElementById('live-chart');
  var statusEl = document.getElementById('live-status');

  var coins = {};
  function addCoin(id) { if (id) coins[id] = true; }
  function attrOf(node, attr) { return node.getAttribute(attr); }
  priceNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-price')); });
  valueNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-value')); });
  pnlNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-pnl')); });
  ch1hNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-change-1h')); });
  ch24hNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-change-24h')); });
  if (chart && chart.dataset.coinId) addCoin(chart.dataset.coinId);
  var coinList = Object.keys(coins);
  if (coinList.length === 0) return;  /* nothing live on this page */

  if (statusEl) statusEl.hidden = false;

  var ws = null;
  var retryMs = 5000;
  var fallbackTimer = null;
  var lastTickAt = null;
  var lastSource = null;

  function fmtTime(d) {
    return d.toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }
  function fmtPrice(p) { return '$' + p.toFixed(4); }
  function fmtMoney(v) {
    return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US',
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtSignedMoney(v) {
    return (v >= 0 ? '+' : '-') + '$' + Math.abs(v).toLocaleString(
      'en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtPct(v, decimals) {
    var d = (decimals === undefined || decimals === null)
      ? 1 : parseInt(decimals, 10);
    if (isNaN(d)) d = 1;
    return (v >= 0 ? '+' : '') + v.toFixed(d) + '%';
  }
  function setFresh(node, stale) {
    if (stale) node.classList.add('live-stale');
    else node.classList.remove('live-stale');
  }
  function stamp(node, tick) {
    node.title = (tick.source || 'feed') + ' · '
      + (tick.as_of || 'just now')
      + (tick.stale ? ' · stale' : '');
  }
  /* The visible text of a live node: its first child span when it has
     one (a "—" or "price unavailable" placeholder), else the node
     itself. Updating the target leaves sibling labels (e.g. the
     wallet's "1h") untouched. */
  function liveTarget(node) {
    return node.querySelector('span') || node;
  }
  /* Write one live value: replace the placeholder text, drop the
     muted placeholder class, set the up/down colour when the value
     has a sign, and stamp source + freshness. */
  function setLive(node, text, tick, signed) {
    var target = liveTarget(node);
    target.textContent = text;
    target.classList.remove('muted');
    if (signed !== undefined && signed !== null) {
      target.classList.remove('up', 'down');
      target.classList.add(signed >= 0 ? 'up' : 'down');
    }
    setFresh(target, !!tick.stale);
    stamp(target, tick);
  }

  function setStatus(state, when) {
    if (!statusEl) return;
    if (state === 'live') {
      statusEl.textContent = '● Live · ' + fmtTime(when || new Date())
        + (lastSource ? ' · ' + lastSource : '');
      statusEl.classList.remove('live-down');
      statusEl.classList.add('live-up');
    } else if (state === 'connecting') {
      statusEl.textContent = '↻ Connecting to live feed…';
      statusEl.classList.remove('live-up');
      statusEl.classList.add('live-down');
    } else {
      statusEl.textContent = '↻ Live feed down — last prices, refreshing';
      statusEl.classList.remove('live-up');
      statusEl.classList.add('live-down');
    }
  }

  function recomputeTotals() {
    function sum(nodes, get) {
      var total = 0;
      var any = false;
      nodes.forEach(function (n) {
        var v = get(n);
        if (typeof v === 'number' && !isNaN(v)) { total += v; any = true; }
      });
      return any ? total : null;
    }
    var holdings = sum(valueNodes, function (n) {
      var v = parseFloat(n.dataset.liveComputed);
      return isNaN(v) ? null : v;
    });
    var pnl = sum(pnlNodes, function (n) {
      var v = parseFloat(n.dataset.liveComputed);
      return isNaN(v) ? null : v;
    });
    totalValueEls.forEach(function (el) {
      if (holdings !== null) {
        el.textContent = fmtMoney(holdings);
      }
    });
    totalPnlEls.forEach(function (el) {
      if (pnl !== null) el.textContent = fmtSignedMoney(pnl);
    });
    portfolioEls.forEach(function (el) {
      var cash = parseFloat(el.getAttribute('data-live-cash'));
      if (holdings !== null && !isNaN(cash)) {
        el.textContent = fmtMoney(cash + holdings) + ' CAD';
      }
    });
  }

  function applyTick(t) {
    if (!t || !t.coin_id || typeof t.price !== 'number') return;
    var id = t.coin_id;
    var asOf = t.as_of ? new Date(t.as_of) : new Date();
    if (isNaN(asOf.getTime())) asOf = new Date();
    lastTickAt = asOf;
    lastSource = t.source || 'feed';

    priceNodes.forEach(function (n) {
      if (attrOf(n, 'data-live-price') !== id) return;
      setLive(n, fmtPrice(t.price), t);
    });
    valueNodes.forEach(function (n) {
      if (attrOf(n, 'data-live-value') !== id) return;
      var amount = parseFloat(n.getAttribute('data-live-amount'));
      if (isNaN(amount)) return;
      var value = amount * t.price;
      n.dataset.liveComputed = String(value);
      setLive(n, fmtMoney(value), t);
    });
    pnlNodes.forEach(function (n) {
      if (attrOf(n, 'data-live-pnl') !== id) return;
      var amount = parseFloat(n.getAttribute('data-live-amount'));
      var avg = parseFloat(n.getAttribute('data-live-avg'));
      if (isNaN(amount) || isNaN(avg)) return;
      var pnl = (t.price - avg) * amount;
      n.dataset.liveComputed = String(pnl);
      setLive(n, fmtSignedMoney(pnl), t, pnl);
    });
    function applyChange(nodes, attr, field) {
      if (typeof t[field] !== 'number') return;
      nodes.forEach(function (n) {
        if (attrOf(n, attr) !== id) return;
        setLive(n, fmtPct(t[field],
          n.getAttribute('data-live-decimals')), t, t[field]);
      });
    }
    applyChange(ch1hNodes, 'data-live-change-1h', 'change_1h');
    applyChange(ch24hNodes, 'data-live-change-24h', 'change_24h');
    if (chart && chart.dataset.coinId === id
        && window.SolEntryTick) {
      window.SolEntryTick.apply(t.price, asOf, t.source);
    }
    recomputeTotals();
    setStatus('live', asOf);
  }

  function fetchSnapshot() {
    fetch('/api/live?coins=' + encodeURIComponent(coinList.join(',')),
      { credentials: 'same-origin' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.ok || !data.ticks) return;
        Object.keys(data.ticks).forEach(function (k) {
          applyTick(data.ticks[k]);
        });
      })
      .catch(function () { /* next poll retries; last prices stand */ });
  }

  function startFallback() {
    if (fallbackTimer) return;
    fetchSnapshot();
    fallbackTimer = setInterval(fetchSnapshot, 10000);
  }
  function stopFallback() {
    if (fallbackTimer) {
      clearInterval(fallbackTimer);
      fallbackTimer = null;
    }
  }

  function connect() {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    try {
      ws = new WebSocket(proto + location.host + '/ws/live');
    } catch (e) {
      onDown();
      return;
    }
    ws.onopen = function () {
      window.__solentryLiveUp = true;
      retryMs = 5000;
      stopFallback();
      ws.send(JSON.stringify({ subscribe: coinList }));
      setStatus('live', new Date());
    };
    ws.onmessage = function (ev) {
      var data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      if (!data) return;
      if (data.type === 'snapshot' && data.ticks) {
        Object.keys(data.ticks).forEach(function (k) {
          applyTick(data.ticks[k]);
        });
      } else if (data.type === 'tick') {
        applyTick(data);
      }
    };
    ws.onclose = function () { onDown(); };
    ws.onerror = function () { /* onclose follows */ };
  }

  function onDown() {
    window.__solentryLiveUp = false;
    ws = null;
    setStatus('down');
    startFallback();
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 30000);
  }

  setStatus('connecting');
  connect();
})();
