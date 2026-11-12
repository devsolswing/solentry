// Jupiter route box (coin detail page).
// Fetches the live Jupiter route for this coin ("Finding route…"
// while it loads). Never invents a route: every string shown
// comes from the server, rendered as text.
(function () {
  var box = document.querySelector("[data-jupiter-route]");
  if (!box) return;

  // --- Jupiter route box ---
  var status = document.getElementById("jupiter-route-status");
  var routeUrl = box.getAttribute("data-route-url");
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

  // --- Wrapped majors picker ---
  // A text box that drops down every wrapped major with a live
  // liquid Jupiter pool right now. Identical in look to the
  // quick-trade token selector: the same text input, the same
  // .search-result rows. The list is live-checked every time the
  // picker is opened and again the moment a major is picked, so
  // a pool that went dead disappears and a new liquid one
  // appears; typing filters the list. Picking one switches this
  // page to that coin by navigating to its detail page — the
  // same as tapping any coin in the list — so the new page
  // renders price and route fresh from the server
  // and nothing can be stale. (Re-rendering the whole coin page
  // in place would mean rebuilding the chart, the overlays and
  // the route box in JS and re-binding the route box, the live
  // ticker and the overlays; the navigation reuses the server
  // render that already guarantees fresh.)
  var majorsBox = document.getElementById("wrapped-majors");
  if (majorsBox) {
    var majorsInput = document.getElementById("wrapped-majors-search");
    var majorsResults = document.getElementById("wrapped-majors-results");
    var majorsUrl = majorsBox.getAttribute("data-wrapped-majors-url");
    var majorsCache = null;

    function majorsLine(text) {
      majorsResults.innerHTML = "";
      var p = document.createElement("p");
      p.className = "muted small";
      p.textContent = text;
      majorsResults.appendChild(p);
    }

    function renderMajors(list, filterText) {
      majorsResults.innerHTML = "";
      var q = (filterText || "").trim().toLowerCase();
      var shown = list.filter(function (m) {
        if (!q) return true;
        return m.display_name.toLowerCase().indexOf(q) !== -1 ||
          m.symbol.toLowerCase().indexOf(q) !== -1;
      });
      if (!shown.length) {
        majorsLine(list.length
          ? "No wrapped major matches that."
          : "No liquid wrapped majors right now.");
        return;
      }
      shown.forEach(function (m) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "search-result";
        var main = document.createElement("span");
        main.textContent = m.display_name + " (" + m.symbol + ")";
        b.appendChild(main);
        var route = document.createElement("span");
        route.textContent = " — via " + (m.route_labels || []).join(" → ") +
          " · price impact " + m.price_impact_pct + "%";
        b.appendChild(route);
        b.addEventListener("click", function () {
          majorsInput.value = m.display_name;
          majorsResults.innerHTML = "";
          majorsLine("Checking " + m.display_name + "…");
          fetch(majorsUrl).then(function (resp) { return resp.json(); }).then(function (data) {
            var stillLiquid = data.ok && (data.majors || []).some(function (x) {
              return x.coin_id === m.coin_id;
            });
            if (stillLiquid || !data.ok) {
              // Still liquid — or the re-check itself failed, in
              // which case the coin's own page re-checks live and
              // says so plainly if the route is dead.
              window.location.href = "/coins/" + encodeURIComponent(m.coin_id) + "/detail";
              return;
            }
            majorsCache = data.majors || [];
            majorsLine(m.display_name + " has no liquid route right now — pick another.");
            renderMajors(majorsCache, "");
          }).catch(function () {
            window.location.href = "/coins/" + encodeURIComponent(m.coin_id) + "/detail";
          });
        });
        majorsResults.appendChild(b);
      });
    }

    function fetchMajors() {
      majorsLine("Checking pools…");
      return fetch(majorsUrl).then(function (resp) { return resp.json(); }).then(function (data) {
        if (!data.ok) {
          majorsCache = null;
          majorsLine(data.error || "Wrapped majors unavailable right now.");
          return data;
        }
        majorsCache = data.majors || [];
        renderMajors(majorsCache, majorsInput.value);
        return data;
      }).catch(function () {
        majorsCache = null;
        majorsLine("Wrapped majors unavailable right now.");
        return null;
      });
    }

    majorsInput.addEventListener("focus", fetchMajors);
    majorsInput.addEventListener("input", function () {
      if (majorsCache) renderMajors(majorsCache, majorsInput.value);
      else fetchMajors();
    });
    document.addEventListener("click", function (e) {
      if (!majorsBox.contains(e.target)) majorsResults.innerHTML = "";
    });
  }
})();
