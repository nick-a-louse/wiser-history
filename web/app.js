/* Wiser heating history.
 *
 * Two charts, not one with two y-axes: temperature (C) and humidity (%) are
 * different scales, and a dual-axis chart invites false correlations. They are
 * linked: one zoom and one set of visible rooms.
 *
 * Theme control and the "Updated" line come from gomez-ui.js.
 */
"use strict";

var BASE = "published/data/";
var TZ = "Europe/London";
var HOUR = 3600000;

var state = {
  meta: null,
  rooms: [],            // [{room_id, name, colorVar, dashed}]
  hours: 24,            // range preset (0 = all history)
  win: null,            // [from, to] of the preset: the x-axis extent
  view: null,           // [from, to] actually visible after zooming
  tier: null,
  series: null,
  targets: false,
  boiler: false,
  hidden: {},           // room name -> true when switched off in the legend
  sort: { key: "swing", dir: -1 },
};

var tempChart, humChart, zoomTimer, loadSeq = 0;

/* ---------- helpers ---------- */

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function getJSON(path) {
  return fetch(BASE + path, { cache: "no-cache" }).then(function (r) {
    if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
    return r.json();
  });
}

function ms(ts) {
  return new Date(ts.length === 10 ? ts + "T00:00:00Z" : ts).getTime();
}

function fmt(t, withTime) {
  var o = withTime
    ? { timeZone: TZ, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }
    : { timeZone: TZ, day: "numeric", month: "short", year: "numeric" };
  return new Date(t).toLocaleString("en-GB", o);
}

function localDay(t) {
  return new Date(t).toLocaleDateString("en-CA", { timeZone: TZ });   // YYYY-MM-DD
}

function showError(msg) {
  var el = document.getElementById("error");
  el.textContent = msg;
  el.hidden = !msg;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, function (m) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]; });
}

/* ---------- colour assignment ----------
 * Eight categorical slots, assigned in fixed order by room id so a room keeps
 * its colour no matter which rooms are visible. Rooms 9 and 10 reuse slots 1-2
 * with a dashed stroke: composite encoding, because a ninth generated hue would
 * not survive a colour-vision check. */
function assignColours(rooms) {
  return rooms
    .slice()
    .sort(function (a, b) { return a.room_id - b.room_id; })
    .map(function (room, i) {
      return { room_id: room.room_id, name: room.name, colorVar: "--series-" + ((i % 8) + 1), dashed: i >= 8 };
    });
}

/* ---------- resolution tiers ---------- */

function pickTier(hours) {
  if (hours && hours <= 168) return "raw";
  if (hours && hours <= 2160) return "half";
  return "daily";
}

function tierLabel(tier) {
  if (tier === "raw") return "Showing every reading (5-minute resolution).";
  if (tier === "half") return "Showing half-hourly averages. Zoom in to 7 days or less to see every reading.";
  return "Showing daily averages. Zoom in to 90 days or less for half-hourly detail.";
}

/* ---------- loading ---------- */

function monthsInRange(from, to) {
  var all = state.meta.months || [];
  if (!from) return all;
  var lo = new Date(from).toISOString().slice(0, 7);
  var hi = new Date(to || Date.now()).toISOString().slice(0, 7);
  var keep = all.filter(function (m) { return m >= lo && m <= hi; });
  // Include the month before the window so the line enters from the left edge.
  var i = all.indexOf(keep[0]);
  if (i > 0) keep.unshift(all[i - 1]);
  return keep.length ? keep : all.slice(-1);
}

