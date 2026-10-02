// Theme toggle, detail drawer and 1-minute auto-refresh for the Washroom Dashboard.
(function () {
  "use strict";
  var REFRESH_MS = 60 * 1000;
  var root = document.documentElement;
  var drawer = document.getElementById("drawer");
  var backdrop = document.querySelector(".drawer-backdrop");
  var lastFocus = null;

  // ---------- theme ----------
  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-theme-toggle]")) return;
    var dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("wd-theme", root.dataset.theme); } catch (e) {}
  });

  // ---------- drawer ----------
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function jsonBlock(title, value) {
    var block = el("section", "json-block");
    var head = el("header");
    head.appendChild(el("h3", "", title));
    var copy = el("button", "btn btn--ghost btn--sm", "Salin JSON");
    copy.type = "button";
    var text = JSON.stringify(value, null, 2);
    copy.addEventListener("click", function () {
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () { copy.textContent = "Tersalin"; });
      }
    });
    head.appendChild(copy);
    block.appendChild(head);
    block.appendChild(el("pre", "mono", text));
    return block;
  }

  function openDrawer(data) {
    var field = function (name) { return drawer.querySelector('[data-field="' + name + '"]'); };
    var meta = field("meta");
    var blocks = field("blocks");
    meta.textContent = "";
    blocks.textContent = "";
    field("kind").textContent = "Detail Work Order";
    field("title").textContent = data.wo;
    var rows = [["Waktu", data.time], ["Device ID", data.device], ["Tujuan", data.endpoint], ["Status", data.status]];
    blocks.appendChild(jsonBlock("Request body", data.request));
    blocks.appendChild(jsonBlock("Respons server", data.response));
    rows.forEach(function (row) {
      meta.appendChild(el("dt", "", row[0]));
      meta.appendChild(el("dd", "", row[1]));
    });
    lastFocus = document.activeElement;
    drawer.hidden = false;
    backdrop.hidden = false;
    drawer.querySelector("[data-drawer-close]").focus();
  }

  function closeDrawer() {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
    backdrop.hidden = true;
    if (lastFocus) lastFocus.focus();
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-drawer]");
    if (trigger && drawer) {
      openDrawer(JSON.parse(trigger.dataset.detail));
      return;
    }
    if (event.target.closest("[data-drawer-close]")) closeDrawer();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeDrawer();
  });

  // ---------- auto-refresh ----------
  // Re-fetch the current page every minute and swap #content and the "Diperbarui" stamp,
  // skipping a tick while the drawer is open or the user is typing in a field.
  function refresh() {
    var typing = document.activeElement && /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
    if (document.hidden || typing || (drawer && !drawer.hidden)) return;
    fetch(window.location.href, { headers: { "X-Requested-With": "fetch" } })
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) {
        if (!html) return;
        var doc = new DOMParser().parseFromString(html, "text/html");
        ["#content", "[data-updated]"].forEach(function (selector) {
          var fresh = doc.querySelector(selector);
          var current = document.querySelector(selector);
          if (fresh && current) current.innerHTML = fresh.innerHTML;
        });
      })
      .catch(function () {});
  }
  setInterval(refresh, REFRESH_MS);
})();
