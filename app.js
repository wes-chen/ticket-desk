/* Sharks Ticket Desk — read-only dashboard.
   Renders from data/outcomes.json. No writes, no storage, no forms. */
(function () {
  "use strict";

  var STATUS_LABEL = {
    listed: "Listed", sold: "Sold", attending: "Going",
    exchanged: "Exchanged", undecided: "To decide"
  };
  var STATUS_CLASS = {
    listed: "listed", sold: "sold", attending: "attending",
    exchanged: "exchanged", undecided: ""
  };
  var TIER_CODE = { PRESEASON: "PRE", "A+": "A+", A: "A", B: "B", C: "C", D: "D" };
  var TIER_CREDIT = { PRESEASON: 51, "A+": 120, A: 109, B: 100, C: 83, D: 69 };

  function $(sel, el) { return (el || document).querySelector(sel); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtMoney(v) {
    return v == null ? "—" : "$" + Math.round(v);
  }
  function fmtDate(iso) {
    var d = new Date(iso.length <= 10 ? iso + "T12:00:00" : iso);
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }
  function median(vals) {
    if (!vals.length) return null;
    var s = vals.slice().sort(function (a, b) { return a - b; });
    var m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  var state = { data: null, selected: null, timer: null };

  function nextGame(games) {
    var now = Date.now();
    var best = null;
    games.forEach(function (g) {
      var t = Date.parse(g.puckTime || g.date);
      if (isNaN(t) || t < now) return;
      if (!best || t < Date.parse(best.puckTime || best.date)) best = g;
    });
    return best;
  }

  function renderHero(g) {
    if (!g) {
      return '<section class="hero"><div><p class="kick">2026-27 SEASON</p>' +
        '<h2>Season complete</h2><p class="meta">All 44 home games are in the books.</p></div></section>';
    }
    var pill = g.status !== "undecided"
      ? '<span class="pill ' + STATUS_CLASS[g.status] + '">' + esc(STATUS_LABEL[g.status]) + '</span>' : "";
    return '<section class="hero">' +
      '<img src="' + esc(g.logo) + '" alt="' + esc(g.opponent) + ' logo" width="64" height="64">' +
      '<div><p class="kick">NEXT UP</p>' +
      '<h2>vs ' + esc(g.opponent) + '</h2>' +
      '<p class="meta">' + esc(fmtDate(g.puckTime || g.date)) + ' · ' + esc(g.tier) + ' tier</p>' +
      '<div class="cd" id="cd"><div><b data-cd="d">–</b><span>DAYS</span></div>' +
      '<div><b data-cd="h">–</b><span>HRS</span></div>' +
      '<div><b data-cd="m">–</b><span>MIN</span></div>' +
      '<div><b data-cd="s">–</b><span>SEC</span></div></div>' + pill +
      '</div></section>';
  }

  function startCountdown(puckTime) {
    var t = Date.parse(puckTime);
    if (isNaN(t)) return;
    function tick() {
      var ms = t - Date.now();
      if (ms < 0) ms = 0;
      var d = Math.floor(ms / 86400000),
          h = Math.floor(ms / 3600000) % 24,
          m = Math.floor(ms / 60000) % 60,
          s = Math.floor(ms / 1000) % 60;
      var root = $("#cd");
      if (!root) return;
      root.querySelector('[data-cd="d"]').textContent = d;
      root.querySelector('[data-cd="h"]').textContent = h;
      root.querySelector('[data-cd="m"]').textContent = m;
      root.querySelector('[data-cd="s"]').textContent = s;
    }
    tick();
    state.timer = setInterval(tick, 1000);
  }

  function renderMixCard(games) {
    var counts = { listed: 0, sold: 0, attending: 0, exchanged: 0, undecided: 0 };
    games.forEach(function (g) { counts[g.status] = (counts[g.status] || 0) + 1; });
    var decided = games.length - counts.undecided;
    var C = 2 * Math.PI * 40, off = 0, arcs = "";
    [["listed", "#2dd4bf"], ["sold", "#4ade80"], ["attending", "#60a5fa"], ["exchanged", "#fbbf24"]]
      .forEach(function (pair) {
        var n = counts[pair[0]];
        if (!n) return;
        var len = (n / games.length) * C;
        arcs += '<circle class="arc" cx="48" cy="48" r="40" fill="none" stroke="' + pair[1] +
          '" stroke-width="11" stroke-dasharray="' + len.toFixed(1) + " " + C.toFixed(1) +
          '" stroke-dashoffset="' + (-off).toFixed(1) + '"/>';
        off += len;
      });
    var leg = ["listed", "sold", "attending", "exchanged", "undecided"].map(function (s) {
      var color = { listed: "#2dd4bf", sold: "#4ade80", attending: "#60a5fa",
                    exchanged: "#fbbf24", undecided: "#5f7a83" }[s];
      return '<div><i style="background:' + color + '"></i>' + STATUS_LABEL[s] + ' · ' + counts[s] + "</div>";
    }).join("");
    return '<section class="card"><h3>SEASON MIX</h3><div class="ringwrap">' +
      '<div class="ringc"><svg class="ring" width="96" height="96" viewBox="0 0 96 96">' +
      '<circle class="trk" cx="48" cy="48" r="40" fill="none" stroke-width="11"/>' + arcs + "</svg>" +
      '<div class="ctr"><b>' + decided + "</b><span>SET</span></div></div>" +
      '<div class="rleg">' + leg + "</div></div></section>";
  }
  function renderPulseShell() {
    return '<section class="card" id="pulsecard"><h3>MARKET PULSE</h3><div class="bigstat" id="pulse"></div></section>';
  }

  function tierMedians(games) {
    var tiers = {}, order = [];
    games.forEach(function (g) {
      if (g.marketMedian == null) return;
      if (!tiers[g.tier]) { tiers[g.tier] = []; order.push(g.tier); }
      tiers[g.tier].push(g.marketMedian);
    });
    var out = {};
    order.forEach(function (t) { out[t] = median(tiers[t]); });
    return out;
  }

  function renderPulse(g) {
    var el = $("#pulse");
    if (!el) return;
    var med = g.marketMedian, n = g.marketCount;
    var tm = state.tierMeds ? state.tierMeds[g.tier] : null;
    var tcode = TIER_CODE[g.tier] || g.tier;
    var tierRow;
    if (tm == null || med == null) {
      tierRow = '<div class="row"><span class="lab">TIER</span><span>' + esc(tcode) + "</span></div>";
    } else {
      var d = Math.round(med - tm);
      var relCls = d === 0 ? "" : (d > 0 ? "up" : "down");
      var relTxt = d === 0 ? "at avg"
        : (d > 0 ? "+" : "-") + "$" + Math.abs(d);
      tierRow = '<div class="row"><span class="lab">TIER</span><span>' + esc(tcode) +
        " · avg " + fmtMoney(tm) + " · " +
        (relCls ? '<span class="' + relCls + '">' + relTxt + "</span>" : relTxt) + "</span></div>";
    }
    var rangeTxt = (g.marketMin != null && g.marketMax != null)
      ? " · range " + fmtMoney(g.marketMin) + "–" + fmtMoney(g.marketMax) : "";
    var pairsTxt = n === 1 ? "1 pair" : n + " pairs";
    el.innerHTML =
      '<div class="gt">' + esc(g.abbrev) + " · " + esc(fmtDate(g.date)) + "</div>" +
      "<b>" + fmtMoney(med) + "</b>" +
      "<span>median" + rangeTxt + " · " + pairsTxt + "</span>" +
      tierRow;
    var credit = TIER_CREDIT[g.tier];
    var cheapest = g.marketMin;
    if (credit != null && cheapest != null) {
      var net = cheapest * 0.9;
      var gap = net - credit;
      var verdict = gap >= 0
        ? "Resale wins " + fmtMoney(gap) + "/seat"
        : "Exchange wins " + fmtMoney(-gap) + "/seat";
      el.innerHTML += '<div class="banner"><div class="v">' + verdict + "</div>" +
        '<div class="m">nets ' + fmtMoney(net) + " vs " + fmtMoney(credit) +
        " credit · cheapest " + fmtMoney(cheapest) + "</div></div>";
    }
  }

  function renderTimeline(games) {
    var byMonth = {}, order = [];
    games.forEach(function (g) {
      var m = (g.date || "").slice(0, 7);
      if (!byMonth[m]) { byMonth[m] = []; order.push(m); }
      byMonth[m].push(g);
    });
    var monthName = function (ym) {
      return new Date(ym + "-02T12:00:00").toLocaleDateString(undefined, { month: "short" }).toUpperCase();
    };
    var rows = order.map(function (m) {
      var dots = byMonth[m].map(function (g) {
        var day = parseInt((g.date || "").slice(8, 10), 10);
        var sel = state.selected === g.gameId ? " sel" : "";
        var tc = TIER_CODE[g.tier] || g.tier;
        return '<span class="tcell"><button class="dot ' + STATUS_CLASS[g.status] + sel + '"' +
          ' data-game="' + g.gameId + '"' +
          ' aria-label="' + esc(g.opponent) + " " + esc(g.date) + " — " + esc(g.tier) +
          " tier — " + esc(STATUS_LABEL[g.status]) + '">' +
          "<span>" + day + "</span></button>" +
          '<span class="tcode">' + esc(tc) + "</span></span>";
      }).join("");
      var n = byMonth[m].length, rws = Math.ceil(n / 5), cls = Math.ceil(n / rws);
      return '<div class="mrow"><div class="mlab">' + monthName(m) + '</div><div class="dots" style="--cols:' + cls + '">' + dots + "</div></div>";
    }).join("");
    return '<section class="card"><h3>SEASON TIMELINE</h3><div class="tgrid">' + rows + "</div>" +
      '<div class="tlegend">Code under each date = Sharks pricing tier</div>' +
      '<div class="gdetail" id="gdetail"></div></section>';
  }

  function renderDetail(g) {
    var el = $("#gdetail");
    if (!el) return;
    var statusLine = g.status === "undecided" ? "No plans yet" : STATUS_LABEL[g.status];
    el.innerHTML =
      '<img src="' + esc(g.logo) + '" alt="" width="40" height="40">' +
      '<div><div class="t1">vs ' + esc(g.opponent) + "</div>" +
      '<div class="t2">' + esc(fmtDate(g.puckTime || g.date)) + " · " + esc(g.tier) + " · " + esc(statusLine) +
      " · Market " + fmtMoney(g.marketMedian) + "</div></div>";
  }

  function renderTiers(games) {
    var tm = tierMedians(games);
    var meds = Object.keys(tm).map(function (t) { return [t, tm[t]]; })
      .filter(function (x) { return x[1] != null; });
    meds.sort(function (a, b) { return b[1] - a[1]; });
    var max = Math.max.apply(null, meds.map(function (x) { return x[1]; }).concat([1]));
    var H = 110;
    var cols = meds.map(function (x) {
      var h = Math.max(6, Math.round((x[1] / max) * H));
      var tc = TIER_CODE[x[0]] || x[0];
      return '<div class="vcol"><span class="vv">' + fmtMoney(x[1]) + "</span>" +
        '<span class="vbar" style="height:' + h + 'px"></span>' +
        '<span class="vt">' + esc(tc) + "</span></div>";
    }).join("");
    if (!cols) cols = '<p class="sub2" style="color:var(--mut);font-size:12px">No market data yet.</p>';
    return '<section class="card"><h3>MARKET BY TIER</h3><div class="vbars">' + cols + "</div>" +
      '<div class="fnote">Median comparable resale list per tier, per seat.</div></section>';
  }

  function renderEconomics() {
    var cards = [
      ["resale_net = list × 0.90", "A sale nets 90% of the list price after the Ticketmaster seller fee."],
      ["break_even = tier_credit / 0.90", "The list price at which a sale exactly matches the exchange credit."],
      ["buyer_total = list × 1.165", "Resale buyers pay a flat 16.5% fee on top of the list price."]
    ].map(function (c) {
      return '<div class="f"><code>' + esc(c[0]) + "</code><p>" + esc(c[1]) + "</p></div>";
    }).join("");
    return '<section class="card"><h3>THE ECONOMICS</h3><div class="snap">' + cards + "</div></section>";
  }

  function renderCheat() {
    var tiers = ["A+", "A", "B", "C", "D", "PRESEASON"];
    var rows = tiers.map(function (t) {
      var c = TIER_CREDIT[t], be = c / 0.9, bt = be * 1.165;
      return "<tr><td class=\"tc\">" + t + "</td><td>$" + c + "</td><td>$" + be.toFixed(2) +
        "</td><td>$" + bt.toFixed(2) + "</td></tr>";
    }).join("");
    return '<section class="card"><h3>LISTING CHEAT SHEET</h3>' +
      '<table class="ctable"><thead><tr><th>TIER</th><th>CREDIT</th><th>BREAK-EVEN</th><th>BUYER PAYS</th></tr></thead>' +
      "<tbody>" + rows + "</tbody></table>" +
      '<p class="fnote">Per seat. Break-even = credit ÷ 0.90 · buyer total = list × 1.165</p></section>';
  }

  function fmtDur(ms) {
    if (ms < 0) return "passed";
    var m = Math.floor(ms / 60000), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60);
    if (d > 0) return d + "d " + h + "h";
    if (h > 0) return h + "h " + (m % 60) + "m";
    return m + "m";
  }

  function renderQueue() {
    var now = Date.now(), items = [];
    state.data.games.forEach(function (g) {
      var puck = Date.parse(g.puckTime || g.date);
      if (isNaN(puck) || puck <= now) return;
      var st = g.status || "undecided";
      if (st === "sold" || st === "attending" || st === "exchanged") return;
      var cutoff = puck - 48 * 3600 * 1000;
      if (cutoff - now > 14 * 24 * 3600 * 1000) return;
      items.push({ g: g, cutoff: cutoff, listed: st === "listed" });
    });
    items.sort(function (a, b) { return a.cutoff - b.cutoff; });
    var body;
    if (!items.length) {
      body = '<p class="qempty">Nothing needs action — every upcoming game is decided or outside the 14-day window.</p>';
    } else {
      body = items.slice(0, 8).map(function (it) {
        var g = it.g, urgent = (it.cutoff - now) < 72 * 3600 * 1000;
        var tag = it.listed ? "Listed" : "No plan yet";
        return '<div class="qrow' + (urgent ? " urgent" : "") + '">' +
          '<div class="qmain"><b>' + esc(g.team) + "</b><span> · " + esc(fmtDate(g.puckTime || g.date)) + "</span></div>" +
          '<div class="qcut">cutoff ' + fmtDur(it.cutoff - now) + "</div>" +
          '<div class="qsub">' + tag + " · " + esc(g.tier) + " tier</div></div>";
      }).join("");
      if (items.length > 8) body += '<p class="fnote">+' + (items.length - 8) + " more in the daily digest</p>";
    }
    return '<section class="card"><h3>ACTION QUEUE</h3>' + body + "</section>";
  }

  function renderFooter(genAt) {
    var d = genAt ? new Date(genAt) : null;
    var when = d && !isNaN(d) ? d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
    }) : "—";
    return '<div class="ft">Data refreshes daily · Updated ' + esc(when) + "</div>";
  }

  function select(gameId) {
    state.selected = gameId;
    var g = null;
    state.data.games.forEach(function (x) { if (x.gameId === gameId) g = x; });
    if (!g) return;
    renderPulse(g);
    renderDetail(g);
    var dots = document.querySelectorAll(".dot");
    for (var i = 0; i < dots.length; i++) {
      dots[i].classList.toggle("sel", dots[i].getAttribute("data-game") == String(gameId));
    }
  }

  function bindTimeline() {
    var buttons = document.querySelectorAll(".dot");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function () {
        select(parseInt(this.getAttribute("data-game"), 10));
      });
    }
  }

  function render(data) {
    state.data = data;
    var games = data.games.slice().sort(function (a, b) {
      return (a.date || "").localeCompare(b.date || "");
    });
    var next = nextGame(games);
    state.selected = (next || games[0]).gameId;
    state.tierMeds = tierMedians(games);
    var app = $("#app");
    app.innerHTML =
      renderHero(next) +
      '<div class="grid2">' + renderQueue() + renderPulseShell() + "</div>" +
      renderTimeline(games) +
      '<div class="grid2x">' + renderTiers(games) + renderMixCard(games) + "</div>" +
      '<div class="grid2x">' + renderCheat() + renderEconomics() + "</div>" +
      renderFooter(data.generated_at);
    var sel = null;
    games.forEach(function (g) { if (g.gameId === state.selected) sel = g; });
    if (sel) { renderPulse(sel); renderDetail(sel); }
    if (next) startCountdown(next.puckTime || next.date);
    bindTimeline();
    snapStripToSelected();
    if (window.addEventListener) window.addEventListener("load", snapStripToSelected);
  }

  function snapStripToSelected() {
    if (!window.matchMedia || !window.matchMedia("(min-width:1024px)").matches) return;
    var sdot = document.querySelector(".tgrid .dot.sel"), tgrid = document.querySelector(".tgrid");
    if (!sdot || !tgrid) return;
    var r = sdot.getBoundingClientRect(), gr = tgrid.getBoundingClientRect();
    var target = tgrid.scrollLeft + (r.left - gr.left) - gr.width / 2 + r.width / 2;
    tgrid.scrollLeft = Math.max(0, Math.min(tgrid.scrollWidth - tgrid.clientWidth, target));
  }

  function fail(msg) {
    var app = $("#app");
    if (app) app.innerHTML = '<div class="err">' + esc(msg) + "</div>";
  }

  fetch("data/outcomes.json", { cache: "no-store" })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      if (!data || !Array.isArray(data.games) || !data.games.length) throw new Error("bad data");
      render(data);
    })
    .catch(function () { fail("Could not load season data. Check back shortly."); });
})();
