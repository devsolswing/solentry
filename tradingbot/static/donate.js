/* SolEntry donate card: Phantom wallet connect + SOL donation.
 *
 * The donor's own Phantom wallet pays. This script only builds the
 * transfer and asks Phantom to sign/send it; the server never touches
 * the donor's money and only records the confirmed donation.
 * No connection is faked: without Phantom, the page shows plain steps
 * to open it in Phantom's in-app browser instead.
 *
 * The destination address is NOT in the page: the script fetches it
 * from /api/donate/info when the donor taps Send, and only then
 * builds the transfer. The page shows only the masked address
 * (first 5 characters + '*****'). Phantom shows the full
 * destination when the donor approves — the masking is the page
 * display, not the signed transaction. When no recipient is
 * configured, /api/donate/info answers 503 and the send is
 * refused plainly.
 */
(function () {
  'use strict';

  var card = document.getElementById('donate-card');
  if (!card) { return; }

  var network = card.getAttribute('data-network');
  var rpcUrl = card.getAttribute('data-rpc-url');
  var connectBtn = document.getElementById('donate-connect');
  var amountInput = document.getElementById('donate-amount');
  var sendBtn = document.getElementById('donate-send');
  var statusEl = document.getElementById('donate-status');
  var helpEl = document.getElementById('donate-phantom-help');
  var csrfToken = '';
  var csrfInput = document.querySelector('input[name="csrf_token"]');
  if (csrfInput) { csrfToken = csrfInput.value; }

  var provider = null;
  var donorAddress = null;

  function setStatus(msg, isError) {
    statusEl.textContent = msg || '';
    statusEl.classList.toggle('err', !!isError);
  }

  function shortAddr(addr) {
    return addr.slice(0, 4) + '...' + addr.slice(-4);
  }

  function refreshSend() {
    var amount = parseFloat(amountInput.value);
    sendBtn.disabled = !(donorAddress && amount > 0);
  }

  function findProvider() {
    if (window.solana && window.solana.isPhantom) { return window.solana; }
    if (window.phantom && window.phantom.solana) { return window.phantom.solana; }
    return null;
  }

  function loadWeb3() {
    if (window.solanaWeb3) { return Promise.resolve(window.solanaWeb3); }
    var urls = [
      'https://cdn.jsdelivr.net/npm/@solana/web3.js@1.98.0/lib/index.iife.min.js',
      'https://unpkg.com/@solana/web3.js@1.98.0/lib/index.iife.min.js'
    ];
    function tryLoad(i) {
      if (i >= urls.length) {
        return Promise.reject(new Error('Solana library failed to load'));
      }
      return new Promise(function (resolve, reject) {
        var s = document.createElement('script');
        s.src = urls[i];
        s.onload = function () { resolve(window.solanaWeb3); };
        s.onerror = function () { resolve(tryLoad(i + 1)); };
        document.head.appendChild(s);
      });
    }
    return tryLoad(0);
  }

  connectBtn.addEventListener('click', function () {
    provider = findProvider();
    if (!provider) {
      helpEl.hidden = false;
      setStatus('Phantom wallet not found in this browser.', true);
      return;
    }
    helpEl.hidden = true;
    setStatus('Connecting to Phantom…');
    connectBtn.disabled = true;
    provider.connect()
      .then(function (resp) {
        donorAddress = resp.publicKey.toString();
        connectBtn.textContent = '● Connected: ' + shortAddr(donorAddress);
        connectBtn.classList.add('connected');
        connectBtn.disabled = false;
        setStatus('Wallet connected. Enter an amount and send your donation.');
        refreshSend();
      })
      .catch(function (err) {
        connectBtn.disabled = false;
        setStatus('Could not connect to Phantom: ' +
          (err && err.message ? err.message : 'connection refused'), true);
      });
  });

  amountInput.addEventListener('input', refreshSend);

  sendBtn.addEventListener('click', function () {
    var amount = parseFloat(amountInput.value);
    if (!donorAddress || !(amount > 0)) { return; }
    sendBtn.disabled = true;
    setStatus('Building your donation — Phantom will ask you to approve it…');
    loadWeb3()
      .then(function (web3) {
        // The recipient is fetched only now, at send time — the page
        // never carries the full address. A non-OK answer means
        // donations are not configured: refuse plainly.
        return fetch('/api/donate/info').then(function (resp) {
          if (!resp.ok) { throw new Error('Donations are not set up yet. Please try again later.'); }
          return resp.json();
        }).then(function (info) {
          if (!info || !info.donation_address) { throw new Error('Donations are not set up yet. Please try again later.'); }
          var connection = new web3.Connection(rpcUrl, 'confirmed');
          var tx = new web3.Transaction().add(
            web3.SystemProgram.transfer({
              fromPubkey: new web3.PublicKey(donorAddress),
              toPubkey: new web3.PublicKey(info.donation_address),
              lamports: Math.round(amount * 1000000000)
            })
          );
          tx.feePayer = new web3.PublicKey(donorAddress);
          return connection.getLatestBlockhash().then(function (bh) {
            tx.recentBlockhash = bh.blockhash;
            return provider.signAndSendTransaction(tx);
          });
        });
      })
      .then(function (result) {
        var signature = typeof result === 'string'
          ? result
          : (result && result.signature ? result.signature : '');
        if (!signature) { throw new Error('Phantom did not return a signature'); }
        return fetch('/api/donate/record', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            csrf_token: csrfToken,
            donor_address: donorAddress,
            amount_sol: amount,
            signature: signature,
            network: network
          })
        }).then(function (resp) {
          if (!resp.ok) { throw new Error('Donation sent, but the record failed to save (status ' + resp.status + ')'); }
          return signature;
        });
      })
      .then(function (signature) {
        setStatus('Thank you! Your donation of ' + amount + ' SOL was sent. Signature: ' + shortAddr(signature));
        amountInput.value = '';
        refreshSend();
        sendBtn.disabled = false;
      })
      .catch(function (err) {
        setStatus('Donation failed: ' + (err && err.message ? err.message : 'unknown error'), true);
        refreshSend();
      });
  });

  refreshSend();
})();