function load(tier, from, to) {
  if (tier === "raw") {
    return Promise.all(monthsInRange(from, to).map(function (m) {
      return getJSON("raw/" + m + ".json");
    })).then(function (parts) {
      return {
        rooms: [].concat.apply([], parts.map(function (p) { return p.rooms; })),
        weather: [].concat.apply([], parts.map(function (p) { return p.weather; })),
        boiler: [].concat.apply([], parts.map(function (p) { return p.boiler; })),
        key: "ts_utc",
        step: (state.meta.poll_seconds || 300) * 1000,
        // Weather is polled a third as often as the hub. Using the hub's
        // interval here marked every weather point as a gap, so the outside
        // line was broken between every pair of points and rendered as
        // nothing at all.
        weatherStep: (state.meta.weather_seconds || 900) * 1000,
      };
    });
  }
  if (tier === "half") {
    var lo = from ? new Date(from).toISOString().slice(0, 4) : "0";
    var years = (state.meta.years || []).filter(function (y) { return y >= lo; });
    return Promise.all(years.map(function (y) { return getJSON("30min/" + y + ".json"); }))
      .then(function (parts) {
        return {
          rooms: [].concat.apply([], parts.map(function (p) { return p.rooms; })),
          weather: [].concat.apply([], parts.map(function (p) { return p.weather; })),
          boiler: [],
          key: "bucket", step: 1800000, weatherStep: 1800000,
        };
      });
  }
  return getJSON("daily/all.json").then(function (p) {
    return { rooms: p.rooms, weather: p.weather, boiler: [], key: "day", step: 86400000, weatherStep: 86400000 };
  });
}

/* ---------- series building ---------- */

/* Insert an explicit null wherever sampling stopped, so ECharts breaks the line.
 * Gaps are never bridged: an outage should look like an outage. */
function withGaps(points, step) {
  var out = [];
  for (var i = 0; i < points.length; i++) {
    if (i && points[i][0] - points[i - 1][0] > step * 2) out.push([points[i - 1][0] + step, null]);
    out.push(points[i]);
  }
  return out;
}

function byTime(pts) {
  return pts.slice().sort(function (a, b) { return a[0] - b[0]; });
}

function buildSeries(data, tier) {
  var key = data.key;
  var daily = tier === "daily";
  var byRoom = {}, humByRoom = {}, targetsByRoom = {}, dayRange = {};

  data.rooms.forEach(function (r) {
    var t = ms(r[key]);
    (byRoom[r.room_id] = byRoom[r.room_id] || []).push([t, daily ? r.mean_c : r.temp_c]);
    if (r.humidity_pct !== null && r.humidity_pct !== undefined) {
      (humByRoom[r.room_id] = humByRoom[r.room_id] || []).push([t, r.humidity_pct]);
    }
    if (!daily && r.setpoint_c !== null && r.setpoint_c !== undefined) {
      (targetsByRoom[r.room_id] = targetsByRoom[r.room_id] || []).push([t, r.setpoint_c]);
    }
    if (daily && r.min_c !== null && r.max_c !== null) {
      (dayRange[r.room_id] = dayRange[r.room_id] || []).push([t, r.min_c, r.max_c]);
    }
  });
  Object.keys(byRoom).forEach(function (k) { byRoom[k] = byTime(byRoom[k]); });
  Object.keys(humByRoom).forEach(function (k) { humByRoom[k] = byTime(humByRoom[k]); });

  return {
    byRoom: byRoom, humByRoom: humByRoom, targetsByRoom: targetsByRoom, dayRange: dayRange,
    outTemp: data.weather.map(function (w) { return [ms(w[key]), daily ? w.mean_c : w.temp_c]; }),
    outHum: data.weather.map(function (w) { return [ms(w[key]), w.humidity_pct]; }),
    boiler: data.boiler, step: data.step, weatherStep: data.weatherStep || data.step,
  };
}

/* Contiguous stretches where the boiler was firing, drawn as background bands. */
function boilerBands(boiler) {
  var bands = [], start = null;
  for (var i = 0; i < boiler.length; i++) {
    var on = boiler[i].boiler_relay === "On";
    if (on && start === null) start = ms(boiler[i].ts_utc);
    if (!on && start !== null) { bands.push([{ xAxis: start }, { xAxis: ms(boiler[i].ts_utc) }]); start = null; }
  }
  if (start !== null && boiler.length) bands.push([{ xAxis: start }, { xAxis: ms(boiler[boiler.length - 1].ts_utc) }]);
  return bands;
}

function gapBands() {
  return (state.meta.gaps || []).map(function (g) { return [{ xAxis: ms(g.from) }, { xAxis: ms(g.to) }]; });
}

/* ---------- rendering ---------- */

function axisCommon() {
  return {
    axisLine: { lineStyle: { color: css("--line-strong") } },
    axisLabel: { color: css("--muted"), fontSize: 11, fontFamily: "Barlow, system-ui, sans-serif" },
    splitLine: { lineStyle: { color: css("--line") } },
  };
}

