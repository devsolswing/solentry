// Jupiter route box + per-coin wallet step (coin detail page).
// Fetches the live Jupiter route for this coin ("Finding route…"
// while it loads) and handles the "Create wallet" tap. The wallet
// for the coin is recorded, the button is replaced by the green-dot
// connected indicator showing the real address (first 4 + last 4),
// and the Buy buttons unlock. Never invents a route or an address:
// every string shown comes from the server, rendered as text.
(function () {
  var box = document.querySelector("[data-jupiter-route]");
  var walletBox = document.getElementById("coin-wallet");
  if (!box && !walletBox) return;

  function csrfToken() {
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : "";
  }

  function unlockBuy() {
    document.querySelectorAll("[data-buy-btn], [data-buy-submit]")
      .forEach(function (el) { el.removeAttribute("disabled"); });
  }

  // --- "Create wallet" tap ---
  if (walletBox) {
    var createBtn = walletBox.querySelector("[data-wallet-create]");
    if (createBtn) {
      createBtn.addEventListener("click", function () {
        var url = walletBox.getAttribute("data-wallet-url");
        createBtn.disabled = true;
        createBtn.textContent = "Creating wallet…";
        fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: "csrf_token=" + encodeURIComponent(csrfToken()),
        }).then(function (resp) { return resp.json(); }).then(function (data) {
          if (data.ok) {
            walletBox.innerHTML = "";
            var p = document.createElement("p");
            p.className = "muted small";
            p.id = "wallet-connected";
            var dot = document.createElement("span");
            dot.style.cssText = "display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;vertical-align:middle;";
            var addr = document.createElement("span");
            addr.textContent = " " + data.truncated;
            p.appendChild(dot);
            p.appendChild(addr);
            walletBox.appendChild(p);
            unlockBuy();
          } else {
            createBtn.disabled = false;
            createBtn.textContent = "Create wallet";
            var err = document.createElement("p");
            err.className = "muted small";
            err.textContent = data.error || "Could not create the wallet.";
            walletBox.appendChild(err);
          }
        }).catch(function () {
          createBtn.disabled = false;
          createBtn.textContent = "Create wallet";
        });
      });
    }
  }

  // --- Jupiter route box ---
  if (!box || !walletBox) return;
  var status = document.getElementById("jupiter-route-status");
  var routeUrl = walletBox.getAttribute("data-route-url");
  if (!routeUrl || !status) return;

  var currentSide = "buy";
  var timer = null;

  function amountFor(side) {
    var input = document.querySelector(
      '[data-jupiter-amount="' + side + '"]');
    var value = input && input.value ? parseFloat(input.value) : NaN;
    return (isFinite(value) && value > 0) ? value : null;
  }

  function showLine(text) {
    box.innerHTML = "";
    var p = document.createElement("p");
    p.className = "muted small";
    p.textContent = text;
    box.appendChild(p);
  }

  function fetchRoute() {
    var amount = amountFor(currentSide);
    var url = routeUrl + "?side=" + currentSide;
    if (amount) url += "&amount_sol=" + encodeURIComponent(amount);
    status.textContent = "Finding route…";
    fetch(url).then(function (resp) { return resp.json(); }).then(function (data) {
      if (!data.ok) {
        showLine(data.error || "No route found for this coin.");
        return;
      }
      var labels = (data.route_labels || []).join(" → ");
      var when = new Date(data.fetched_at).toLocaleTimeString();
      if (data.side === "sell") {
        showLine("Route via " + labels + " · expected " +
          data.out_amount_ui + " SOL · price impact " +
          data.price_impact_pct + "% · fetched " + when);
      } else {
        showLine("Route via " + labels + " · " +
          data.in_amount_ui + " SOL → " + data.out_amount_ui + " " +
          data.display_name + " · price impact " +
          data.price_impact_pct + "% · fetched " + when);
      }
    }).catch(function () {
      showLine("Route unavailable right now.");
    });
  }

  fetchRoute();
  document.querySelectorAll("[data-jupiter-amount]").forEach(function (input) {
    input.addEventListener("input", function () {
      currentSide = input.getAttribute("data-jupiter-amount");
      if (timer) clearTimeout(timer);
      timer = setTimeout(fetchRoute, 600);
    });
  });
})();
