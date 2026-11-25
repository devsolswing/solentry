/* Live prices over the app's own WebSocket (/ws/live), with a REST
   fallback. Pages mark the numbers they want kept live with data
   attributes — live.js subscribes to exactly those coins, then
   updates prices, holding values (USD and SOL), P&L and 1h/24h %
   as validated ticks arrive. Every tick carries its source and
   time: the status pill in the topbar shows both, and each updated
   node gets them as a tooltip. A tick the server rejected never
   arrives here. The home page also re-fetches its holdings list
   every 60 seconds (/api/home-holdings), so a coin leaves the
   list the moment its balance hits 0 without a manual reload.
   If the socket drops, the page says so plainly (status pill) and
   falls back to polling /api/live every 4 seconds, while the
   socket reconnects with backoff. A socket that connects but never
   delivers a message within 10s counts as down too — the status
   pill only says "Live" after the first tick arrives, never on
   connect alone. The coin detail page's own
   1-second REST poller (coin_tick.js) stands down while the socket
   is up and resumes when it drops, so the live chart never gaps.
   A stale tick (older than 30s at the server) renders the node
   text "no data" — never the dimmed old number — and a subscribed
   coin absent from a snapshot renders "no data" the same way.
   Pages with no live nodes do nothing at all. */
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
  var valueSolNodes = collect('[data-live-value-sol]');
  var totalValueEls = collect('[data-live-total-value]');
  var totalPnlEls = collect('[data-live-total-pnl]');
  var portfolioEls = collect('[data-live-total-portfolio]');
  /* Last validated price per coin, for the SOL-value ticking
     below (a holding's SOL value needs its coin price AND the
     SOL price from the same feed). */
  var lastPrices = {};

  /* Holdings 1hr sparklines (home page): each coin row's small
     chart refreshes every 5 seconds from /api/sparklines, so the
     1hr chart keeps moving with the live feed instead of sitting
     static from page load. Held coins refresh on the 5-second
     cadence; other data pages poll every 4 seconds. A coin with
     fewer than 2 real points in the last hour returns null -- its
     sparkline is left alone (chart building), never faked. The
     poll stops when the page is hidden or closed, so it never runs
     in the background. */
  var sparkEls = collect('[data-sparkline-coin]');
  var SPARK_POLL_MS = 5000;
  var sparkTimer = null;
  function fetchSparklines() {
    /* Re-collect every poll: the holdings refresh can swap rows
       in or out, and the poll must follow the rows actually on
       the page, not the ones from page load. */
    var els = collect('[data-sparkline-coin]');
    if (!els.length) return;
    var ids = [];
    els.forEach(function (el) {
      ids.push(el.getAttribute('data-sparkline-coin'));
    });
    fetch('/api/sparklines?coins=' + encodeURIComponent(ids.join(',')),
      { credentials: 'same-origin' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.ok || !data.sparklines) return;
        els.forEach(function (el) {
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

  /* Holdings list auto-refresh (home page): every 60 seconds the
     page re-fetches /api/home-holdings and updates the rows in
     place (SOL value, 1h %, sparkline) when the coin set is
     unchanged. A coin that left the server's rows (its balance
     hit 0) is removed from the DOM, and a new coin's row is
     inserted — so the list always shows only coins actually
     owned, without a manual reload. Any insert / removal
     re-collects live.js's node lists and re-subscribes the
     socket, so the new rows tick live too. The poll runs while
     the page is visible and stops when hidden, like the
     sparkline poll. */
  var holdingsList = document.querySelector('[data-home-holdings-list]');
  var HOLDINGS_POLL_MS = 5000;
  var holdingsTimer = null;
  function refreshLiveCollections() {
    priceNodes = collect('[data-live-price]');
    valueNodes = collect('[data-live-value]');
    pnlNodes = collect('[data-live-pnl]');
    ch1hNodes = collect('[data-live-change-1h]');
    ch24hNodes = collect('[data-live-change-24h]');
    valueSolNodes = collect('[data-live-value-sol]');
    totalValueEls = collect('[data-live-total-value]');
    totalPnlEls = collect('[data-live-total-pnl]');
    portfolioEls = collect('[data-live-total-portfolio]');
    sparkEls = collect('[data-sparkline-coin]');
    coins = {};
    priceNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-price')); });
    valueNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-value')); });
    pnlNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-pnl')); });
    ch1hNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-change-1h')); });
    ch24hNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-change-24h')); });
    valueSolNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-value-sol')); });
    if (valueSolNodes.length) addCoin('SOL');
    if (chart && chart.dataset.coinId) addCoin(chart.dataset.coinId);
    coinList = Object.keys(coins);
    if (ws && ws.readyState === 1) {
      try { ws.send(JSON.stringify({ subscribe: coinList })); } catch (e) { /* next tick resubscribes */ }
    }
    fetchSnapshot();
  }
  function fetchHomeHoldings() {
    if (!holdingsList) return;
    fetch('/api/home-holdings', { credentials: 'same-origin' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.ok || !Array.isArray(data.rows)) return;
        var byId = {};
        data.rows.forEach(function (r) { byId[r.coin_id] = r; });
        var changed = false;
        var domRows = holdingsList.querySelectorAll('[data-coin-id]');
        for (var i = 0; i < domRows.length; i++) {
          if (!byId[domRows[i].getAttribute('data-coin-id')]) {
            domRows[i].remove();
            changed = true;
          }
        }
        data.rows.forEach(function (r) {
          var el = holdingsList.querySelector(
            '[data-coin-id="' + r.coin_id + '"]');
          if (el) {
            var solEl = el.querySelector('[data-live-value-sol]');
            if (solEl && typeof r.value_sol === 'string') {
              solEl.textContent = r.value_sol;
            }
            var pctWrap = el.querySelector('.holdings-1h');
            var pctEl = el.querySelector('[data-live-change-1h]');
            if (pctEl && typeof r.change_1h === 'number') {
              pctEl.textContent = fmtPct(r.change_1h, 2);
              if (pctWrap) {
                pctWrap.classList.remove('up', 'down', 'muted');
                pctWrap.classList.add(r.change_1h >= 0 ? 'up' : 'down');
              }
            }
            var sparkEl = el.querySelector('[data-sparkline-coin]');
            if (sparkEl && typeof r.sparkline_svg === 'string'
                && r.sparkline_svg) {
              sparkEl.innerHTML = r.sparkline_svg;
            }
            return;
          }
          if (typeof r.html === 'string' && r.html) {
            holdingsList.insertAdjacentHTML('beforeend', r.html);
            changed = true;
          }
        });
        var emptyEl = holdingsList.querySelector('[data-home-empty]');
        if (emptyEl) emptyEl.hidden = data.rows.length > 0;
        var staleEl = document.querySelector('[data-home-stale]');
        if (staleEl) staleEl.hidden = !data.balances_stale;
        if (changed) {
          refreshLiveCollections();
          startSparkPoll();
        }
      })
      .catch(function () { /* next poll retries; last rows stand */ });
  }
  function startHoldingsPoll() {
    if (holdingsTimer) return;
    fetchHomeHoldings();
    holdingsTimer = setInterval(fetchHomeHoldings, HOLDINGS_POLL_MS);
  }
  function stopHoldingsPoll() {
    if (holdingsTimer) { clearInterval(holdingsTimer); holdingsTimer = null; }
  }
  if (holdingsList) {
    startHoldingsPoll();
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stopHoldingsPoll();
      else startHoldingsPoll();
    });
    window.addEventListener('pagehide', stopHoldingsPoll);
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
  valueSolNodes.forEach(function (n) { addCoin(attrOf(n, 'data-live-value-sol')); });
  /* A holding's SOL value needs the SOL price too, so the feed's
     SOL tick is subscribed whenever a value-sol node exists. */
  if (valueSolNodes.length) addCoin('SOL');
  if (chart && chart.dataset.coinId) addCoin(chart.dataset.coinId);
  var coinList = Object.keys(coins);
  if (coinList.length === 0) return;  /* nothing live on this page */

  if (statusEl) statusEl.hidden = false;

  var ws = null;
  var retryMs = 5000;
  var fallbackTimer = null;
  var watchdogTimer = null;
  var lastTickAt = null;
  var lastSource = null;

  function fmtTime(d) {
    return d.toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }
  /* Significant-figure price formatting: decimals fit the size,
     so a sub-$0.0001 price never renders as $0.0000 and a $100
     price never carries eight decimals. Three significant figures
     (decimals = clamp(floor(-log10(p)) + 3, 2, 12)). */
  function fmtPrice(p) {
    if (!(p > 0) || !isFinite(p)) return 'no data';
    var decimals = Math.floor(-Math.log10(p)) + 3;
    decimals = Math.max(2, Math.min(12, decimals));
    return '$' + p.toFixed(decimals);
  }
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
  function fmtSol(v) { return v.toFixed(5) + ' SOL'; }
  /* A holding's SOL value ticks live too: amount × coin price ÷
     the SOL price. The SOL price comes from the feed's SOL tick
     (subscribed whenever a value-sol node exists); until the
     first SOL tick arrives the 60s holdings refresh keeps the
     values right. A SOL tick recomputes every row; a coin tick
     recomputes only that coin's rows. */
  function recomputeSolValues(changedId, tick) {
    var sol = lastPrices['SOL'];
    if (typeof sol !== 'number' || !(sol > 0)) return;
    valueSolNodes.forEach(function (n) {
      var coinId = attrOf(n, 'data-live-value-sol');
      if (changedId !== 'SOL' && coinId !== changedId) return;
      var price = lastPrices[coinId];
      if (typeof price !== 'number') return;
      var amount = parseFloat(n.getAttribute('data-live-amount'));
      if (isNaN(amount)) return;
      setLive(n, fmtSol(amount * price / sol), tick);
    });
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
     one (a "—" or "no data" placeholder), else the node
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
      statusEl.textContent = '↻ Live feed down — refreshing';
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

  /* No live price for a coin: every node that shows its price,
     value or P&L reads "no data" (the change nodes keep their last
     value — a % change is not a price). Value/P&L totals clear
     their computed figure so a total never sums a stale price. */
  function renderNoData(id) {
    function noData(nodes, attr, clearComputed) {
      nodes.forEach(function (n) {
        if (attrOf(n, attr) !== id) return;
        if (clearComputed) n.dataset.liveComputed = '';
        var target = liveTarget(n);
        target.textContent = 'no data';
        target.classList.add('muted');
        target.classList.remove('up', 'down');
      });
    }
    noData(priceNodes, 'data-live-price', false);
    noData(valueNodes, 'data-live-value', true);
    noData(pnlNodes, 'data-live-pnl', true);
    recomputeTotals();
  }

  function applyTick(t) {
    if (!t || !t.coin_id) return;
    var id = t.coin_id;
    /* A stale tick is no price at all: its nodes read "no data",
       never the dimmed old number — and its value/P&L nodes clear
       too, so no total is computed from a stale price. */
    if (t.stale || typeof t.price !== 'number') {
      renderNoData(id);
      return;
    }
    var asOf = t.as_of ? new Date(t.as_of) : new Date();
    if (isNaN(asOf.getTime())) asOf = new Date();
    lastTickAt = asOf;
    lastSource = t.source || 'feed';
    lastPrices[id] = t.price;

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
    recomputeSolValues(id, t);
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
        /* A subscribed coin the snapshot does not carry has no live
           price right now: its nodes read "no data", never the
           last number. */
        coinList.forEach(function (id) {
          if (!data.ticks[id]) renderNoData(id);
        });
      })
      .catch(function () { /* next poll retries; nodes already
                              read "no data" until a tick lands */ });
  }

  function startFallback() {
    if (fallbackTimer) return;
    fetchSnapshot();
    fallbackTimer = setInterval(fetchSnapshot, 4000);
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
      /* Not "live" yet — only the first tick earns that. A socket
         that connects but never speaks is not live: if no message
         arrives within 10s, drop it so the REST fallback takes over
         and the reconnect backoff runs. */
      setStatus('connecting');
      if (watchdogTimer) clearTimeout(watchdogTimer);
      watchdogTimer = setTimeout(function () {
        if (ws) { try { ws.close(); } catch (e) { /* onclose follows */ } }
      }, 10000);
    };
    ws.onmessage = function (ev) {
      if (watchdogTimer) {
        clearTimeout(watchdogTimer);
        watchdogTimer = null;
      }
      var data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      if (!data) return;
      if (data.type === 'snapshot' && data.ticks) {
        Object.keys(data.ticks).forEach(function (k) {
          applyTick(data.ticks[k]);
        });
        coinList.forEach(function (id) {
          if (!data.ticks[id]) renderNoData(id);
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
    if (watchdogTimer) {
      clearTimeout(watchdogTimer);
      watchdogTimer = null;
    }
    ws = null;
    setStatus('down');
    startFallback();
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 30000);
  }

  setStatus('connecting');
  connect();
})();