function tooltipBase() {
  return {
    trigger: "axis",
    backgroundColor: css("--surface"),
    borderColor: css("--line-strong"),
    textStyle: { color: css("--ink"), fontSize: 12, fontFamily: "Barlow, system-ui, sans-serif" },
  };
}

function legendBase(names) {
  var selected = {};
  names.forEach(function (n) { selected[n] = !state.hidden[n]; });
  return {
    type: "scroll", top: 0, data: names, selected: selected,
    textStyle: { color: css("--muted"), fontSize: 11, fontFamily: "Barlow, system-ui, sans-serif" },
    inactiveColor: css("--faint"),
    pageIconColor: css("--ink"), pageTextStyle: { color: css("--muted") },
  };
}

/* The same zoom on both charts: the temperature chart shows the slider, the
 * humidity chart has an identical (hidden) one so the linked charts stay in step. */
function zoomBase(showSlider) {
  var z = state.view ? { startValue: state.view[0], endValue: state.view[1] } : { start: 0, end: 100 };
  return [
    Object.assign({ type: "inside", filterMode: "none" }, z),
    Object.assign({
      type: "slider", show: showSlider, height: 26, bottom: 12, filterMode: "none",
      borderColor: css("--line-strong"), fillerColor: "rgba(127,127,127,0.12)",
      handleStyle: { color: css("--surface"), borderColor: css("--accent") },
      moveHandleStyle: { color: css("--line-strong") },
      dataBackground: { lineStyle: { color: css("--faint") }, areaStyle: { color: css("--line") } },
      textStyle: { color: css("--muted"), fontSize: 10 },
    }, z),
  ];
}

function renderTemp() {
  var s = state.series, from = state.win[0], to = state.win[1];
  var series = [];

  state.rooms.forEach(function (room) {
    series.push({
      name: room.name, type: "line", showSymbol: false, connectNulls: false,
      lineStyle: { width: 2, type: room.dashed ? "dashed" : "solid" },
      itemStyle: { color: css(room.colorVar) },
      emphasis: { focus: "series" },
      data: withGaps(s.byRoom[room.room_id] || [], s.step),
    });
  });

  if (state.targets) {
    state.rooms.forEach(function (room) {
      if (state.hidden[room.name]) return;
      var pts = s.targetsByRoom[room.room_id];
      if (!pts || !pts.length) return;
      series.push({
        name: room.name + " target", type: "line", step: "end", showSymbol: false, connectNulls: false,
        lineStyle: { width: 1, type: "dotted", opacity: 0.85 },
        itemStyle: { color: css(room.colorVar) },
        data: withGaps(byTime(pts), s.step),
        tooltip: { show: false },
      });
    });
  }

  // Outside is a reference series, not another category: muted ink, dashed.
  series.push({
    name: "Outside", type: "line", showSymbol: false, connectNulls: false, z: 1,
    lineStyle: { width: 2, type: "dashed" },
    itemStyle: { color: css("--faint") },
    data: withGaps(byTime(s.outTemp), s.weatherStep),
  });

  if (series.length) {
    series[0].markArea = { silent: true, itemStyle: { color: css("--boiler-band") },
      data: state.boiler && s.boiler.length ? boilerBands(s.boiler) : [] };
    series[series.length - 1].markArea = { silent: true, itemStyle: { color: css("--line"), opacity: 0.6 }, data: gapBands() };
  }

  var tip = tooltipBase();
  tip.axisPointer = { type: "cross", label: { backgroundColor: css("--muted") } };
  tip.formatter = function (ps) {
    if (!ps.length) return "";
    var rows = ps.filter(function (p) { return p.value[1] !== null; })
      .sort(function (a, b) { return b.value[1] - a.value[1]; })
      .map(function (p) { return p.marker + esc(p.seriesName) + "  <strong>" + p.value[1].toFixed(1) + "°C</strong>"; });
    return fmt(ps[0].value[0], state.tier !== "daily") + "<br>" + rows.join("<br>");
  };

  tempChart.setOption({
    animation: false, backgroundColor: "transparent",
    grid: { left: 46, right: 14, top: 34, bottom: 64 },
    legend: legendBase(state.rooms.map(function (r) { return r.name; }).concat(["Outside"])),
    tooltip: tip,
    xAxis: Object.assign({ type: "time", min: from, max: to }, axisCommon()),
    yAxis: Object.assign({ type: "value", scale: true, name: "°C", nameTextStyle: { color: css("--muted") } }, axisCommon()),
    dataZoom: zoomBase(true),
    series: series,
  }, true);
}

