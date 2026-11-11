/* SolEntry UI helpers: balance hide/show, bot carousel arrows,
   and the X (back) button. Plain static JS — no secrets, no
   network calls, works in any in-app browser. */
(function () {
  'use strict';

  /* Eye: hide / show every [data-balance] value on the page. The
     real text is kept in a data attribute on first hide, so the
     true value is never lost. */
  document.querySelectorAll('[data-eye-toggle]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var hidden = btn.getAttribute('aria-pressed') === 'true';
      document.querySelectorAll('[data-balance]').forEach(function (el) {
        if (!hidden) {
          if (!el.dataset.realText) el.dataset.realText = el.textContent;
          el.textContent = '••••••';
        } else if (el.dataset.realText) {
          el.textContent = el.dataset.realText;
        }
      });
      btn.setAttribute('aria-pressed', String(!hidden));
      btn.textContent = hidden ? '👁' : '🙈';
    });
  });

  /* Bot carousel: < and > scroll the strip by one card. The strip
     also scrolls by touch — the arrows are a second way to move. */
  document.querySelectorAll('[data-carousel]').forEach(function (strip) {
    var step = function () {
      var card = strip.querySelector('.bot-card');
      return card ? card.offsetWidth + 12 : 300;
    };
    document.querySelectorAll('[data-carousel-prev]').forEach(function (b) {
      b.addEventListener('click', function () {
        strip.scrollBy({ left: -step(), behavior: 'smooth' });
      });
    });
    document.querySelectorAll('[data-carousel-next]').forEach(function (b) {
      b.addEventListener('click', function () {
        strip.scrollBy({ left: step(), behavior: 'smooth' });
      });
    });
    /* Keep the + / − / Edit buttons pointed at the card in view. */
    var sync = function () {
      var cards = strip.querySelectorAll('.bot-card');
      if (!cards.length) return;
      var mid = strip.scrollLeft + strip.clientWidth / 2;
      var active = cards[0];
      cards.forEach(function (c) {
        if (c.offsetLeft + c.offsetWidth / 2 <= mid) active = c;
      });
      strip.querySelectorAll('.bot-card').forEach(function (c) {
        c.classList.toggle('active', c === active);
      });
      document.querySelectorAll('[data-edit-target]').forEach(function (a) {
        if (active.dataset.editUrl) a.href = active.dataset.editUrl;
      });
      document.querySelectorAll('[data-delete-target]').forEach(function (a) {
        if (active.dataset.deleteUrl) a.href = active.dataset.deleteUrl;
      });
    };
    strip.addEventListener('scroll', sync, { passive: true });
    sync();
  });

  /* X: close an open popup if one is showing, otherwise go back.
     With no history to go back to, land on the home page. */
  document.querySelectorAll('[data-go-back]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var open = document.querySelector('.overlay:not(.hidden)');
      if (open) {
        open.classList.add('hidden');
        return;
      }
      if (window.history.length > 1) {
        window.history.back();
      } else {
        window.location.href = btn.dataset.fallback || '/wallet';
      }
    });
  });

  /* Generic popup windows: any button with data-overlay-open="<id>"
     opens that overlay; any button with data-overlay-close closes
     the overlay it sits in. Used by the home Receive window and
     the coin page's Send / Receive / Buy / Sell windows. */
  document.querySelectorAll('[data-overlay-open]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var el = document.getElementById(btn.dataset.overlayOpen);
      if (el) el.classList.remove('hidden');
    });
  });
  document.querySelectorAll('[data-overlay-close]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var el = btn.closest('.overlay');
      if (el) el.classList.add('hidden');
    });
  });

  /* Platform-wallet rotator (Bots page): < and > cycle through the
     platform panels one at a time, wrapping around. The panels are
     all visible without JS; once this runs, only the active one
     shows. The counter reads "1 of 3 · pump.fun". */
  document.querySelectorAll('[data-wallet-rotator]').forEach(function (rot) {
    var panels = rot.querySelectorAll('[data-wallet-panel]');
    if (!panels.length) return;
    var counter = rot.parentElement.querySelector('[data-wallet-counter]');
    var idx = 0;
    var show = function (i) {
      idx = (i + panels.length) % panels.length;
      panels.forEach(function (p, j) {
        p.classList.toggle('active', j === idx);
      });
      if (counter) {
        counter.textContent = (idx + 1) + ' of ' + panels.length + ' · ' +
          (panels[idx].dataset.platform || '');
      }
    };
    rot.setAttribute('data-rotating', '');
    rot.querySelectorAll('[data-wallet-prev]').forEach(function (b) {
      b.addEventListener('click', function () { show(idx - 1); });
    });
    rot.querySelectorAll('[data-wallet-next]').forEach(function (b) {
      b.addEventListener('click', function () { show(idx + 1); });
    });
    show(0);
  });
})();
