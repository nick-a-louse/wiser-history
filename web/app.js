/* Wiser heating history.
 *
 * Two charts, not one with two y-axes: temperature (C) and humidity (%) are
 * different scales, and a dual-axis chart invites false correlations. They share
 * an x-axis and a single zoom control instead.
 */
"use strict";

var BASE = "published/data/";
var TZ = "Europe/London";

var state = {
  meta: null,
  rooms: [],            // [{room_id, name, colorVar, dashed}]
  hours: 24,
  tier: null,
  series: null,
  targets: false,
  boiler: false,
};

var tempChart, humChart;

/* ---------- helpers ---------- */

function css(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
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

function showError(msg) {
  var el = document.getElementById("error");
  el.textContent = msg;
  el.hidden = false;
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
      return {
        room_id: room.room_id,
        name: room.name,
        colorVar: "--series-" + ((i % 8) + 1),
        dashed: i >= 8,
      };
    });
}

/* ---------- tier selection ---------- */

function pickTier(hours) {
  if (hours && hours <= 168) return "raw";
  if (hours && hours <= 2160) return "half";
  return "daily";
}

function tierLabel(tier) {
  if (tier === "raw") return "Showing every reading (5-minute resolution).";
  if (tier === "half") return "Showing half-hourly averages — zoom in past 7 days for every reading.";
  return "Showing daily averages — zoom in past 90 days for finer detail.";
}

/* ---------- loading ---------- */

function monthsInRange(from) {
  var all = state.meta.months || [];
  if (!from) return all;
  var cut = new Date(from).toISOString().slice(0, 7);
  var keep = all.filter(function (m) { return m >= cut; });
  // Include the month before the window so the line enters from the left edge.
  var i = all.indexOf(keep[0]);
  if (i > 0) keep.unshift(all[i - 1]);
  return keep.length ? keep : all.slice(-1);
}