function renderHumidity() {
  var s = state.series, from = state.win[0], to = state.win[1];
  var series = [], names = [];

  state.rooms.forEach(function (room) {
    var pts = s.humByRoom[room.room_id];
    if (!pts || !pts.length) return;
    names.push(room.name);
    series.push({
      name: room.name, type: "line", showSymbol: false, connectNulls: false,
      lineStyle: { width: 2, type: room.dashed ? "dashed" : "solid" },
      itemStyle: { color: css(room.colorVar) },
      data: withGaps(pts, s.step),
    });
  });
  document.getElementById("hum-note").textContent = names.length
    ? "Only " + listJoin(names) + " " + (names.length === 1 ? "has" : "have") + " a humidity sensor (RoomStat)."
    : "No room has a humidity sensor.";

  series.push({
    name: "Outside", type: "line", showSymbol: false, connectNulls: false,
    lineStyle: { width: 2, type: "dashed" },
    itemStyle: { color: css("--faint") },
    data: withGaps(byTime(s.outHum), s.weatherStep),
  });

  var tip = tooltipBase();
  tip.formatter = function (ps) {
    if (!ps.length) return "";
    var rows = ps.filter(function (p) { return p.value[1] !== null; }).map(function (p) {
      return p.marker + esc(p.seriesName) + "  <strong>" + Math.round(p.value[1]) + "%</strong>";
    });
    return fmt(ps[0].value[0], state.tier !== "daily") + "<br>" + rows.join("<br>");
  };

  humChart.setOption({
    animation: false, backgroundColor: "transparent",
    grid: { left: 46, right: 14, top: 28, bottom: 28 },
    legend: legendBase(names.concat(["Outside"])),
    tooltip: tip,
    xAxis: Object.assign({ type: "time", min: from, max: to }, axisCommon()),
    yAxis: Object.assign({ type: "value", min: 0, max: 100, name: "%", nameTextStyle: { color: css("--muted") } }, axisCommon()),
    dataZoom: zoomBase(false),
    series: series,
  }, true);
}

function listJoin(a) {
  return a.length < 2 ? a.join("") : a.slice(0, -1).join(", ") + " and " + a[a.length - 1];
}

/* ---------- room table (league table for the visible period) ----------
 * Also the relief for three light-mode hues that fall below 3:1 against the
 * surface: identity never rests on colour alone. */
function roomStats() {
  var s = state.series, v = state.view || state.win;
  var inView = function (p) { return p[0] >= v[0] && p[0] <= v[1] && p[1] !== null && p[1] !== undefined; };
  return state.rooms.map(function (room) {
    var pts = (s.byRoom[room.room_id] || []).filter(inView);
    var vals = pts.map(function (p) { return p[1]; });
    var hum = (s.humByRoom[room.room_id] || []).filter(inView);
    var swing = null;
    if (state.tier === "daily") {
      (s.dayRange[room.room_id] || []).forEach(function (d) {
        if (d[0] >= v[0] - 86400000 && d[0] <= v[1]) swing = Math.max(swing === null ? 0 : swing, d[2] - d[1]);
      });
    } else {
      var days = {};
      pts.forEach(function (p) {
        var k = localDay(p[0]), d = days[k] || (days[k] = [Infinity, -Infinity]);
        d[0] = Math.min(d[0], p[1]); d[1] = Math.max(d[1], p[1]);
      });
      Object.keys(days).forEach(function (k) { swing = Math.max(swing === null ? 0 : swing, days[k][1] - days[k][0]); });
    }
    return {
      room: room, name: room.name,
      latest: vals.length ? vals[vals.length - 1] : null,
      mean: vals.length ? vals.reduce(function (a, b) { return a + b; }, 0) / vals.length : null,
      min: vals.length ? Math.min.apply(null, vals) : null,
      max: vals.length ? Math.max.apply(null, vals) : null,
      swing: swing,
      hum: hum.length ? hum[hum.length - 1][1] : null,
    };
  });
}

