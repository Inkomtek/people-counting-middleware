// Theme toggle and auto-refresh for the Washroom Dashboard.
(function () {
  "use strict";
  var MAX_BACKOFF_MS = 5 * 60 * 1000;
  var root = document.documentElement;
  var timer = null;
  var inFlight = false;
  var failures = 0;
  var lastSuccess = Date.now();

  // ---------- theme ----------
  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-theme-toggle]")) return;
    var dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("wd-theme", root.dataset.theme); } catch (e) {}
  });

  // ---------- location filter ----------
  // Changing Client/Region/Site/Area/Scope resets every level below it to "all" before the form submits
  // (capture phase, so it runs before the select's own onchange submit).
  document.addEventListener("change", function (event) {
    var select = event.target.closest("[data-location-level]");
    if (!select) return;
    var levels = Array.prototype.slice.call(select.form.querySelectorAll("[data-location-level]"));
    levels.slice(levels.indexOf(select) + 1).forEach(function (lower) { lower.value = "all"; });
  }, true);

  // ---------- auto-refresh ----------
  // Every `data-refresh-seconds` (set in Admin > Scheduler config) re-fetch this page and swap in the
  // regions below, keeping scroll position and open controls. Failed fetches show a banner and back off.
  var SWAP = ["#content", "[data-alerts]"];
  var updated = document.querySelector("[data-updated]");
  var updatedText = document.querySelector("[data-updated-text]");
  var offlineAlert = document.querySelector("[data-offline-alert]");
  var clock = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Jakarta", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });

  function intervalMs() {
    var seconds = parseInt(document.body.dataset.refreshSeconds, 10);
    return (seconds >= 10 ? seconds : 60) * 1000;
  }

  function schedule() {
    clearTimeout(timer);
    var delay = failures ? Math.min(intervalMs() * Math.pow(2, failures), MAX_BACKOFF_MS) : intervalMs();
    timer = setTimeout(refresh, delay);
  }

  function setUpdating(on) {
    if (!updated) return;
    updated.classList.toggle("is-updating", on);
    if (on && updatedText) updatedText.textContent = updated.dataset.updating;
  }

  function showUpdated(time) {
    if (updatedText) updatedText.textContent = updated.dataset.label + " " + clock.format(time) + " WIB";
  }

  function assetVersions(doc) {
    return Array.prototype.map.call(
      doc.querySelectorAll('link[rel="stylesheet"][href*="?v="], script[src*="?v="]'),
      function (el) { return el.getAttribute("href") || el.getAttribute("src"); }
    ).join("|");
  }

  function busy() {
    var el = document.activeElement;
    return el && /INPUT|SELECT|TEXTAREA/.test(el.tagName);
  }

  function refresh() {
    if (inFlight) return;
    if (document.hidden || busy()) { schedule(); return; }
    inFlight = true;
    setUpdating(true);
    fetch(window.location.href, { headers: { "X-Requested-With": "fetch" }, cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        // A deploy changed the CSS/JS (their URLs carry a version): swapping new markup into a page
        // still running the old stylesheet breaks the layout, so reload the whole page instead.
        if (assetVersions(doc) !== assetVersions(document)) {
          window.location.reload();
          return;
        }
        SWAP.forEach(function (selector) {
          var fresh = doc.querySelector(selector);
          var current = document.querySelector(selector);
          if (fresh && current) current.innerHTML = fresh.innerHTML;
        });
        // Pick up an interval changed in Admin since the page was opened.
        if (doc.body && doc.body.dataset.refreshSeconds) {
          document.body.dataset.refreshSeconds = doc.body.dataset.refreshSeconds;
        }
        failures = 0;
        lastSuccess = Date.now();
        if (offlineAlert) offlineAlert.hidden = true;
      })
      .catch(function () {
        failures += 1;
        if (offlineAlert) offlineAlert.hidden = false;
      })
      .then(function () {
        inFlight = false;
        setUpdating(false);
        showUpdated(new Date(lastSuccess));
        schedule();
      });
  }

  // Coming back to the tab after a while refreshes at once instead of waiting for the next tick.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && Date.now() - lastSuccess >= intervalMs()) refresh();
  });
  window.addEventListener("online", refresh);

  schedule();
})();