function load(tier, from) {
  if (tier === "raw") {
    return Promise.all(monthsInRange(from).map(function (m) {
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
    var years = state.meta.years || [];
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
    return {
      rooms: p.rooms, weather: p.weather, boiler: [],
      key: "day", step: 86400000, weatherStep: 86400000,
    };
  });
}

/* ---------- series building ---------- */

/* Insert an explicit null wherever sampling stopped, so ECharts breaks the line.
 * Gaps are never bridged: an outage should look like an outage. */
function withGaps(points, step) {
  var out = [];
  for (var i = 0; i < points.length; i++) {
    if (i && points[i][0] - points[i - 1][0] > step * 2) {
      out.push([points[i - 1][0] + step, null]);
    }
    out.push(points[i]);
  }
  return out;
}

function buildSeries(data, tier) {
  var key = data.key;
  var valueField = tier === "daily" ? "mean_c" : "temp_c";

  var byRoom = {};
  data.rooms.forEach(function (r) {
    (byRoom[r.room_id] = byRoom[r.room_id] || []).push([ms(r[key]), r[valueField]]);
  });

  var humByRoom = {};
  data.rooms.forEach(function (r) {
    if (r.humidity_pct === null || r.humidity_pct === undefined) return;
    (humByRoom[r.room_id] = humByRoom[r.room_id] || []).push([ms(r[key]), r.humidity_pct]);
  });

  var targetsByRoom = {};
  if (tier !== "daily") {
    data.rooms.forEach(function (r) {
      if (r.setpoint_c === null || r.setpoint_c === undefined) return;
      (targetsByRoom[r.room_id] = targetsByRoom[r.room_id] || []).push([ms(r[key]), r.setpoint_c]);
    });
  }

  var outTemp = data.weather.map(function (w) {
    return [ms(w[key]), tier === "daily" ? w.mean_c : w.temp_c];
  });
  var outHum = data.weather.map(function (w) { return [ms(w[key]), w.humidity_pct]; });

  return {
    byRoom: byRoom, humByRoom: humByRoom, targetsByRoom: targetsByRoom,
    outTemp: outTemp, outHum: outHum, boiler: data.boiler,
    step: data.step,
    weatherStep: data.weatherStep || data.step,
  };
}

/* Contiguous stretches where the boiler was firing, drawn as background bands. */
function boilerBands(boiler) {
  var bands = [], start = null;
  for (var i = 0; i < boiler.length; i++) {
    var on = boiler[i].boiler_relay === "On";
    if (on && start === null) start = ms(boiler[i].ts_utc);
    if (!on && start !== null) {
      bands.push([{ xAxis: start }, { xAxis: ms(boiler[i].ts_utc) }]);
      start = null;
    }
  }
  if (start !== null && boiler.length) {
    bands.push([{ xAxis: start }, { xAxis: ms(boiler[boiler.length - 1].ts_utc) }]);
  }
  return bands;
}

function gapBands() {
  return (state.meta.gaps || []).map(function (g) {
    return [{ xAxis: ms(g.from) }, { xAxis: ms(g.to) }];
  });
}

/* ---------- rendering ---------- */

function axisCommon() {
  return {
    axisLine: { lineStyle: { color: css("--baseline") } },
    axisLabel: { color: css("--text-muted"), fontSize: 11 },
    splitLine: { lineStyle: { color: css("--gridline") } },
  };
}

function renderTemp(from, to) {
  var s = state.series;
  var series = [];

  state.rooms.forEach(function (room) {
    var pts = withGaps(byTime(s.byRoom[room.room_id] || []), s.step);
    series.push({
      name: room.name,
      type: "line",
      showSymbol: false,
      connectNulls: false,
      lineStyle: { width: 2, type: room.dashed ? "dashed" : "solid" },
      itemStyle: { color: css(room.colorVar) },
      emphasis: { focus: "series" },
      data: pts,
    });
  });

  if (state.targets) {
    var sel = tempChart ? (tempChart.getOption().legend[0].selected || {}) : {};
    state.rooms.forEach(function (room) {
      if (sel[room.name] === false) return;
      var pts = s.targetsByRoom[room.room_id];
      if (!pts || !pts.length) return;
      series.push({
        name: room.name + " target",
        type: "line",
        step: "end",
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: 1, type: "dotted", opacity: 0.85 },
        itemStyle: { color: css(room.colorVar) },
        data: withGaps(byTime(pts), s.step),
        tooltip: { show: false },
      });
    });
  }

  // Outside is a reference series, not another category: muted ink, dashed.
  series.push({
    name: "Outside",
    type: "line",
    showSymbol: false,
    connectNulls: false,
    lineStyle: { width: 2, type: "dashed" },
    itemStyle: { color: css("--text-muted") },
    z: 1,
    data: withGaps(byTime(s.outTemp), s.weatherStep),
  });

  var marks = [];
  if (state.boiler && s.boiler.length) {
    marks = boilerBands(s.boiler);
  }
  var gaps = gapBands();

  if (series.length) {
    series[0].markArea = {
      silent: true,
      itemStyle: { color: state.boiler ? "rgba(235,104,52,0.10)" : "transparent" },
      data: marks,
    };
    series[series.length - 1].markArea = {
      silent: true,
      itemStyle: { color: css("--gridline"), opacity: 0.55 },
      data: gaps,
    };
  }

  tempChart.setOption({
    animation: false,
    backgroundColor: "transparent",
    grid: { left: 46, right: 14, top: 34, bottom: 64 },
    legend: {
      type: "scroll",
      top: 0,
      textStyle: { color: css("--text-secondary"), fontSize: 11 },
      inactiveColor: css("--text-muted"),
      data: state.rooms.map(function (r) { return r.name; }).concat(["Outside"]),
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", label: { backgroundColor: css("--text-secondary") } },
      backgroundColor: css("--surface-1"),
      borderColor: css("--border"),
      textStyle: { color: css("--text-primary"), fontSize: 12 },
      formatter: function (ps) {
        if (!ps.length) return "";
        var head = fmt(ps[0].value[0], state.tier !== "daily");
        var rows = ps.filter(function (p) { return p.value[1] !== null; })
          .sort(function (a, b) { return b.value[1] - a.value[1]; })
          .map(function (p) {
            return p.marker + p.seriesName + "  <strong>" + p.value[1].toFixed(1) + "°C</strong>";
          });
        return head + "<br>" + rows.join("<br>");
      },
    },
    xAxis: Object.assign({ type: "time", min: from, max: to }, axisCommon()),
    yAxis: Object.assign({ type: "value", scale: true, name: "°C", nameTextStyle: { color: css("--text-muted") } }, axisCommon()),
    dataZoom: [
      { type: "inside", filterMode: "none" },
      { type: "slider", height: 26, bottom: 12, filterMode: "none",
        borderColor: css("--border"), fillerColor: "rgba(42,120,214,0.14)",
        textStyle: { color: css("--text-muted"), fontSize: 10 } },
    ],
    series: series,
  }, true);
}

function byTime(pts) {
  return pts.slice().sort(function (a, b) { return a[0] - b[0]; });
}

function renderHumidity(from, to) {
  var s = state.series;
  var series = [];

  state.rooms.forEach(function (room) {
    var pts = s.humByRoom[room.room_id];
    if (!pts || !pts.length) return;
    series.push({
      name: room.name,
      type: "line",
      showSymbol: false,
      connectNulls: false,
      lineStyle: { width: 2, type: room.dashed ? "dashed" : "solid" },
      itemStyle: { color: css(room.colorVar) },
      data: withGaps(byTime(pts), s.step),
    });
  });

  series.push({
    name: "Outside",
    type: "line",
    showSymbol: false,
    connectNulls: false,
    lineStyle: { width: 2, type: "dashed" },
    itemStyle: { color: css("--text-muted") },
    data: withGaps(byTime(s.outHum), s.weatherStep),
  });

  document.getElementById("panel-humidity").hidden = series.length === 0;

  humChart.setOption({
    animation: false,
    backgroundColor: "transparent",
    grid: { left: 46, right: 14, top: 28, bottom: 28 },
    legend: { top: 0, textStyle: { color: css("--text-secondary"), fontSize: 11 }, inactiveColor: css("--text-muted") },
    tooltip: {
      trigger: "axis",
      backgroundColor: css("--surface-1"),
      borderColor: css("--border"),
      textStyle: { color: css("--text-primary"), fontSize: 12 },
      formatter: function (ps) {
        if (!ps.length) return "";
        var rows = ps.filter(function (p) { return p.value[1] !== null; }).map(function (p) {
          return p.marker + p.seriesName + "  <strong>" + Math.round(p.value[1]) + "%</strong>";
        });
        return fmt(ps[0].value[0], state.tier !== "daily") + "<br>" + rows.join("<br>");
      },
    },
    xAxis: Object.assign({ type: "time", min: from, max: to }, axisCommon()),
    yAxis: Object.assign({ type: "value", min: 0, max: 100, name: "%", nameTextStyle: { color: css("--text-muted") } }, axisCommon()),
    dataZoom: [{ type: "inside", filterMode: "none" }],
    series: series,
  }, true);
}

/* The table is the relief for three light-mode hues that fall below 3:1 against
 * the surface - identity must never rest on colour alone. */
function renderTable() {
  var s = state.series;
  var head = document.querySelector("#table thead");
  var body = document.querySelector("#table tbody");
  head.innerHTML = "<tr><th>Room</th><th>Latest</th><th>Min</th><th>Max</th><th>Humidity</th></tr>";

  var rows = state.rooms.map(function (room) {
    var pts = (s.byRoom[room.room_id] || []).filter(function (p) { return p[1] !== null; });
    if (!pts.length) return "";
    var vals = pts.map(function (p) { return p[1]; });
    var hum = s.humByRoom[room.room_id];
    return "<tr><td><span class='swatch' style='background:" + css(room.colorVar) + "'></span>" +
      room.name + "</td><td>" + vals[vals.length - 1].toFixed(1) + "°C</td><td>" +
      Math.min.apply(null, vals).toFixed(1) + "°C</td><td>" +
      Math.max.apply(null, vals).toFixed(1) + "°C</td><td>" +
      (hum && hum.length ? Math.round(hum[hum.length - 1][1]) + "%" : "—") + "</td></tr>";
  });
  body.innerHTML = rows.join("");
}

/* ---------- freshness ---------- */

function renderFreshness() {
  var el = document.getElementById("freshness");
  var last = state.meta.last_success_utc;
  if (!last) {
    el.textContent = "No successful collection recorded";
    el.className = "freshness stale";
    return;
  }
  var mins = Math.round((Date.now() - ms(last)) / 60000);
  var stale = mins > 30;
  el.className = "freshness " + (stale ? "stale" : "ok");
  var cov = state.meta.coverage || {};
  el.textContent = (mins < 1 ? "Updated just now" : "Updated " + mins + " min ago") +
    (cov.first ? " · history from " + fmt(ms(cov.first), false) : "") +
    (stale ? " — collection may have stopped" : "");
}

/* ---------- orchestration ---------- */

function refresh() {
  var tier = pickTier(state.hours);
  var to = Date.now();
  var from = state.hours ? to - state.hours * 3600000 : null;

  document.getElementById("tier-note").textContent = tierLabel(tier);

  return load(tier, from).then(function (data) {
    state.tier = tier;
    state.series = buildSeries(data, tier);

    var cov = state.meta.coverage || {};
    var lo = from !== null ? from : (cov.first ? ms(cov.first) : to - 86400000);
    renderTemp(lo, to);
    renderHumidity(lo, to);
    renderTable();
  });
}

function bind() {
  document.getElementById("ranges").addEventListener("click", function (e) {
    var btn = e.target.closest("button");
    if (!btn) return;
    [].forEach.call(this.querySelectorAll("button"), function (b) { b.classList.remove("on"); });
    btn.classList.add("on");
    state.hours = Number(btn.dataset.hours);
    refresh().catch(function (err) { showError(err.message); });
  });

  document.getElementById("opt-targets").addEventListener("change", function () {
    state.targets = this.checked;
    renderTemp.apply(null, currentWindow());
  });

  document.getElementById("opt-boiler").addEventListener("change", function () {
    state.boiler = this.checked;
    renderTemp.apply(null, currentWindow());
  });

  document.getElementById("opt-table").addEventListener("change", function () {
    document.getElementById("panel-table").hidden = !this.checked;
  });

  document.getElementById("theme-toggle").addEventListener("click", function () {
    var dark = document.documentElement.getAttribute("data-theme") === "dark";
    document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
    try { localStorage.setItem("wiser-theme", dark ? "light" : "dark"); } catch (e) {}
    // Colours come from CSS variables, so a repaint has to re-read them.
    var w = currentWindow();
    renderTemp(w[0], w[1]);
    renderHumidity(w[0], w[1]);
    renderTable();
  });

  window.addEventListener("resize", function () {
    tempChart.resize();
    humChart.resize();
  });

  // The collector writes every 5 minutes; picking data up on the same cadence
  // keeps the freshness indicator honest without hammering the server.
  setInterval(function () {
    getJSON("meta.json").then(function (m) {
      state.meta = m;
      renderFreshness();
    }).catch(function () {});
  }, 300000);
}

function currentWindow() {
  var to = Date.now();
  var cov = state.meta.coverage || {};
  var from = state.hours ? to - state.hours * 3600000 : (cov.first ? ms(cov.first) : to - 86400000);
  return [from, to];
}

function init() {
  try {
    var saved = localStorage.getItem("wiser-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch (e) {}

  tempChart = echarts.init(document.getElementById("chart-temp"), null, { renderer: "canvas" });
  humChart = echarts.init(document.getElementById("chart-hum"), null, { renderer: "canvas" });

  getJSON("meta.json").then(function (meta) {
    state.meta = meta;
    state.rooms = assignColours(meta.rooms || []);
    renderFreshness();
    bind();
    return refresh();
  }).catch(function (err) {
    showError("Could not load data: " + err.message);
  });
}

init();