function renderTable() {
  var key = state.sort.key, dir = state.sort.dir;
  var rows = roomStats().sort(function (a, b) {
    if (key === "name") return dir * a.name.localeCompare(b.name);
    if (a[key] === null) return 1;
    if (b[key] === null) return -1;
    return dir * (a[key] - b[key]);
  });
  var c = function (x, unit, dp) { return x === null ? "–" : x.toFixed(dp === undefined ? 1 : dp) + unit; };
  document.querySelector("#table tbody").innerHTML = rows.map(function (r) {
    return "<tr" + (state.hidden[r.name] ? ' class="quiet"' : "") + "><td><span class='swatch" + (r.room.dashed ? " dashed" : "") +
      "' style='border-top-color:" + css(r.room.colorVar) + "'></span>" + esc(r.name) + "</td>" +
      "<td class='num'>" + c(r.latest, "°C") + "</td><td class='num'>" + c(r.mean, "°C") + "</td>" +
      "<td class='num'>" + c(r.min, "°C") + "</td><td class='num'>" + c(r.max, "°C") + "</td>" +
      "<td class='num'>" + c(r.swing, "°C") + "</td><td class='num'>" + c(r.hum, "%", 0) + "</td></tr>";
  }).join("");
  document.querySelectorAll("#table th").forEach(function (th) {
    var b = th.querySelector("button");
    th.setAttribute("aria-sort", b.dataset.sort === key ? (dir > 0 ? "ascending" : "descending") : "none");
  });
  var v = state.view || state.win;
  document.getElementById("table-note").textContent =
    fmt(v[0], state.tier !== "daily") + " to " + fmt(v[1], state.tier !== "daily") + ". Click a heading to sort.";
}

function renderAll() {
  renderTemp();
  renderHumidity();
  renderTable();
}

/* ---------- freshness and coverage ---------- */

function renderFreshness() {
  var el = document.getElementById("updated");
  var last = state.meta.last_success_utc;
  // The line only becomes a live gomez-ui "Updated" line once meta.json has loaded,
  // so it never flashes "Not updated yet" while the page starts.
  el.className = "g-updated";
  if (window.gomezUI) window.gomezUI.setUpdated(el, last || "", null);
  var cov = state.meta.coverage || {};
  document.getElementById("coverage").textContent = cov.first ? "History from " + fmt(ms(cov.first), false) : "";
}

/* ---------- loading a range, and switching resolution on zoom ---------- */

function refresh() {
  var to = Date.now();
  var cov = state.meta.coverage || {};
  var from = state.hours ? to - state.hours * HOUR : (cov.first ? ms(cov.first) : to - 24 * HOUR);
  state.win = [from, to];
  state.view = null;
  return loadTier(pickTier(state.hours), from, to);
}

function loadTier(tier, from, to) {
  var seq = ++loadSeq;
  document.getElementById("tier-note").textContent = tierLabel(tier);
  return load(tier, from, to).then(function (data) {
    if (seq !== loadSeq) return;            // a newer zoom or range click won
    state.tier = tier;
    state.series = buildSeries(data, tier);
    renderAll();
  });
}

/* Called after the user zooms or pans (slider, wheel, pinch or drag). Works out
 * the visible window; if it needs a finer or coarser tier, loads that tier for
 * just the visible window and redraws without changing the zoom. */
function onZoom() {
  var opt = tempChart.getOption().dataZoom[0];
  var span = state.win[1] - state.win[0];
  var a = opt.startValue !== undefined ? opt.startValue : state.win[0] + span * (opt.start || 0) / 100;
  var b = opt.endValue !== undefined ? opt.endValue : state.win[0] + span * (opt.end === undefined ? 100 : opt.end) / 100;
  state.view = [a, b];
  clearTimeout(zoomTimer);
  zoomTimer = setTimeout(function () {
    var tier = pickTier((b - a) / HOUR);
    if (tier !== state.tier) {
      // Load a little either side so a small pan doesn't immediately need a reload.
      var pad = (b - a) * 0.5;
      loadTier(tier, Math.max(state.win[0], a - pad), Math.min(state.win[1], b + pad))
        .catch(function (err) { showError(err.message); });
    } else {
      renderTable();
    }
  }, 300);
}

