/* Live prices over the app's own WebSocket (/ws/live), with a REST
   fallback. Pages mark the numbers they want kept live with data
   attributes — live.js subscribes to exactly those coins, then
   updates prices, holding values (USD and SOL), P&L and 1h/24h %
   as validated ticks arrive. Every tick carries its source and
   time: the status pill in the topbar shows both, and each updated
   node gets them as a tooltip. A tick the server rejected never
   arrives here. The home page also re-fetches its holdings list
   every 5 seconds (/api/home-holdings), so a coin leaves the
   list the moment its balance hits 0 without a manual reload,
   and Page 10 re-fetches its trenches list on the same cadence
   (/api/trenches), so it never sits frozen.
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
          var coin = el.getAttribute('data-sparkline-coin');
          var svg = data.sparklines[coin];
          if (typeof svg === 'string' && svg) el.innerHTML = svg;
          /* The badge's own 1h % comes from the same points the
             chart draws (data.pct_1h) — update it with the chart,
             so the two can never disagree. Home rows have no such
             node (their % is data-live-change-1h) and skip this. */
          var pct = data.pct_1h ? data.pct_1h[coin] : null;
          var pctEl = el.parentNode
            ? el.parentNode.querySelector('[data-sparkline-pct-1h]')
            : null;
          if (pctEl && typeof pct === 'number') {
            pctEl.textContent = fmtPct(pct, 1) + ' 1h';
            pctEl.classList.remove('up', 'down', 'muted');
            pctEl.classList.add(pct >= 0 ? 'up' : 'down');
          }
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

  /* Holdings list auto-refresh (home page): every 5 seconds the
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
            } else if (pctEl && r.change_1h === null) {
              /* The server has no 1h % any more (too few chart
                 points) — clear the stale value to "—" instead of
                 leaving yesterday's % standing. */
              pctEl.textContent = '—';
              if (pctWrap) {
                pctWrap.classList.remove('up', 'down');
                pctWrap.classList.add('muted');
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
  /* Trenches list auto-refresh (Page 10): every 5 seconds the
     page re-fetches /api/trenches and updates the rows in place
     (price, 1h %, sparkline) when the coin set is unchanged. A
     coin that left the server's rows is removed from the DOM,
     and a new coin's row is inserted — so the list never sits
     frozen, without a manual reload. A row the feed AND the
     chain read cannot price is kept reading "no data" (the
     endpoint keeps it), so a stall cannot yank rows off the
     page. Any insert / removal re-collects live.js's node lists
     and re-subscribes the socket, so the new rows tick live too.
     The poll runs while the page is visible and stops when
     hidden, like the holdings poll. */
  var trenchesList = document.querySelector('[data-trenches-list]');
  var TRENCHES_POLL_MS = 5000;
  var trenchesTimer = null;
  function fetchTrenches() {
    if (!trenchesList) return;
    fetch('/api/trenches', { credentials: 'same-origin' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.ok || !Array.isArray(data.rows)) return;
        var byId = {};
        data.rows.forEach(function (r) { byId[r.mint] = r; });
        var changed = false;
        var domRows = trenchesList.querySelectorAll('[data-trenches-mint]');
        for (var i = 0; i < domRows.length; i++) {
          if (!byId[domRows[i].getAttribute('data-trenches-mint')]) {
            domRows[i].remove();
            changed = true;
          }
        }
        data.rows.forEach(function (r) {
          var el = trenchesList.querySelector(
            '[data-trenches-mint="' + r.mint + '"]');
          if (el) {
            var heldNow = el.getAttribute('data-held');
            var heldNext = r.held ? '1' : '0';
            if (heldNow !== null && heldNow !== heldNext
                && typeof r.html === 'string' && r.html) {
              /* Holding appeared or disappeared: the buttons must
                 change with it (Sell All Now in or out) — the
                 in-place price update cannot do that, so swap the
                 whole row for the server's fresh markup. */
              var tmp = document.createElement('div');
              tmp.innerHTML = r.html;
              var fresh = tmp.firstElementChild;
              if (fresh) {
                el.replaceWith(fresh);
                changed = true;
              }
              return;
            }
            var priceEl = el.querySelector('[data-live-price]');
            if (priceEl && typeof r.price_display === 'string') {
              priceEl.textContent = r.price_display;
            }
            var pctEl = el.querySelector('[data-sparkline-pct-1h]');
            if (pctEl && typeof r.change_1h === 'number') {
              pctEl.textContent = fmtPct(r.change_1h, 1) + ' 1h';
              pctEl.classList.remove('up', 'down', 'muted');
              pctEl.classList.add(r.change_1h >= 0 ? 'up' : 'down');
            } else if (pctEl && r.change_1h === null) {
              pctEl.textContent = '—';
              pctEl.classList.remove('up', 'down');
              pctEl.classList.add('muted');
            }
            var sparkEl = el.querySelector('[data-sparkline-coin]');
            if (sparkEl && typeof r.sparkline_svg === 'string'
                && r.sparkline_svg) {
              sparkEl.innerHTML = r.sparkline_svg;
            }
            return;
          }
          if (typeof r.html === 'string' && r.html) {
            trenchesList.insertAdjacentHTML('beforeend', r.html);
            changed = true;
          }
        });
        var emptyEl = trenchesList.querySelector('[data-trenches-empty]');
        if (emptyEl) emptyEl.hidden = data.rows.length > 0;
        if (changed) {
          refreshLiveCollections();
          startSparkPoll();
        }
      })
      .catch(function () { /* next poll retries; last rows stand */ });
  }
  function startTrenchesPoll() {
    if (trenchesTimer) return;
    fetchTrenches();
    trenchesTimer = setInterval(fetchTrenches, TRENCHES_POLL_MS);
  }
  function stopTrenchesPoll() {
    if (trenchesTimer) { clearInterval(trenchesTimer); trenchesTimer = null; }
  }
  if (trenchesList) {
    /* The page carries a 30s meta-refresh backstop so it can never
       sit frozen when this script is stale or dead. The poll is
       running now, so the backstop is removed — the 5s poll owns
       freshness from here and the page never reloads on its own. */
    var staleMeta = document.querySelector('meta[http-equiv="refresh"]');
    if (staleMeta) staleMeta.remove();
    startTrenchesPoll();
    /* One-tap quick buy (batch 6): after a badge trade lands, the
       list re-fetches at once so Sell All Now appears (after a
       buy) or disappears (after a sell) without waiting for the
       next 5s tick. */
    document.addEventListener('trenches:refresh', fetchTrenches);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stopTrenchesPoll();
      else startTrenchesPoll();
    });
    window.addEventListener('pagehide', stopTrenchesPoll);
  }

  /* Coins-page holdings list auto-refresh: every 5 seconds the
     page re-fetches /api/coins-rows and updates the rows in place
     (unit price, 1h %, P&L) when the coin set is unchanged. A coin
     that left the server's rows (its balance hit 0) is removed
     from the DOM, and a new coin's row is inserted — so the list
     never sits frozen, without a manual reload. Same pattern as
     the home holdings poll; the coins page renders the same rows
     in its own markup, so it gets its own endpoint. Any insert /
     removal re-collects live.js's node lists and re-subscribes
     the socket, so the new rows tick live too. The poll runs
     while the page is visible and stops when hidden, like the
     other list polls. */
  var coinsList = document.querySelector('[data-coins-list]');
  var COINS_POLL_MS = 5000;
  var coinsTimer = null;
  function fetchCoinsRows() {
    if (!coinsList) return;
    fetch('/api/coins-rows', { credentials: 'same-origin' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.ok || !Array.isArray(data.rows)) return;
        var byId = {};
        data.rows.forEach(function (r) { byId[r.coin_id] = r; });
        var changed = false;
        var domRows = coinsList.querySelectorAll('[data-coin-id]');
        for (var i = 0; i < domRows.length; i++) {
          if (!byId[domRows[i].getAttribute('data-coin-id')]) {
            domRows[i].remove();
            changed = true;
          }
        }
        data.rows.forEach(function (r) {
          var el = coinsList.querySelector(
            '[data-coin-id="' + r.coin_id + '"]');
          if (el) {
            var priceEl = el.querySelector('[data-live-price]');
            if (priceEl && typeof r.price_display === 'string') {
              priceEl.textContent = r.price_display;
            }
            var pctEl = el.querySelector('[data-live-change-1h]');
            if (pctEl && typeof r.change_1h === 'number') {
              pctEl.textContent = fmtPct(r.change_1h, 1) + ' 1h';
              pctEl.classList.remove('up', 'down', 'muted');
              pctEl.classList.add(r.change_1h >= 0 ? 'up' : 'down');
            } else if (pctEl && r.change_1h === null) {
              pctEl.textContent = '—';
              pctEl.classList.remove('up', 'down');
              pctEl.classList.add('muted');
            }
            var pnlEl = el.querySelector('[data-live-pnl]');
            if (pnlEl && typeof r.pnl_display === 'string') {
              pnlEl.textContent = r.pnl_display;
            } else if (pnlEl && r.pnl === null) {
              pnlEl.textContent = '—';
              pnlEl.classList.add('muted');
            }
            return;
          }
          if (typeof r.html === 'string' && r.html) {
            coinsList.insertAdjacentHTML('beforeend', r.html);
            changed = true;
          }
        });
        /* The empty message is rendered by the server only when
           the list starts empty (the page must not tell a holder
           they hold nothing, even hidden) — so the poll creates
           it when the list empties and removes it when a row
           arrives, using the server's mode label for the text. */
        var emptyEl = coinsList.querySelector('[data-coins-empty]');
        if (data.rows.length === 0) {
          if (!emptyEl) {
            emptyEl = document.createElement('p');
            emptyEl.className = 'muted';
            emptyEl.setAttribute('data-coins-empty', '');
            emptyEl.textContent = 'You hold no coins in '
              + (data.mode_label || '') + ' yet — buy one below.';
            coinsList.appendChild(emptyEl);
          } else {
            emptyEl.hidden = false;
          }
        } else if (emptyEl) {
          emptyEl.remove();
        }
        if (changed) {
          refreshLiveCollections();
        }
      })
      .catch(function () { /* next poll retries; last rows stand */ });
  }
  function startCoinsPoll() {
    if (coinsTimer) return;
    fetchCoinsRows();
    coinsTimer = setInterval(fetchCoinsRows, COINS_POLL_MS);
  }
  function stopCoinsPoll() {
    if (coinsTimer) { clearInterval(coinsTimer); coinsTimer = null; }
  }
  if (coinsList) {
    startCoinsPoll();
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stopCoinsPoll();
      else startCoinsPoll();
    });
    window.addEventListener('pagehide', stopCoinsPoll);
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
     first SOL tick arrives the 5s holdings refresh keeps the
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

  /* Snapshot grace: a fresh trenches mint has no feed tick yet
     (the stream watches held + watched mints only), so a snapshot
     that does not carry it must not blank the server-rendered
     price in the first 30s after page load — the app's own
     staleness bound. After 30s a missing coin is genuinely stale
     and reads "no data" as before. */
  var pageLoadedAt = Date.now();

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
           last number — but not in the first 30s after page load
           (see pageLoadedAt above): a fresh coin's tick has not
           arrived yet and the server price still stands. */
        coinList.forEach(function (id) {
          if (!data.ticks[id] && Date.now() - pageLoadedAt > 30000) {
            renderNoData(id);
          }
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
        /* Same 30s grace as the REST snapshot (pageLoadedAt):
           a fresh coin's tick has not arrived yet. */
        coinList.forEach(function (id) {
          if (!data.ticks[id] && Date.now() - pageLoadedAt > 30000) {
            renderNoData(id);
          }
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
