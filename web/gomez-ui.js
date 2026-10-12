/* gomez-ui v1.1 — theme control and "Updated" line. Copy unchanged into each app.
   Also put the one-line theme boot snippet in <head> (see 3c) so there's no flash. */
(function () {
  "use strict";
  var KEY = "theme";                      // same key in every app (storage is per-port, so it won't sync)
  var root = document.documentElement;
  var ORDER = ["auto", "light", "dark"];
  var LABEL = { auto: "Theme: follows your device", light: "Theme: light", dark: "Theme: dark" };

  function current() { return root.dataset.theme || "auto"; }
  function apply(mode) {
    if (mode === "auto") delete root.dataset.theme; else root.dataset.theme = mode;
    try { mode === "auto" ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, mode); } catch (e) {}
    document.querySelectorAll(".g-theme").forEach(function (b) {
      b.setAttribute("aria-label", LABEL[mode] + ". Click to change.");
      b.title = LABEL[mode];
    });
    document.dispatchEvent(new CustomEvent("g:themechange", { detail: { mode: mode, dark: isDark() } }));
  }
  function isDark() {
    var t = root.dataset.theme;
    return t ? t === "dark" : !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".g-theme");
    if (!b) return;
    apply(ORDER[(ORDER.indexOf(current()) + 1) % ORDER.length]);
  });
  if (window.matchMedia) matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
    if (current() === "auto") document.dispatchEvent(new CustomEvent("g:themechange", { detail: { mode: "auto", dark: isDark() } }));
  });

  /* ---------- "Updated" line ----------
     <p class="g-updated" data-updated="2026-10-12T14:02:00Z" data-ok-mins="30" data-stale-mins="120"></p>
     data-updated : ISO time of the last successful update (empty = never)
     data-ok-mins : green while younger than this
     data-stale-mins : red once older than this (amber in between)
     data-error   : optional message; shows red "Last update failed" with the message on hover
     Call gomezUI.setUpdated(el, iso, error) after a refresh to update it. */
  function rel(ms) {
    var m = Math.round((Date.now() - ms) / 60000);
    if (m < 1) return "just now";
    if (m < 60) return m + " min ago";
    var d = new Date(ms), now = new Date();
    var hhmm = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return "today " + hhmm;
    var y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return "yesterday " + hhmm;
    return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }) + " " + hhmm;
  }
  function render(el) {
    var iso = el.dataset.updated, err = el.dataset.error;
    var ok = +(el.dataset.okMins || 30), stale = +(el.dataset.staleMins || ok * 4);
    if (!iso) { el.dataset.state = err ? "error" : "stale"; el.textContent = err ? "Update failed" : "Not updated yet"; el.title = err || ""; return; }
    var t = Date.parse(iso), age = (Date.now() - t) / 60000;
    var full = new Date(t).toLocaleString("en-GB", { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
    if (err) { el.dataset.state = "error"; el.textContent = "Last update failed · data from " + rel(t); el.title = err + " — last good update " + full; return; }
    el.dataset.state = age <= ok ? "ok" : age <= stale ? "late" : "stale";
    el.textContent = "Updated " + rel(t);
    el.title = "Last updated " + full;
  }
  function renderAll() { document.querySelectorAll(".g-updated").forEach(render); }
  function setUpdated(el, iso, error) {
    if (iso !== undefined) el.dataset.updated = iso || "";
    if (error) el.dataset.error = error; else delete el.dataset.error;
    render(el);
  }

  window.gomezUI = { isDark: isDark, setTheme: apply, setUpdated: setUpdated, renderUpdated: renderAll };
  document.addEventListener("DOMContentLoaded", function () { apply(current()); renderAll(); });
  setInterval(renderAll, 60000);
})();
