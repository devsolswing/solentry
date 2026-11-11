/* Quick Buy/Sell popup: live coin search as you type, $/tokens
   amount at the live price, same buy/sell endpoints as the coin
   popup and detail page. Config comes from data attributes on the
   overlay element (data-search-url, data-currency, data-mode-label)
   so this file needs no template code. Any button with
   data-quick-trade-open opens the popup; data-trade="buy" or "sell"
   focuses that side of the ticket. */
(function () {
  'use strict';
  var qtOverlay = document.getElementById('quick-trade-overlay');
  if (!qtOverlay) return;
  var qtSearch = document.getElementById('quick-trade-search');
  var qtResults = document.getElementById('quick-trade-results');
  var qtSelected = document.getElementById('quick-trade-selected');
  var qtBuyForm = document.getElementById('quick-trade-buy-form');
  var qtSellForm = document.getElementById('quick-trade-sell-form');
  var qtSellAllForm = document.getElementById('quick-trade-sellall-form');
  var qtBuyAmount = document.getElementById('quick-trade-buy-amount');
  var qtSellAmount = document.getElementById('quick-trade-sell-amount');
  var searchUrl = qtOverlay.dataset.searchUrl || '/api/coins/search';
  var mode = qtOverlay.dataset.mode || 'paper';
  var modeLabel = qtOverlay.dataset.modeLabel || '';
  // Paper amounts are CAD (fake money); real-mode amounts are $ at
  // the live USD price. The live coin price itself stays $ (CoinGecko USD).
  var qtCur = qtOverlay.dataset.currency || '$';
  var qtCoin = null;
  var qtTimer = null;

  function qtClose() { qtOverlay.classList.add('hidden'); }
  function qtOpen(side) {
    qtOverlay.classList.remove('hidden');
    if (qtSearch) qtSearch.focus();
    if (side === 'buy' && qtBuyAmount) qtBuyAmount.focus();
    if (side === 'sell' && qtSellAmount) qtSellAmount.focus();
  }
  function qtAmountKind() {
    var kind = (document.querySelector('input[name="quick-amount-kind"]:checked') || {}).value || 'usd';
    qtBuyAmount.name = kind === 'tokens' ? 'amount_tokens' : 'usd';
    qtSellAmount.name = kind === 'tokens' ? 'amount' : 'amount_usd';
    qtEquiv();
  }
  function qtEquiv() {
    var el = document.getElementById('quick-trade-equiv');
    if (!qtCoin || qtCoin.price_usd == null) { el.textContent = ''; return; }
    var kind = (document.querySelector('input[name="quick-amount-kind"]:checked') || {}).value || 'usd';
    var buyVal = parseFloat(qtBuyAmount.value);
    var sellVal = parseFloat(qtSellAmount.value);
    var val = !isNaN(buyVal) && buyVal > 0 ? buyVal : (!isNaN(sellVal) && sellVal > 0 ? sellVal : null);
    if (val == null) {
      el.textContent = 'Live price: $' + qtCoin.price_usd + ' per ' + qtCoin.symbol + '.';
      return;
    }
    if (kind === 'usd') {
      el.textContent = qtCur + val + ' ≈ ' + (val / qtCoin.price_usd).toFixed(4) + ' ' + qtCoin.symbol + ' at the live price.';
    } else {
      el.textContent = val + ' ' + qtCoin.symbol + ' ≈ ' + qtCur + (val * qtCoin.price_usd).toFixed(2) + ' at the live price.';
    }
  }
  function qtSelect(r) {
    qtCoin = r;
    document.getElementById('quick-trade-name').textContent = r.name;
    document.getElementById('quick-trade-symbol').textContent = '(' + r.symbol + ')';
    document.getElementById('quick-trade-price').textContent = r.price_usd != null ? '$' + r.price_usd : 'Price unavailable';
    // A devnet mint with no feed price is still tradeable in real
    // mode: the buy is priced from the bonding curve in SOL, and
    // the executor refuses plainly when there is no devnet curve.
    // Paper trades need a price, so they stay refused without one.
    var canTrade = r.price_usd != null || (mode === 'devnet' && r.is_mint);
    document.getElementById('quick-trade-source').textContent = r.price_usd != null
      ? 'Live price.'
      : (canTrade
        ? 'No live price — the buy is priced from the bonding curve (devnet).'
        : 'No live price right now — the trade will be refused rather than guessed.');
    // Token amounts cannot convert without a price: force $ when
    // there is none, so the ticket never offers a refused trade.
    var qtTokensRadio = document.querySelector('input[name="quick-amount-kind"][value="tokens"]');
    if (qtTokensRadio) {
      qtTokensRadio.disabled = r.price_usd == null;
      if (r.price_usd == null) {
        var qtUsdRadio = document.querySelector('input[name="quick-amount-kind"][value="usd"]');
        if (qtUsdRadio) qtUsdRadio.checked = true;
      }
    }
    document.getElementById('quick-trade-held').textContent = (r.held > 0)
      ? 'You hold ' + r.held + ' ' + r.symbol + ' in ' + modeLabel + '.'
      : 'You hold none of this coin in ' + modeLabel + '.';
    qtBuyForm.action = '/coins/' + encodeURIComponent(r.coin_id) + '/buy';
    qtSellForm.action = '/coins/' + encodeURIComponent(r.coin_id) + '/sell';
    qtSellAllForm.action = '/coins/' + encodeURIComponent(r.coin_id) + '/sell';
    document.getElementById('quick-trade-buy-btn').disabled = !canTrade;
    document.getElementById('quick-trade-sell-btn').disabled = !canTrade;
    qtSellAllForm.classList.toggle('hidden', !(r.held > 0));
    qtSelected.classList.remove('hidden');
    qtResults.innerHTML = '';
    qtAmountKind();
  }

  /* One small tag per verified network the coin exists on
     (a multi-chain coin shows each one); a wrapped token gets a
     Wrapped tag after its network. No network the app verified
     means a "Network unknown" tag — never a guessed network. */
  function qtNetworkTags(r) {
    var tags = [];
    var networks = (r.networks && r.networks.length)
      ? r.networks : [null];
    networks.forEach(function (net) {
      var t = document.createElement('span');
      t.className = 'search-result-network';
      t.textContent = net || 'Network unknown';
      tags.push(t);
    });
    if (r.wrapped) {
      var w = document.createElement('span');
      w.className = 'search-result-network';
      w.textContent = 'Wrapped';
      tags.push(w);
    }
    return tags;
  }

  document.querySelectorAll('[data-quick-trade-open]').forEach(function (btn) {
    btn.addEventListener('click', function () { qtOpen(btn.dataset.trade || null); });
  });
  var qtCloseBtn = document.getElementById('quick-trade-close');
  if (qtCloseBtn) qtCloseBtn.addEventListener('click', qtClose);
  var qtBackBtn = document.getElementById('quick-trade-back');
  if (qtBackBtn) qtBackBtn.addEventListener('click', qtClose);
  qtOverlay.addEventListener('click', function (e) { if (e.target === qtOverlay) qtClose(); });
  qtSearch.addEventListener('input', function () {
    clearTimeout(qtTimer);
    var q = qtSearch.value.trim();
    if (q.length < 2) { qtResults.innerHTML = ''; return; }
    qtTimer = setTimeout(function () {
      fetch(searchUrl + '?q=' + encodeURIComponent(q))
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (!data.results || !data.results.length) {
            qtResults.innerHTML = '<p class="muted">' + (data.error ? String(data.error).replace(/</g, '&lt;') : 'No coins found — try another name or paste a token address.') + '</p>';
            return;
          }
          qtResults.innerHTML = '';
          data.results.forEach(function (r) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'search-result';
            var main = document.createElement('span');
            main.textContent = r.name + ' (' + r.symbol + ')';
            b.appendChild(main);
            b.appendChild(document.createTextNode(' '));
            qtNetworkTags(r).forEach(function (t) {
              b.appendChild(t);
            });
            var pr = document.createElement('span');
            pr.textContent = r.price_usd != null ? ' — $' + r.price_usd : ' — price unavailable';
            b.appendChild(pr);
            b.addEventListener('click', function () { qtSelect(r); });
            qtResults.appendChild(b);
          });
        })
        .catch(function () {
          qtResults.innerHTML = '<p class="muted">Search unavailable right now.</p>';
        });
    }, 300);
  });
  document.querySelectorAll('input[name="quick-amount-kind"]').forEach(function (radio) {
    radio.addEventListener('change', qtAmountKind);
  });
  qtBuyAmount.addEventListener('input', qtEquiv);
  qtSellAmount.addEventListener('input', qtEquiv);

  /* A link can open the popup directly: /wallet?trade=buy, or the
     coins page with ?trade=sell — used by the home BUY/SELL buttons
     when they link here instead of opening the popup in place. */
  var params = new URLSearchParams(window.location.search);
  var trade = params.get('trade');
  if (trade === 'buy' || trade === 'sell') qtOpen(trade);
})();