/* One zoom across both charts. Done by hand rather than with echarts.connect,
 * which also mirrors legend clicks and fights the room shortcuts below. */
var syncingZoom = false;
function linkZoom(src, dst) {
  src.on("datazoom", function () {
    if (syncingZoom) return;                // this event came from the other chart
    var z = src.getOption().dataZoom[0];
    syncingZoom = true;
    dst.dispatchAction({ type: "dataZoom", start: z.start, end: z.end });
    syncingZoom = false;
    onZoom();
  });
}

/* ---------- rooms: legend, All / None / Only ---------- */

function setHidden(names, hidden) {
  names.forEach(function (n) { if (hidden) state.hidden[n] = true; else delete state.hidden[n]; });
  [tempChart, humChart].forEach(function (ch) {
    names.forEach(function (n) { ch.dispatchAction({ type: hidden ? "legendUnSelect" : "legendSelect", name: n }); });
  });
  if (state.targets) renderTemp();          // targets follow the visible rooms
  renderTable();
}

function onLegend(e) {
  // Mirror a legend click on one chart to the other, so both show the same rooms.
  var on = e.selected[e.name];
  if (on === !state.hidden[e.name]) return;
  document.getElementById("rooms-only").value = "";
  setHidden([e.name], !on);
}

/* ---------- wiring ---------- */

function bind() {
  document.getElementById("ranges").addEventListener("click", function (e) {
    var btn = e.target.closest("button");
    if (!btn) return;
    this.querySelectorAll("button").forEach(function (b) { b.setAttribute("aria-checked", b === btn ? "true" : "false"); });
    state.hours = Number(btn.dataset.hours);
    refresh().catch(function (err) { showError(err.message); });
  });
  document.getElementById("opt-targets").addEventListener("change", function () { state.targets = this.checked; renderTemp(); });
  document.getElementById("opt-boiler").addEventListener("change", function () { state.boiler = this.checked; renderTemp(); });

  var names = function () { return state.rooms.map(function (r) { return r.name; }); };
  document.getElementById("rooms-all").addEventListener("click", function () {
    document.getElementById("rooms-only").value = ""; setHidden(names(), false);
  });
  document.getElementById("rooms-none").addEventListener("click", function () {
    document.getElementById("rooms-only").value = ""; setHidden(names(), true);
  });
  document.getElementById("rooms-only").addEventListener("change", function () {
    var pick = this.value;
    if (!pick) return;
    setHidden(names().filter(function (n) { return n !== pick; }), true);
    setHidden([pick], false);
  });

  document.querySelector("#table thead").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-sort]");
    if (!b) return;
    var k = b.dataset.sort;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === "name" ? 1 : -1) };
    renderTable();
  });

  linkZoom(tempChart, humChart);
  linkZoom(humChart, tempChart);
  tempChart.on("legendselectchanged", onLegend);
  humChart.on("legendselectchanged", onLegend);

  // Colours come from CSS variables, so a theme change has to re-read them.
  document.addEventListener("g:themechange", function () { if (state.series) renderAll(); });
  window.addEventListener("resize", function () { tempChart.resize(); humChart.resize(); });

  // The collector writes every 5 minutes; picking data up on the same cadence
  // keeps the Updated line honest without hammering the server.
  setInterval(function () {
    getJSON("meta.json").then(function (m) { state.meta = m; renderFreshness(); }).catch(function () {});
  }, 300000);
}

function init() {
  tempChart = echarts.init(document.getElementById("chart-temp"), null, { renderer: "canvas" });
  humChart = echarts.init(document.getElementById("chart-hum"), null, { renderer: "canvas" });

  getJSON("meta.json").then(function (meta) {
    state.meta = meta;
    state.rooms = assignColours(meta.rooms || []);
    var only = document.getElementById("rooms-only");
    state.rooms.forEach(function (r) {
      var o = document.createElement("option"); o.value = r.name; o.textContent = r.name; only.appendChild(o);
    });
    renderFreshness();
    bind();
    return refresh();
  }).catch(function (err) {
    showError("Couldn't load the heating data: " + err.message + ". Check the collector is running and publishing.");
  });
}

init();
