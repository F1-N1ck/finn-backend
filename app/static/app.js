// FINN front end. Plain JavaScript, no libraries, no external requests.
// All text from the server is inserted with textContent (never innerHTML).
//
// Backend behaviour each part relies on (see FRONTEND_REDESIGN.md):
//   Portfolio  GET /api/portfolio/demo (starting list), GET /api/symbols/check, GET /api/symbols/search
//   Today      POST /api/digest {symbols}, POST /api/filings/refresh {symbols}, GET /api/filings/status, POST /api/prices/refresh {symbols}
//              (the selection lives in this browser; the server only reads the ticker list in each request and stores nothing)
//   Research   POST /api/analyze, GET /api/analyze/{id}, GET /api/questions, POST /api/research (saved answers only)
//   Guidance   GET /api/guidance

var DEFAULT_FILTERS = { hours: 24, vol: 2, move: 3, routine: false };

var state = {
  digest: null,
  digestFailed: false,
  liveRows: null,
  liveFetchedUtc: null,
  priceError: null,
  pricesBusy: false,
  priceFail: {},
  priceLoading: {},
  filingsPending: {},
  autoChecked: {},
  autoPrice: {},
  digestSerial: 0,
  digestLoading: false,
  portfolio: null,
  portfolioError: null,
  demoList: null,
  undo: null,
  filters: loadFilters(),
  focusFiling: null,
  contextNote: null,
  filingsPolling: false,
  autoRefreshed: false,
  guidanceLoaded: false,
  questions: null,
  research: { token: 0, symbol: null, running: false, startedAt: 0, jobId: null, timer: null, job: null }
};

// ------------------------------------------------------------------ helpers

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined && text !== null) { node.textContent = text; }
  return node;
}

function add(parent) {
  for (var i = 1; i < arguments.length; i++) {
    if (arguments[i]) { parent.appendChild(arguments[i]); }
  }
  return parent;
}

function clear(node) {
  while (node.firstChild) { node.removeChild(node.firstChild); }
}

function byId(id) { return document.getElementById(id); }

function storeGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}

function storeSet(key, value) {
  try { window.localStorage.setItem(key, value); return true; } catch (e) { return false; }
}

function storeRemove(key) {
  try { window.localStorage.removeItem(key); return true; } catch (e) { return false; }
}

function loadFilters() {
  var filters = { hours: DEFAULT_FILTERS.hours, vol: DEFAULT_FILTERS.vol, move: DEFAULT_FILTERS.move, routine: DEFAULT_FILTERS.routine };
  var raw = storeGet("finn.filters");
  if (!raw) { return filters; }
  try {
    var saved = JSON.parse(raw);
    if ([24, 72, 168].indexOf(saved.hours) >= 0) { filters.hours = saved.hours; }
    if (typeof saved.vol === "number" && saved.vol >= 1 && saved.vol <= 10) { filters.vol = saved.vol; }
    if (typeof saved.move === "number" && saved.move >= 1 && saved.move <= 20) { filters.move = saved.move; }
    filters.routine = saved.routine === true;
  } catch (e) { /* keep defaults */ }
  return filters;
}

function saveFilters() {
  storeSet("finn.filters", JSON.stringify(state.filters));
}

// Every network call has a timeout and a few quick retries. The 3 Nov measurements showed waits of 60 s and
// 120 s that were connection set-up timeouts, not slow answers (RESEARCH_NOTES.md, round 10). The submit and
// refresh endpoints are safe to repeat: the server returns the job that is already running.
function fetchJson(url, options, timeoutMs, tries) {
  var attempt = 0;
  function once() {
    attempt = attempt + 1;
    var controller = new AbortController();
    var timer = window.setTimeout(function () { controller.abort(); }, timeoutMs);
    var opts = Object.assign({}, options || {}, { signal: controller.signal });
    return fetch(url, opts)
      .then(function (response) {
        window.clearTimeout(timer);
        return response.json().then(function (body) {
          return { ok: response.ok, status: response.status, body: body };
        }, function () {
          return { ok: response.ok, status: response.status, body: {} };
        });
      })
      .catch(function (error) {
        window.clearTimeout(timer);
        if (attempt < tries) {
          return new Promise(function (resolve) { window.setTimeout(resolve, 1000); }).then(once);
        }
        throw error;
      });
  }
  return once();
}

var IST = "Asia/Kolkata";

function formatNumber(value, digits) {
  if (value === null || value === undefined) { return "–"; }
  return Number(value).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function compact(value) {
  if (value === null || value === undefined) { return "–"; }
  if (value >= 10000000) { return (value / 10000000).toFixed(2) + " Cr"; }
  if (value >= 100000) { return (value / 100000).toFixed(2) + " L"; }
  return formatNumber(value, 0);
}

function fmtTime(iso) {
  if (!iso) { return "unknown time"; }
  var date = new Date(iso);
  if (isNaN(date.getTime())) { return String(iso); }
  return date.toLocaleString("en-IN", { timeZone: IST, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) + " IST";
}

function fmtDay(iso) {
  var date = new Date(iso);
  if (isNaN(date.getTime())) { return String(iso); }
  return date.toLocaleDateString("en-IN", { timeZone: IST, day: "numeric", month: "short", year: "numeric" });
}

function fmtBarDate(text) {
  if (!text) { return "unknown date"; }
  var date = new Date(text + "T12:00:00+05:30");
  if (isNaN(date.getTime())) { return text; }
  return date.toLocaleDateString("en-IN", { timeZone: IST, day: "numeric", month: "short" });
}

function signed(value, digits) {
  if (value === null || value === undefined) { return "–"; }
  var sign = value > 0 ? "+" : (value < 0 ? "−" : "");
  return sign + Math.abs(value).toFixed(digits);
}

function arrow(value) {
  if (value === null || value === undefined) { return ""; }
  return value > 0 ? "▲ " : (value < 0 ? "▼ " : "");
}

function safeLink(url, text, className) {
  if (typeof url === "string" && url.indexOf("https://") === 0) {
    var link = el("a", className || "linkbtn", text);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }
  return null;
}

function button(text, className, onClick, label) {
  var node = el("button", className, text);
  node.type = "button";
  if (label) { node.setAttribute("aria-label", label); }
  if (onClick) { node.addEventListener("click", onClick); }
  return node;
}

function chevron() {
  var node = el("span", "chev", "›");
  node.setAttribute("aria-hidden", "true");
  return node;
}

// ------------------------------------------------------------------ views and sheet

function showView(name) {
  var views = ["today", "portfolio", "research", "profile"];
  for (var i = 0; i < views.length; i++) {
    var on = views[i] === name;
    byId("view-" + views[i]).hidden = !on;
    var nav = byId("nav-" + views[i]);
    nav.classList.toggle("on", on);
    if (on) { nav.setAttribute("aria-current", "page"); } else { nav.removeAttribute("aria-current"); }
  }
  window.scrollTo(0, 0);
  var heading = document.querySelector("#view-" + name + " h1");
  if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus({ preventScroll: true }); }  // screen readers hear the new page title
  if (name === "research") { initResearchView(); }
  if (name === "profile") { renderProfile(); }
  if (name === "portfolio") { renderPortfolio(); }
}

function openSheet(title, build) {
  var dialog = byId("sheet");
  byId("sheet-title").textContent = title;
  var body = byId("sheet-body");
  clear(body);
  build(body);
  if (!dialog.open) {
    if (dialog.showModal) { dialog.showModal(); } else { dialog.setAttribute("open", ""); }
  }
}

function closeSheet() {
  var dialog = byId("sheet");
  if (dialog.close) { dialog.close(); } else { dialog.removeAttribute("open"); }
}

// ------------------------------------------------------------------ digest data

function currentStats(entry) {
  if (state.liveRows) {
    for (var i = 0; i < state.liveRows.length; i++) {
      var row = state.liveRows[i];
      if (row.symbol === entry.symbol && row.ok) { return row; }
    }
  }
  return entry;
}

function loadDigest() {
  if (!state.portfolio) { return Promise.resolve(); }
  var symbols = currentSymbols();
  if (symbols.length === 0) {
    state.digest = null;
    state.digestFailed = false;
    state.digestLoading = false;
    state.digestSerial = state.digestSerial + 1;
    renderToday();
    return Promise.resolve();
  }
  var serial = state.digestSerial + 1;
  state.digestSerial = serial;
  state.digestLoading = true;
  if (!state.digest) {
    // The free server sleeps when idle and can take up to a minute to wake: say so rather than leave a silent skeleton.
    window.setTimeout(function () {
      if (serial === state.digestSerial && state.digestLoading && !state.digest) {
        var hint = document.querySelector("#freshness .hint");
        if (hint) { hint.textContent = "Still loading. The free server may be waking up; this can take up to a minute. Nothing is wrong yet."; }
        var skeleton = document.querySelector("#priority .skeleton");
        if (skeleton) { skeleton.textContent = "Still loading filings…"; }
      }
    }, 7000);
  }
  return fetchJson("/api/digest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbols: symbols }) }, 15000, 3)
    .then(function (result) {
      if (serial !== state.digestSerial) { return; }  // the selection changed while this was loading
      if (!result.ok) { throw new Error("HTTP " + result.status); }
      state.digest = result.body;
      state.digestFailed = false;
      state.digestLoading = false;
      renderToday();
      renderPortfolio();
      var fs = state.digest.filings_status;
      if (fs && fs.running) { pollFilings(); }
      autoCover();
    })
    .catch(function (error) {
      if (serial !== state.digestSerial) { return; }
      state.digestLoading = false;
      if (state.digest) {
        state.digestFailed = true;
        renderFreshness();
        renderPortfolio();
        return;
      }
      renderDigestFailure(error.message);
      renderPortfolio();
    });
}

function renderDigestFailure(message) {
  var box = byId("freshness");
  clear(box);
  var row = el("div", "fresh-row");
  add(row, el("span", "g st-failed", "▲"));
  var text = el("span", "t");
  add(text, el("b", null, "Could not load the digest. "), el("span", null, "(" + message + ") "));
  add(row, text, button("Retry", "linkbtn", function () { loadDigest(); }));
  box.appendChild(row);
  ["priority", "others", "signals", "watchlist"].forEach(function (id) {
    var node = byId(id);
    clear(node);
    node.appendChild(el("div", "empty", "Nothing to show until the digest loads."));
  });
}

function windowEnd() {
  return state.digest.as_of_utc ? new Date(state.digest.as_of_utc).getTime() : Date.now();
}

function filingsInWindow() {
  var cutoff = windowEnd() - state.filters.hours * 3600 * 1000;
  var seen = {};
  var result = [];
  for (var i = 0; i < state.digest.filings.length; i++) {
    var filing = state.digest.filings[i];
    if (new Date(filing.at).getTime() < cutoff) { continue; }
    var key = filing.symbol + "|" + filing.at + "|" + filing.desc + "|" + filing.text;
    if (seen[key]) { continue; }  // the same filing never appears twice in the feed
    seen[key] = true;
    result.push(filing);
  }
  return result;
}

// ------------------------------------------------------------------ freshness (filings and prices are separate)

function ageText(iso) {
  // "3 days old" / "5 hours old": how stale a saved copy is, so a date alone is not left to be worked out.
  var t = new Date(iso).getTime();
  if (!iso || isNaN(t)) { return ""; }
  var hours = Math.floor((Date.now() - t) / 3600000);
  if (hours < 1) { return "under an hour old"; }
  if (hours < 36) { return hours + (hours === 1 ? " hour old" : " hours old"); }
  var days = Math.round(hours / 24);
  return days + " days old";
}

function dated(iso) {
  var age = ageText(iso);
  return fmtTime(iso) + (age ? " (" + age + ")" : "");
}

function filingsFreshness() {
  var fs = state.digest.filings_status;
  var snap = fmtTime(fs.snapshot_fetched_utc);
  var run = fs.last_run;
  var unchecked = fs.symbol_counts && fs.symbol_counts.unchecked ? fs.symbol_counts.unchecked : 0;
  var tail = unchecked ? " · " + unchecked + " not checked yet" : "";
  var out = { cls: "st-snap", glyph: "○", title: "Filings", detail: "saved copy from " + dated(fs.snapshot_fetched_utc) + ", not live" + tail };
  if (fs.mode === "unchecked") {
    out = { cls: "st-snap", glyph: "○", title: "Filings, not checked yet", detail: fs.running ? "checking NSE now; FINN has no saved copy for these companies" : "FINN has no saved copy for these companies until a live NSE check runs" };
  } else if (fs.mode === "live") {
    out = { cls: "st-live", glyph: "●", title: "Filings, live from NSE", detail: "updated " + fmtTime(fs.last_success_utc) + " · " + run.ok + " of " + run.total + " companies" + tail };
  } else if (fs.mode === "partial") {
    var failed = run.failed_symbols.slice(0, 3).join(", ") + (run.failed_symbols.length > 3 ? " +" + (run.failed_symbols.length - 3) : "");
    out = { cls: "st-partial", glyph: "◆", title: "Filings, partly updated", detail: "updated " + fmtTime(fs.last_success_utc) + " · " + run.ok + " of " + run.total + (failed ? " · failed: " + failed : "") + tail };
  } else if (fs.mode === "refresh_failed") {
    out = { cls: "st-failed", glyph: "▲", title: "Filings, refresh failed", detail: "tried " + fmtTime(fs.last_attempt_utc) + "; showing " + (fs.last_success_utc ? "the update from " + fmtTime(fs.last_success_utc) : (fs.snapshot_fetched_utc ? "the saved copy from " + snap : "nothing, as there is no saved copy")) + tail };
  }
  return out;
}

function liveRowsForPortfolio() {
  var mine = {};
  currentSymbols().forEach(function (symbol) { mine[symbol] = true; });
  return (state.liveRows || []).filter(function (row) { return mine[row.symbol] && row.ok; });
}

function pricesFreshness() {
  var meta = state.digest.meta;
  var last = fmtBarDate(meta.prices.last_bar);
  var failedSymbols = Object.keys(state.priceFail).filter(function (symbol) { return inPortfolio(symbol); });
  if (state.priceError) {
    return { cls: "st-failed", glyph: "▲", title: "Prices, refresh failed", detail: state.priceError + " · showing the end-of-day close of " + last };
  }
  var rows = liveRowsForPortfolio();
  if (rows.length) {
    var latest = "";
    var intraday = false;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].last_date > latest) { latest = rows[i].last_date; }
      if (rows[i].partial) { intraday = true; }
    }
    if (failedSymbols.length) {
      return { cls: "st-partial", glyph: "◆", title: "Prices, partly updated", detail: "checked on Yahoo " + fmtTime(state.liveFetchedUtc) + " · failed: " + failedSymbols.slice(0, 3).join(", ") + (failedSymbols.length > 3 ? " +" + (failedSymbols.length - 3) : "") };
    }
    if (intraday) {
      return { cls: "st-live", glyph: "●", title: "Prices, intraday", detail: "Yahoo data fetched " + fmtTime(state.liveFetchedUtc) + " · bar of " + fmtBarDate(latest) + ", session open, may lag · not an exchange feed" };
    }
    return { cls: "st-live", glyph: "●", title: "Prices, end-of-day close of " + fmtBarDate(latest), detail: "checked on Yahoo " + fmtTime(state.liveFetchedUtc) + " · not a live feed" };
  }
  if (failedSymbols.length) {
    return { cls: "st-failed", glyph: "▲", title: "Prices, refresh failed", detail: "failed: " + failedSymbols.slice(0, 3).join(", ") + (failedSymbols.length > 3 ? " +" + (failedSymbols.length - 3) : "") + " · showing the end-of-day close of " + last + " where one exists" };
  }
  return { cls: "st-snap", glyph: "○", title: "Prices, end-of-day close of " + last, detail: "saved copy from " + dated(meta.prices.fetched_utc) + " · not live" };
}

function freshnessRow(info, onDetails, onRefresh, busy, progress, refreshLabel) {
  var row = el("div", "fresh-row");
  add(row, el("span", "g " + info.cls, info.glyph));
  var text = el("button", "fr-text");
  text.type = "button";
  text.setAttribute("aria-haspopup", "dialog");
  var inner = el("span");
  add(inner, el("b", info.cls, info.title), el("span", "fr-detail", " · " + info.detail));
  text.appendChild(inner);
  text.addEventListener("click", onDetails);
  add(row, text);
  var action = button(busy ? "Refreshing…" : "Refresh", "linkbtn", onRefresh, refreshLabel);
  action.disabled = busy;
  add(row, action);
  if (progress) {
    var bar = document.createElement("progress");
    bar.max = progress.total;
    bar.value = progress.done;
    bar.setAttribute("aria-label", "Filings refresh progress");
    add(row, bar);
  }
  return row;
}

function renderFreshness() {
  var box = byId("freshness");
  clear(box);
  if (!state.digest) { return; }
  var fs = state.digest.filings_status;
  var filingsBusy = !!(fs && fs.running);
  add(box, freshnessRow(filingsFreshness(), function () { openFreshnessSheet(); }, function () { refreshFilings(); },
    filingsBusy, filingsBusy ? fs.progress : null, "Refresh filings from NSE"));
  add(box, freshnessRow(pricesFreshness(), function () { openFreshnessSheet(); }, function () { refreshPrices(); },
    state.pricesBusy, null, "Refresh prices from Yahoo"));
  if (state.digestFailed) {
    var note = el("div", "fresh-row");
    add(note, el("span", "g st-failed", "▲"), el("span", "t", "Could not reload the digest just now. What you see is from the last successful load."));
    box.appendChild(note);
  }
}

// ------------------------------------------------------------------ Today

function renderToday() {
  var now = new Date();
  var who = state.profile ? state.profile.name : "FINN";
  byId("today-kicker").textContent = who + " · " + now.toLocaleDateString("en-IN", { timeZone: IST, weekday: "long", day: "numeric", month: "long" });
  renderPrefs();
  var box = byId("today-empty");
  clear(box);
  var empty = !!state.portfolio && state.portfolio.symbols.length === 0;
  var broken = !state.portfolio && !!state.portfolioError;
  box.hidden = !(empty || broken);
  byId("today-grid").hidden = empty || broken;
  byId("today-foot").hidden = empty || broken;
  byId("freshness").hidden = empty || broken;
  if (empty || broken) {
    byId("coverage").hidden = true;
    renderFilterBadge();
    box.appendChild(empty ? emptyPortfolioNode(true) : demoLoadFailedNode());
    return;
  }
  if (!state.digest) { return; }
  renderFreshness();
  renderCoverage();
  renderFilterBadge();
  renderFeed();
  renderSignals();
  renderWatchlist();
}

function filingText(text) {
  // The feed keeps only the first part of each filing's summary. When it was cut, say so instead of ending mid-word.
  var cap = state.digest && state.digest.meta && state.digest.meta.announcements ? state.digest.meta.announcements.text_truncated_to_chars : 220;
  var trimmed = String(text).replace(/\s+$/, "");
  if (trimmed.length >= (cap || 220) - 5 && !/[.!?…]$/.test(trimmed)) { return trimmed + "… (summary cut short; open the filing for the full text)"; }
  return trimmed;
}

function filingNode(filing, opts) {
  opts = opts || {};
  var item = el("article", "filing");
  var meta = el("div", "f-meta");
  add(meta, el("span", "sym", filing.symbol), el("span", null, fmtTime(filing.at)));
  if (filing.tier === "high") { add(meta, el("span", "tag material", "Material")); }
  if (filing.tier === "low") { add(meta, el("span", "tag", "Routine")); }
  if (filing.dup_count > 1) { add(meta, el("span", null, "filed ×" + filing.dup_count)); }
  if (filing.source === "live") {
    add(meta, el("span", "f-src st-live", "● live · fetched " + fmtTime(filing.fetched_utc)));
  } else {
    add(meta, el("span", "f-src st-snap", "○ snapshot"));
  }
  item.appendChild(meta);
  item.appendChild(el("h3", "f-title", filing.desc));
  if (filing.text) { item.appendChild(el("p", "f-text", filingText(filing.text))); }
  if (filing.tier === "high") { item.appendChild(el("p", "f-why", "Flagged because " + filing.tier_reason.replace(/^NSE category/, "NSE category").replace(/^text mentions/, "its text mentions") + ".")); }
  var actions = el("div", "f-actions");
  var link = safeLink(filing.url, "Open filing (PDF)");
  if (link) {
    link.setAttribute("aria-label", "Open " + filing.symbol + " filing PDF on NSE (new tab)");
    actions.appendChild(link);
  }
  if (!opts.noResearch) {
    actions.appendChild(button("Research company", "linkbtn", function () { researchCompany(filing.symbol, filing); },
      "Research " + filing.symbol + " (the filing itself is not analysed)"));
  }
  item.appendChild(actions);
  return item;
}

function renderFeed() {
  var all = filingsInWindow();
  var high = [];
  var medium = [];
  var low = [];
  for (var i = 0; i < all.length; i++) {
    if (all[i].tier === "high") { high.push(all[i]); }
    else if (all[i].tier === "low") { low.push(all[i]); }
    else { medium.push(all[i]); }
  }
  var hoursLabel = state.filters.hours === 24 ? "24 hours" : (state.filters.hours === 72 ? "3 days" : "7 days");
  var gap = filingsGap();
  byId("window-note").textContent = state.digest.as_of_utc
    ? "Filed in the " + hoursLabel + " up to " + fmtTime(state.digest.as_of_utc) + ", the time of the last filings update (" + (ageText(state.digest.as_of_utc) || "just now") + ")."
    : "No filings check has finished for these companies yet, so no filings are shown.";

  var priority = byId("priority");
  clear(priority);
  byId("priority-count").textContent = high.length ? String(high.length) : "";
  if (high.length === 0) {
    var msg = el("div", "empty");
    if (gap.length) {
      add(msg, el("b", null, "No priority filings among the companies FINN has checked. "), document.createTextNode(gap.length + " of " + currentSymbols().length + " " + (gap.length === 1 ? "company has" : "companies have") + " no usable filings data yet (" + gap.slice(0, 4).join(", ") + (gap.length > 4 ? " +" + (gap.length - 4) : "") + "), so their filings are unknown, not absent. See Portfolio for each company's status."));
    } else {
      add(msg, el("b", null, "No priority filings in this window. "), document.createTextNode("Priority means NSE categories such as board outcomes, orders, dividends or management changes. Widen the window in Filters to look further back."));
    }
    priority.appendChild(msg);
  }
  for (var h = 0; h < high.length; h++) { priority.appendChild(filingNode(high[h])); }

  var others = byId("others");
  clear(others);
  var shown = medium.length + (state.filters.routine ? low.length : 0);
  byId("others-count").textContent = shown ? String(shown) : "";
  if (medium.length === 0 && (!state.filters.routine || low.length === 0)) {
    others.appendChild(el("div", "empty", gap.length ? "No other filings among the companies FINN has checked." : "No other filings in this window."));
  } else {
    if (medium.length) { others.appendChild(groupNode("Updates", medium)); }
    if (state.filters.routine && low.length) { others.appendChild(groupNode("Routine", low)); }
  }
  if (!state.filters.routine && low.length) {
    others.appendChild(el("p", "hint", low.length + " routine filing" + (low.length > 1 ? "s are" : " is") + " hidden. Show them in Filters."));
  }
}

function groupNode(title, list) {
  var details = el("details");
  var summary = el("summary");
  add(summary, el("span", null, title + " (" + list.length + ")"), chevron());
  details.appendChild(summary);
  for (var i = 0; i < list.length; i++) { details.appendChild(filingNode(list[i])); }
  return details;
}

function priceBasis() {
  return state.liveRows ? pricesFreshness() : pricesFreshness();
}

function renderSignals() {
  var box = byId("signals");
  clear(box);
  var info = pricesFreshness();
  byId("signals-note").textContent = "Volume at least " + state.filters.vol.toFixed(1) + "× its 20-day average, or a move of at least ±" + state.filters.move + "%. " + info.title + ".";
  var rows = [];
  for (var i = 0; i < state.digest.portfolio.length; i++) {
    var entry = state.digest.portfolio[i];
    var stats = currentStats(entry);
    var parts = [];
    var score = 0;
    if (stats.volume_ratio !== null && stats.volume_ratio !== undefined && stats.volume_ratio >= state.filters.vol) {
      parts.push("Volume " + stats.volume_ratio.toFixed(1) + "× average");
      score = Math.max(score, stats.volume_ratio / state.filters.vol);
    }
    if (stats.change_pct !== null && stats.change_pct !== undefined && Math.abs(stats.change_pct) >= state.filters.move) {
      parts.push("Price " + signed(stats.change_pct, 2) + "%");
      score = Math.max(score, Math.abs(stats.change_pct) / state.filters.move);
    }
    if (parts.length) { rows.push({ entry: entry, stats: stats, text: parts.join(" · "), score: score }); }
  }
  rows.sort(function (a, b) { return b.score - a.score; });
  byId("signals-count").textContent = rows.length ? String(rows.length) : "";
  if (rows.length === 0) {
    box.appendChild(el("div", "empty", "No signals at these thresholds."));
    return;
  }
  for (var r = 0; r < rows.length; r++) {
    var item = rows[r];
    var row = el("button", "row");
    row.type = "button";
    row.setAttribute("aria-label", item.entry.symbol + ": " + item.text + ". Open company.");
    var left = el("span");
    add(left, el("span", "sym", item.entry.symbol), el("span", "sig", item.text + " · bar of " + fmtBarDate(item.stats.last_date)));
    add(row, left);
    var chg = item.stats.change_pct;
    add(row, el("span", "chg " + (chg > 0 ? "up" : (chg < 0 ? "down" : "")), arrow(chg) + signed(chg, 2) + "%"));
    (function (symbol) { row.addEventListener("click", function () { openCompany(symbol); }); })(item.entry.symbol);
    box.appendChild(row);
  }
}

function renderWatchlist() {
  var box = byId("watchlist");
  clear(box);
  box.setAttribute("aria-busy", "false");
  var info = pricesFreshness();
  byId("watch-note").textContent = info.title + " · " + info.detail;
  for (var i = 0; i < state.digest.portfolio.length; i++) {
    var entry = state.digest.portfolio[i];
    var stats = currentStats(entry);
    var row = el("button", "row three");
    row.type = "button";
    var change = stats.change_pct;
    var hasPrice = stats.close !== null && stats.close !== undefined;
    var cov = priceCoverage(entry.symbol);
    var shownName = entry.name || storedName(entry.symbol) || (stats.name || "");
    row.setAttribute("aria-label", entry.symbol + (shownName ? ", " + shownName : "") + (hasPrice ? ", close " + formatNumber(stats.close, 2) : ", price " + COV[cov.s].word.toLowerCase()) +
      (hasPrice && change !== null && change !== undefined ? ", " + signed(change, 2) + " percent" : "") + ". Open company.");
    var left = el("span");
    add(left, el("span", "sym", entry.symbol), el("span", "nm", shownName));
    if (hasPrice) {
      add(row, left, el("span", "num", formatNumber(stats.close, 2)));
      var cls = change > 0 ? "up" : (change < 0 ? "down" : "");
      add(row, el("span", "chg " + cls, change === null || change === undefined ? "–" : arrow(change) + signed(change, 2) + "%"));
    } else {
      add(row, left, el("span", "num", "–"));
      add(row, el("span", "chg cov " + COV[cov.s].cls, COV[cov.s].glyph + " " + COV[cov.s].word));
    }
    (function (symbol) { row.addEventListener("click", function () { openCompany(symbol); }); })(entry.symbol);
    box.appendChild(row);
  }
}

// ------------------------------------------------------------------ filter sheet

function activeFilterCount() {
  var n = 0;
  if (state.filters.hours !== DEFAULT_FILTERS.hours) { n++; }
  if (state.filters.vol !== DEFAULT_FILTERS.vol) { n++; }
  if (state.filters.move !== DEFAULT_FILTERS.move) { n++; }
  if (state.filters.routine !== DEFAULT_FILTERS.routine) { n++; }
  return n;
}

function renderFilterBadge() {
  var badge = byId("filter-badge");
  var n = activeFilterCount();
  badge.hidden = n === 0;
  badge.textContent = String(n);
  byId("open-filters").setAttribute("aria-label", n ? "Filters, " + n + " changed from default" : "Filters");
}

function stepperRow(label, hint, getText, onMinus, onPlus, minusLabel, plusLabel) {
  var row = el("div", "step");
  var lbl = el("div", "lbl", label);
  lbl.appendChild(el("small", null, hint));
  add(row, lbl);
  var wrap = el("div", "stepper");
  var out = el("output", null, getText());
  var minus = button("−", null, function () { onMinus(); out.textContent = getText(); }, minusLabel);
  var plus = button("+", null, function () { onPlus(); out.textContent = getText(); }, plusLabel);
  add(wrap, minus, out, plus);
  add(row, wrap);
  return row;
}

function openFilters() {
  openSheet("Filters", function (body) {
    var set = el("fieldset", "set");
    set.appendChild(el("legend", null, "Filing window"));
    var seg = el("div", "seg");
    [[24, "24 hours"], [72, "3 days"], [168, "7 days"]].forEach(function (opt) {
      var label = el("label");
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "window";
      input.value = String(opt[0]);
      input.checked = state.filters.hours === opt[0];
      input.addEventListener("change", function () {
        state.filters.hours = opt[0];
        saveFilters();
        renderToday();
      });
      add(label, input, el("span", null, opt[1]));
      seg.appendChild(label);
    });
    set.appendChild(seg);
    set.appendChild(el("p", "hint", "Counted back from the last filings update."));
    body.appendChild(set);

    var signals = el("fieldset", "set");
    signals.appendChild(el("legend", null, "Market signals"));
    signals.appendChild(stepperRow("Volume signal", "multiple of the 20-day average",
      function () { return state.filters.vol.toFixed(1) + "×"; },
      function () { state.filters.vol = Math.max(1, Math.round((state.filters.vol - 0.5) * 10) / 10); saveFilters(); renderToday(); },
      function () { state.filters.vol = Math.min(10, Math.round((state.filters.vol + 0.5) * 10) / 10); saveFilters(); renderToday(); },
      "Lower the volume signal", "Raise the volume signal"));
    signals.appendChild(stepperRow("Price move", "one-day change, up or down",
      function () { return "±" + state.filters.move + "%"; },
      function () { state.filters.move = Math.max(1, state.filters.move - 1); saveFilters(); renderToday(); },
      function () { state.filters.move = Math.min(20, state.filters.move + 1); saveFilters(); renderToday(); },
      "Lower the price move threshold", "Raise the price move threshold"));
    body.appendChild(signals);

    var routine = el("label", "check");
    var box = document.createElement("input");
    box.type = "checkbox";
    box.checked = state.filters.routine;
    box.addEventListener("change", function () { state.filters.routine = box.checked; saveFilters(); renderToday(); });
    add(routine, box, el("span", null, "Show routine filings (newspaper notices, share-scheme updates)"));
    body.appendChild(routine);

    var actions = el("div", "sheet-actions");
    add(actions, button("Reset", "btn", function () {
      state.filters = { hours: DEFAULT_FILTERS.hours, vol: DEFAULT_FILTERS.vol, move: DEFAULT_FILTERS.move, routine: DEFAULT_FILTERS.routine };
      saveFilters();
      renderToday();
      openFilters();
    }), button("Done", "btn primary", closeSheet));
    body.appendChild(actions);
  });
}

// ------------------------------------------------------------------ freshness sheet

function openFreshnessSheet() {
  openSheet("Data freshness", function (body) {
    var fs = state.digest.filings_status;
    var f = filingsFreshness();
    var p = pricesFreshness();
    body.appendChild(el("h3", "serif", "Filings"));
    body.appendChild(el("p", null, f.glyph + " " + f.title + " · " + f.detail));
    body.appendChild(el("p", "hint", "Source: NSE corporate announcements. A refresh reads the last 7 days for each company in your portfolio. The saved copy was fetched " + fmtTime(fs.snapshot_fetched_utc) + "."));
    if (fs.last_success_utc) { body.appendChild(el("p", "hint", "Last successful update: " + fmtTime(fs.last_success_utc) + ".")); }
    var busy = !!fs.running;
    var rf = button(busy ? "Refreshing…" : "Refresh filings", "btn", function () { refreshFilings(); closeSheet(); }, "Refresh filings from NSE");
    rf.disabled = busy;
    var wrap = el("div", "sheet-actions");
    wrap.appendChild(rf);
    body.appendChild(wrap);
    var list = el("dl", "kv");
    for (var i = 0; i < state.digest.portfolio.length; i++) {
      var sym = state.digest.portfolio[i].symbol;
      var st = fs.symbols[sym] || { state: "snapshot" };
      var words = {
        live: "● live, filings found", valid_empty: "● live check: none in 7 days", ambiguous_empty: "◆ empty, cannot be verified",
        stale_live: "▲ refresh failed, older live data kept", failed_snapshot: "▲ refresh failed, snapshot shown", snapshot: "○ snapshot only",
        unchecked: "○ not checked yet", failed: "▲ refresh failed, no data"
      };
      var text = words[st.state] || st.state;
      if (st.fetched_utc) { text = text + " · " + fmtTime(st.fetched_utc); }
      if (st.detail && (st.state === "stale_live" || st.state === "failed_snapshot")) { text = text + " · " + st.detail; }
      add(list, el("dt", null, sym), el("dd", null, text));
    }
    body.appendChild(list);

    body.appendChild(el("h3", "serif", "Prices"));
    body.appendChild(el("p", null, p.glyph + " " + p.title + " · " + p.detail));
    body.appendChild(el("p", "hint", "Source: Yahoo Finance daily bars. This is the latest available end-of-day close, not a live exchange feed. Refreshing fetches the newest bar and says whether it is a finished close or an intraday bar. Prices and filings are refreshed separately."));
    var rp = button(state.pricesBusy ? "Refreshing…" : "Refresh prices", "btn", function () { refreshPrices(); closeSheet(); }, "Refresh prices from Yahoo");
    rp.disabled = state.pricesBusy;
    var wrap2 = el("div", "sheet-actions");
    wrap2.appendChild(rp);
    body.appendChild(wrap2);
  });
}

// ------------------------------------------------------------------ refreshes

function mergeLiveRows(rows, fetchedUtc) {
  var list = state.liveRows ? state.liveRows.slice() : [];
  rows.forEach(function (row) {
    if (!row.ok) { return; }
    var placed = false;
    for (var i = 0; i < list.length; i++) {
      if (list[i].symbol === row.symbol) { list[i] = row; placed = true; }
    }
    if (!placed) { list.push(row); }
  });
  state.liveRows = list;
  if (fetchedUtc) { state.liveFetchedUtc = fetchedUtc; }
}

function refreshPrices(symbols) {
  var wanted = symbols && symbols.length ? symbols.filter(inPortfolio) : currentSymbols();
  if (wanted.length === 0) { return; }
  if (!symbols && state.pricesBusy) { return; }
  var whole = !symbols;
  if (whole) { state.pricesBusy = true; state.priceError = null; }
  wanted.forEach(function (symbol) { state.priceLoading[symbol] = true; });
  renderAfterPriceChange();
  fetchJson("/api/prices/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbols: wanted }) }, 25000, 2)
    .then(function (result) {
      if (!result.ok) {
        var code = result.body.detail && result.body.detail.code ? result.body.detail.code : "HTTP " + result.status;
        var text = code === "refresh_too_soon" ? "too soon after the last refresh; wait a moment" : (code === "live_prices_unavailable" ? "the price source could not be reached" : code);
        if (whole) { state.priceError = text; }
        wanted.forEach(function (symbol) { state.priceFail[symbol] = code; });
        return;
      }
      mergeLiveRows(result.body.rows, result.body.fetched_utc);
      result.body.rows.forEach(function (row) {
        if (row.ok) { delete state.priceFail[row.symbol]; } else { state.priceFail[row.symbol] = row.error || "failed"; }
        if (row.ok && row.name) { rememberName(row.symbol, row.name); }
      });
      if (whole) { state.priceError = null; }
    })
    .catch(function () {
      if (whole) { state.priceError = "no answer from the server"; }
      wanted.forEach(function (symbol) { state.priceFail[symbol] = "no answer from the server"; });
    })
    .then(function () {
      if (whole) { state.pricesBusy = false; }
      wanted.forEach(function (symbol) { delete state.priceLoading[symbol]; });
      renderAfterPriceChange();
    });
}

function renderAfterPriceChange() {
  if (state.digest) { renderToday(); }
  renderPortfolio();
}

function autoCover() {
  // Runs after each digest load. One automatic filings refresh per page load (when nothing live is under 30 minutes old),
  // plus a one-off check for any company that has no filings data at all, and a one-off price fetch for any without a price.
  var fs = state.digest.filings_status;
  var toCheck = [];
  if (!state.autoRefreshed) {
    state.autoRefreshed = true;
    var fresh = fs && fs.last_success_utc && (Date.now() - new Date(fs.last_success_utc).getTime()) < 30 * 60 * 1000;
    if (!fresh) {
      currentSymbols().forEach(function (symbol) { state.autoChecked[symbol] = true; toCheck.push(symbol); });
    }
  }
  var needPrice = [];
  state.digest.portfolio.forEach(function (entry) {
    var st = fs.symbols[entry.symbol];
    if (st && st.state === "unchecked" && !state.autoChecked[entry.symbol]) {
      state.autoChecked[entry.symbol] = true;
      toCheck.push(entry.symbol);
    }
    var hasPrice = (entry.close !== null && entry.close !== undefined) || !!liveRowFor(entry.symbol);
    if (!hasPrice && !state.autoPrice[entry.symbol]) {
      state.autoPrice[entry.symbol] = true;
      needPrice.push(entry.symbol);
    }
  });
  if (toCheck.length) { refreshFilings(toCheck); }
  if (needPrice.length) { refreshPrices(needPrice); }
}

function refreshFilings(symbols) {
  var wanted = symbols && symbols.length ? symbols.filter(inPortfolio) : currentSymbols();
  if (wanted.length === 0) { return; }
  wanted.forEach(function (symbol) { state.filingsPending[symbol] = true; });
  renderAfterPriceChange();
  fetchJson("/api/filings/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbols: wanted }) }, 15000, 3)
    .then(function () { return pollFilings(); })
    .catch(function () {
      wanted.forEach(function (symbol) { delete state.filingsPending[symbol]; });
      if (state.digest && state.digest.filings_status) {
        state.digestFailed = true;
        renderFreshness();
      }
      renderPortfolio();
    });
}

function pollFilings() {
  if (state.filingsPolling) { return; }
  state.filingsPolling = true;
  var misses = 0;
  function finish() {
    state.filingsPolling = false;
    state.filingsPending = {};
  }
  function tick() {
    fetchJson("/api/filings/status", {}, 10000, 1)
      .then(function (result) {
        misses = 0;
        var status = result.body;
        if (state.digest && state.digest.filings_status) {
          state.digest.filings_status.running = status.running;
          state.digest.filings_status.progress = status.progress;
          renderFreshness();
          renderPortfolio();
        }
        if (status.running) {
          window.setTimeout(tick, 2000);
        } else {
          finish();
          loadDigest();
        }
      })
      .catch(function () {
        misses = misses + 1;
        if (misses >= 8) {
          finish();
          state.digestFailed = true;
          renderFreshness();
          renderPortfolio();
          return;
        }
        window.setTimeout(tick, 2000);
      });
  }
  tick();
}

// ------------------------------------------------------------------ company sheet

function openCompany(symbol) {
  var entry = null;
  for (var i = 0; i < state.digest.portfolio.length; i++) {
    if (state.digest.portfolio[i].symbol === symbol) { entry = state.digest.portfolio[i]; }
  }
  if (!entry) { return; }
  var stats = currentStats(entry);
  openSheet(entry.symbol, function (body) {
    var shownName = entry.name || storedName(symbol) || stats.name;
    if (shownName) { body.appendChild(el("p", "hint", shownName)); }
    var price = el("p", "price-big", stats.close === null || stats.close === undefined ? "–" : "₹" + formatNumber(stats.close, 2));
    body.appendChild(price);
    var chg = stats.change_pct;
    var line = el("p", chg > 0 ? "up" : (chg < 0 ? "down" : ""), chg === null || chg === undefined ? "" : arrow(chg) + signed(chg, 2) + "% on the day");
    line.style.fontWeight = "700";
    body.appendChild(line);
    var info = pricesFreshness();
    var priceCov = priceCoverage(symbol);
    if (stats.close === null || stats.close === undefined) {
      body.appendChild(el("p", "hint", COV[priceCov.s].glyph + " Price " + COV[priceCov.s].word.toLowerCase() + ": " + priceCov.text));
    } else {
      body.appendChild(el("p", "hint", "Bar of " + fmtBarDate(stats.last_date) + " · volume " + compact(stats.volume) +
        (stats.volume_ratio ? " (" + stats.volume_ratio.toFixed(2) + "× 20-day average)" : "") + " · " + info.title + "."));
    }
    var actions = el("div", "sheet-actions");
    actions.appendChild(button("Research company", "btn primary", function () { closeSheet(); researchCompany(symbol, null); }, "Research " + symbol));
    body.appendChild(actions);

    body.appendChild(el("h3", "serif", "Filings"));
    var st = state.digest.filings_status.symbols[symbol] || { state: "snapshot" };
    var labels = {
      live: "Live from NSE, " + fmtTime(st.fetched_utc), valid_empty: "Checked live " + fmtTime(st.fetched_utc) + ": none in the last 7 days",
      ambiguous_empty: "NSE returned nothing, even over 90 days; this cannot be read as no filings",
      stale_live: "Refresh failed; showing live data from " + fmtTime(st.fetched_utc), failed_snapshot: "Refresh failed; showing the saved copy",
      snapshot: "Saved copy from " + fmtTime(state.digest.filings_status.snapshot_fetched_utc) + ", not live",
      unchecked: "Not checked yet. FINN has no filings data for this company until a live NSE check has run",
      failed: "The NSE check failed and there is no saved copy, so FINN has no filings data for this company"
    };
    body.appendChild(el("p", "hint", labels[st.state] || st.state));
    var found = 0;
    for (var j = 0; j < state.digest.filings.length; j++) {
      var filing = state.digest.filings[j];
      if (filing.symbol === symbol) { body.appendChild(filingNode(filing, { noResearch: true })); found = found + 1; }
    }
    if (found === 0) {
      var unknown = st.state === "unchecked" || st.state === "failed" || st.state === "ambiguous_empty";
      body.appendChild(el("div", "empty", entry.no_filings_note || (unknown ? "Filings for this company are unknown, not absent." : "No filings in view for this company.")));
    }
  });
}

// ------------------------------------------------------------------ research: entry points

function researchCompany(symbol, filing) {
  state.focusFiling = filing || null;
  state.contextNote = null;
  showView("research");
  byId("r-input").value = symbol;
  byId("r-input-error").hidden = true;
  renderContext();
  var go = byId("r-go");
  if (go.scrollIntoView) { byId("r-form").scrollIntoView({ block: "start" }); }
  go.focus();
}

function renderContext() {
  var box = byId("r-context");
  clear(box);
  var filing = state.focusFiling;
  if (!filing) { return; }
  var ctx = el("section", "ctx");
  ctx.setAttribute("aria-label", "Filing you came from");
  ctx.appendChild(el("h2", "serif", "The filing you came from"));
  var meta = el("div", "f-meta");
  add(meta, el("span", "sym", filing.symbol), el("span", null, "Filed " + fmtTime(filing.at)),
    el("span", filing.source === "live" ? "f-src st-live" : "f-src st-snap", filing.source === "live" ? "● live · fetched " + fmtTime(filing.fetched_utc) : "○ snapshot"));
  ctx.appendChild(meta);
  ctx.appendChild(el("p", "f-title", filing.desc));
  if (filing.text) { ctx.appendChild(el("p", "f-text", filingText(filing.text))); }
  var link = safeLink(filing.url, "Open filing (PDF)");
  if (link) { ctx.appendChild(link); }
  ctx.appendChild(el("p", "note", "FINN has not analysed this filing. Research reads the company's latest annual report and earnings-call transcript, so it can only reflect this filing if it is one of them."));
  if (state.contextNote) { ctx.appendChild(el("p", "note", state.contextNote)); }
  ctx.appendChild(button("Clear", "linkbtn", function () { state.focusFiling = null; state.contextNote = null; renderContext(); }, "Clear the filing context"));
  box.appendChild(ctx);
}

function parseDocDate(doc) {
  if (!doc || doc.status !== "ok") { return null; }
  var text = (doc.date_in_text || "").replace(/(\d+)(st|nd|rd|th)/, "$1").replace(",", "");
  var parsed = Date.parse(text);
  if (!isNaN(parsed)) { return new Date(parsed); }
  parsed = Date.parse("1 " + (doc.date_label || ""));
  return isNaN(parsed) ? null : new Date(parsed);
}

function filingVsBriefNote(brief) {
  var filing = state.focusFiling;
  if (!filing || !brief || !brief.documents) { return null; }
  if ((brief.ticker || (brief.company || {}).nse_symbol) !== filing.symbol) { return null; }
  var filed = new Date(filing.at);
  var newest = null;
  var docs = [brief.documents.concall, brief.documents.annual_report];
  for (var i = 0; i < docs.length; i++) {
    var date = parseDocDate(docs[i]);
    if (date && (newest === null || date > newest)) { newest = date; }
  }
  if (newest === null) { return "This filing is dated " + fmtTime(filing.at) + ". The dates of the documents below could not be read, so FINN cannot say which is newer."; }
  var month = newest.toLocaleDateString("en-IN", { month: "short", year: "numeric" });
  if (filed > newest) { return "This filing (" + fmtTime(filing.at) + ") is newer than the newest document below (about " + month + "), so the research below does not cover it."; }
  return "This filing (" + fmtTime(filing.at) + ") is not newer than the documents below (about " + month + "), so they may discuss it. Open the filing to check.";
}

// ------------------------------------------------------------------ research: recents and search

function loadRecents() {
  var raw = storeGet("finn.recents");
  if (!raw) { return []; }
  try {
    var list = JSON.parse(raw);
    return Array.isArray(list) ? list : [];
  } catch (e) { return []; }
}

function addRecent(symbol, name) {
  var list = loadRecents().filter(function (item) { return item.symbol !== symbol; });
  var previous = loadRecents().filter(function (item) { return item.symbol === symbol; })[0];
  list.unshift({ symbol: symbol, name: name || (previous && previous.name) || null, at: new Date().toISOString() });
  storeSet("finn.recents", JSON.stringify(list.slice(0, 6)));
}

function briefKey(symbol) { return "finn.brief." + symbol; }

function renderRecents() {
  var box = byId("r-recents");
  clear(box);
  var recents = loadRecents();
  var wrap = el("section", "recents");
  var title = recents.length ? "Recent companies" : "From your portfolio";
  wrap.appendChild(el("h2", "serif", title));
  var rows = [];
  if (recents.length) {
    rows = recents.map(function (item) {
      var saved = storeGet(briefKey(item.symbol)) ? "result saved on this device" : "";
      return { symbol: item.symbol, name: item.name, when: "Last " + fmtDay(item.at), saved: saved };
    });
  } else if (state.portfolio) {
    rows = state.portfolio.symbols.slice(0, 5).map(function (item) { return { symbol: item.symbol, name: displayName(item.symbol), when: "", saved: "" }; });
  }
  if (rows.length === 0) { return; }
  rows.forEach(function (item) {
    var row = el("button", "recent");
    row.type = "button";
    row.setAttribute("aria-label", "Research " + item.symbol + (item.name ? ", " + item.name : ""));
    var left = el("span");
    add(left, el("span", "sym", item.symbol), el("span", "nm", (item.name || "") + (item.saved ? (item.name ? " · " : "") + item.saved : "")));
    add(row, left, el("span", "when", item.when));
    row.addEventListener("click", function () { byId("r-input").value = item.symbol; state.focusFiling = null; renderContext(); startResearch(item.symbol); });
    wrap.appendChild(row);
  });
  box.appendChild(wrap);
}

function renderOptions() {
  var list = byId("r-options");
  clear(list);
  var seen = {};
  var symbols = loadRecents().map(function (item) { return item.symbol; });
  symbols = symbols.concat(currentSymbols());
  symbols.forEach(function (symbol) {
    if (seen[symbol]) { return; }
    seen[symbol] = true;
    var option = document.createElement("option");
    option.value = symbol;
    list.appendChild(option);
  });
}

function initResearchView() {
  renderOptions();
  if (!state.research.running && byId("r-result").childNodes.length === 0) { renderRecents(); }
  if (!state.guidanceLoaded) { loadGuidance(); }
}

// ------------------------------------------------------------------ research: running

var ERROR_TEXT = {
  invalid_ticker: "That is not a valid NSE ticker. Use letters, digits, & or - (up to 20 characters).",
  busy: "Another analysis is running on the free server.",
  rate_limited_hourly: "The hourly limit for fresh analyses has been reached.",
  rate_limited_daily: "The daily limit for fresh analyses has been reached.",
  model_not_configured: "The live model is not configured on this server.",
  ticker_not_found: "No company page was found for that ticker. Check the NSE symbol.",
  source_unreachable: "The document index (Screener) could not be reached.",
  company_not_resolved: "The page did not identify a company for that ticker.",
  ticker_mismatch: "The page found belongs to a different NSE symbol.",
  documents_not_listed: "The company page lists no annual report or earnings-call documents.",
  job_timeout: "The analysis took too long and was stopped."
};

function describeError(code, fallback) {
  return ERROR_TEXT[code] || fallback || ("Request failed (" + code + ").");
}

var STEP_LABELS = [
  [/^discover/, "Find the company's documents"],
  [/^concall/, "Read the latest earnings call"],
  [/^annual_report/, "Read the latest annual report"],
  [/^overview|^business/, "Write the business overview"],
  [/^outlook/, "Find management's outlook"],
  [/^numbers|^financ/, "Find the reported numbers"]
];

function stepLabel(name) {
  for (var i = 0; i < STEP_LABELS.length; i++) {
    if (STEP_LABELS[i][0].test(name)) { return STEP_LABELS[i][1]; }
  }
  return name;
}

function setBusy(busy) {
  byId("r-go").disabled = busy;
  byId("r-input").disabled = busy;
}

function elapsedText() {
  var seconds = Math.round((Date.now() - state.research.startedAt) / 1000);
  var minutes = Math.floor(seconds / 60);
  return minutes > 0 ? minutes + " min " + (seconds % 60) + " s" : seconds + " s";
}

function renderRunning(job) {
  var box = byId("r-run");
  clear(box);
  var run = el("section", "run");
  run.appendChild(el("h2", "serif", "Researching " + state.research.symbol));
  run.appendChild(el("p", "hint", elapsedText() + " elapsed · usually 2 to 4 minutes · it stops at 4.5 minutes"));
  var bar = document.createElement("progress");
  bar.max = 270;
  bar.value = Math.min(270, Math.round((Date.now() - state.research.startedAt) / 1000));
  bar.setAttribute("aria-label", "Time elapsed of the 4.5 minute limit");
  run.appendChild(bar);
  var steps = el("ol", "steps");
  var list = (job && job.stages) || [];
  if (list.length === 0) {
    var first = el("li");
    add(first, el("span", "g", "…"), el("span", null, "Contacting the server"));
    steps.appendChild(first);
  }
  for (var i = 0; i < list.length; i++) {
    var stage = list[i];
    var glyph = { ok: "✓", running: "…", failed: "✕", unverified: "!", not_found: "–" }[stage.status] || "•";
    var li = el("li");
    var text = el("span");
    text.appendChild(document.createTextNode(stepLabel(stage.name)));
    if (stage.status === "failed" || stage.status === "unverified" || stage.status === "not_found") {
      text.appendChild(el("span", "d", { failed: "Failed", unverified: "Could not be verified", not_found: "Not found" }[stage.status] + (stage.note ? ": " + stage.note : "")));
    }
    add(li, el("span", "g", glyph), text);
    steps.appendChild(li);
  }
  run.appendChild(steps);
  if (Date.now() - state.research.startedAt > 50000) {
    run.appendChild(el("p", "hint", "Waiting is normal: the free model plan limits how fast each section can be written."));
  }
  box.appendChild(run);
}

function validTicker(text) {
  return /^[A-Za-z0-9&-]{1,20}$/.test(text);
}

function onSubmit(event) {
  event.preventDefault();
  var raw = byId("r-input").value.trim();
  var error = byId("r-input-error");
  if (!validTicker(raw)) {
    error.textContent = describeError("invalid_ticker");
    error.hidden = false;
    return;
  }
  error.hidden = true;
  startResearch(raw.toUpperCase());
}

function startResearch(symbol) {
  if (state.focusFiling && state.focusFiling.symbol !== symbol) { state.focusFiling = null; state.contextNote = null; renderContext(); }
  var run = state.research;
  window.clearTimeout(run.timer);
  run.token = run.token + 1;
  var token = run.token;
  run.symbol = symbol;
  run.running = true;
  run.startedAt = Date.now();
  run.jobId = null;
  run.job = null;
  state.contextNote = null;
  byId("r-input").value = symbol;
  clear(byId("r-result"));
  clear(byId("r-recents"));
  setBusy(true);
  renderRunning(null);
  addRecent(symbol, null);
  fetchJson("/api/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticker: symbol }) }, 15000, 4)
    .then(function (result) {
      if (token !== run.token) { return; }
      if (!result.ok) {
        var detail = result.body.detail || {};
        var code = detail.code || ("HTTP " + result.status);
        if (result.status === 422 && !detail.code) { code = "invalid_ticker"; }
        if (code === "invalid_ticker") {
          endRun();
          var error = byId("r-input-error");
          error.textContent = describeError(code);
          error.hidden = false;
          return;
        }
        researchFailed(symbol, describeError(code, detail.message), code, token);
        return;
      }
      handleJob(result.body, token);
    })
    .catch(function () {
      if (token !== run.token) { return; }
      researchFailed(symbol, "The server did not answer after 4 tries.", "network", token);
    });
}

function endRun() {
  state.research.running = false;
  clear(byId("r-run"));
  setBusy(false);
}

function handleJob(job, token) {
  var run = state.research;
  if (token !== run.token) { return; }
  run.jobId = job.job_id;
  run.job = job;
  if (job.status === "running") {
    renderRunning(job);
    window.clearTimeout(run.timer);
    run.timer = window.setTimeout(function () { pollJob(token); }, 3000);
    return;
  }
  if (job.status === "failed") {
    var error = job.error || {};
    researchFailed(run.symbol, describeError(error.code, error.message), error.code || "failed", token);
    return;
  }
  researchSucceeded(job.brief, token);
}

function pollJob(token) {
  var run = state.research;
  if (token !== run.token) { return; }
  if (Date.now() - run.startedAt > 8 * 60 * 1000) {
    researchFailed(run.symbol, "Still running after 8 minutes; stopped waiting.", "timeout", token);
    return;
  }
  fetchJson("/api/analyze/" + run.jobId, {}, 10000, 2)
    .then(function (result) {
      if (token !== run.token) { return; }
      if (!result.ok) {
        researchFailed(run.symbol, "The server lost this job (the free server may have restarted).", "job_lost", token);
        return;
      }
      handleJob(result.body, token);
    })
    .catch(function () {
      if (token !== run.token) { return; }
      renderRunning(run.job);
      var note = el("p", "hint", "No answer from the server just now. Still trying; the analysis keeps running there.");
      byId("r-run").appendChild(note);
      run.timer = window.setTimeout(function () { pollJob(token); }, 3000);
    });
}

function saveBrief(brief) {
  var symbol = brief && (brief.ticker || (brief.company || {}).nse_symbol);
  if (!symbol || brief.answer_source === "snapshot_fallback") { return; }
  var text = JSON.stringify({ saved_at: new Date().toISOString(), brief: brief });
  if (text.length < 1500000) { storeSet(briefKey(symbol), text); }
}

function researchSucceeded(brief, token) {
  var symbol = state.research.symbol;
  endRun();
  var box = byId("r-result");
  clear(box);
  if (brief.answer_source === "snapshot_fallback") {
    // The server decided not to run fresh research (limit or configuration) and sent its saved answers.
    var reason = brief.fallback_reason || {};
    renderOperator(box, symbol, brief.snapshot_answers || [], {
      why: "Fresh research was not run" + (reason.code ? " (" + describeError(reason.code, reason.message).replace(/\.$/, "") + ")" : "") + "."
    });
    return;
  }
  saveBrief(brief);
  addRecent(symbol, (brief.company || {}).name || null);
  state.contextNote = filingVsBriefNote(brief);
  renderContext();
  var reused = brief.answer_source === "cached_brief";
  renderBrief(box, brief, {
    kind: reused ? "reused" : "fresh",
    line: reused ? "Reused from an earlier run on the same documents · generated " + fmtTime(brief.generated_at)
      : "Fresh analysis · generated " + fmtTime(brief.generated_at)
  });
  jumpToResult();
}

function jumpToResult() {
  var box = byId("r-result");
  if (box.scrollIntoView) { box.scrollIntoView({ block: "start" }); }
}

// Fresh research failed. Show a saved result for the SAME company if one exists, labelled with its date and
// provenance; otherwise an honest error with Retry. Never invent one.
function researchFailed(symbol, reason, code, token) {
  if (token !== state.research.token) { return; }
  endRun();
  var box = byId("r-result");
  clear(box);
  var raw = storeGet(briefKey(symbol));
  var saved = null;
  if (raw) {
    try { saved = JSON.parse(raw); } catch (e) { saved = null; }
  }
  var savedFor = saved && saved.brief ? String(saved.brief.ticker || (saved.brief.company || {}).nse_symbol || "").toUpperCase() : "";
  if (saved && saved.brief && savedFor !== symbol) {
    saved = null;  // never show a saved brief for a different company, even if the stored value was moved or edited
  }
  if (saved && saved.brief) {
    renderBrief(box, saved.brief, {
      kind: "device",
      failure: reason,
      line: "Saved on this device · model-generated " + fmtTime(saved.brief.generated_at || saved.saved_at),
      retry: symbol
    });
    return;
  }
  loadQuestions().then(function (questions) {
    var mine = questions.filter(function (q) { return q.symbol === symbol; });
    if (mine.length === 0) { renderResearchError(box, symbol, reason); return; }
    return Promise.all(mine.map(function (q) {
      return fetchJson("/api/research", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question_id: q.id, mode: "cached" }) }, 15000, 2)
        .then(function (result) { return result.ok ? result.body : null; })
        .catch(function () { return null; });
    })).then(function (records) {
      var good = records.filter(function (r) { return r && r.source_type; });
      if (good.length === 0) { renderResearchError(box, symbol, reason); return; }
      renderOperator(box, symbol, good, { why: "Fresh research failed: " + reason, retry: symbol });
    });
  }).catch(function () { renderResearchError(box, symbol, reason); });
}

function loadQuestions() {
  if (state.questions) { return Promise.resolve(state.questions); }
  return fetchJson("/api/questions", {}, 10000, 2).then(function (result) {
    state.questions = result.ok ? (result.body.questions || []) : [];
    return state.questions;
  }).catch(function () { return []; });
}

function renderResearchError(box, symbol, reason) {
  clear(box);
  var notice = el("div", "notice bad");
  notice.setAttribute("role", "alert");
  add(notice, el("b", null, "✕ Research for " + symbol + " did not complete"), el("span", null, reason + " No saved result exists for this company, so nothing is shown."));
  var actions = el("div", "actions");
  actions.appendChild(button("Retry", "btn primary", function () { startResearch(symbol); }, "Retry research for " + symbol));
  notice.appendChild(actions);
  box.appendChild(notice);
  notice.scrollIntoView && notice.scrollIntoView({ block: "center" });
}

// ------------------------------------------------------------------ research: result rendering

function docKind(brief, url) {
  var docs = (brief && brief.documents) || {};
  if (docs.concall && docs.concall.url === url) { return "Earnings call"; }
  if (docs.annual_report && docs.annual_report.url === url) { return "Annual report"; }
  return "Document";
}

function highlightInto(container, text, quote) {
  var words = (quote || "").trim().split(/\s+/);
  var found = null;
  if (words.length > 0 && words[0] !== "") {
    var escaped = words.map(function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); });
    found = new RegExp(escaped.join("\\s+"), "i").exec(text);
  }
  if (!found) { container.textContent = text; return false; }
  container.appendChild(document.createTextNode(text.slice(0, found.index)));
  container.appendChild(el("mark", null, found[0]));
  container.appendChild(document.createTextNode(text.slice(found.index + found[0].length)));
  return true;
}

// One claim or number. `ev` = {kind, date, page, speaker, url, title, quote, context, passage}
function claimNode(headline, ev, metaExtra, options) {
  options = options || {};
  var details = el("details", "claim");
  var summary = el("summary");
  var left = el("span");
  left.appendChild(el("span", "c-text", headline));
  var meta = ev.kind + " · " + (ev.date || "date not shown") + " · PDF page " + ev.page + (ev.speaker ? " · " + ev.speaker : "");
  if (metaExtra) { meta = metaExtra + " · " + meta; }
  left.appendChild(el("span", "c-meta", meta));
  add(summary, left, chevron());
  details.appendChild(summary);

  var evidence = el("div", "evidence");
  evidence.appendChild(el("p", "ev-label", "Supporting quote"));
  evidence.appendChild(el("blockquote", null, "“" + ev.quote + "”"));
  if (ev.context) { evidence.appendChild(el("p", "ev-ctx", "Said just before: “" + ev.context + "”")); }
  var dl = el("dl", "kv");
  add(dl, el("dt", null, "Document"), el("dd", null, ev.title || ev.kind));
  add(dl, el("dt", null, "Dated"), el("dd", null, ev.date ? (/^annual report/i.test(ev.date) ? ev.date + " (the report's year; no filing date is shown)" : ev.date) : "not shown in the document list"));
  add(dl, el("dt", null, "Where"), el("dd", null, "PDF page " + ev.page + (ev.speaker ? ", " + ev.speaker : "")));
  evidence.appendChild(dl);
  var link = safeLink(ev.url, "Open original document ↗");
  if (link) { evidence.appendChild(link); }
  if (ev.passage) {
    var more = el("details", "ev-more");
    more.appendChild(el("summary", null, "Show the surrounding text"));
    var holder = el("p");
    highlightInto(holder, ev.passage, ev.quote);
    more.appendChild(holder);
    evidence.appendChild(more);
  }
  evidence.appendChild(el("p", "ev-note", options.operator
    ? "✓ The quote appears word for word on this page. Written and read by hand by the builder; the check does not prove the meaning."
    : "✓ The quote appears word for word in the source text and any numbers match. This does not check meaning."));
  details.appendChild(evidence);
  return details;
}

function citationEvidence(brief, citation, passages) {
  var passage = null;
  for (var i = 0; i < (passages || []).length; i++) {
    if (passages[i].source_id === citation.source_id) { passage = passages[i].text; }
  }
  return { kind: docKind(brief, citation.url), date: citation.doc_date, page: citation.pdf_page, speaker: citation.speaker,
    url: citation.url, title: citation.doc_title, quote: citation.excerpt || "", context: citation.context, passage: passage };
}

function sectionShell(id, title, statusText, bad) {
  var section = el("section", "rsec");
  section.id = id;
  section.setAttribute("aria-labelledby", id + "-h");
  var h = el("h3", null, title);
  h.id = id + "-h";
  section.appendChild(h);
  if (statusText) { section.appendChild(el("p", bad ? "sec-status bad" : "sec-status", statusText)); }
  return section;
}

function briefSection(brief, id, title, section, kind) {
  var status = section.status;
  var statusText = null;
  var bad = false;
  if (status === "not_found") { statusText = "Not found: " + (section.reason || ""); bad = true; }
  else if (status === "unverified") { statusText = "Could not be verified: " + (section.reason || ""); bad = true; }
  else if (status === "unavailable") { statusText = "Unavailable: " + (section.reason || ""); bad = true; }
  var node = sectionShell(id, title, statusText, bad);
  var claims = section.claims || [];
  for (var i = 0; i < claims.length; i++) {
    node.appendChild(claimNode(claims[i].text, citationEvidence(brief, claims[i].citation, section.passages)));
  }
  var items = section.items || [];
  for (var j = 0; j < items.length; j++) {
    var item = items[j];
    var box = el("div", "metric");
    box.appendChild(el("p", "m-label", item.metric));
    var val = el("p", "m-val", String(item.value));
    val.appendChild(el("small", null, " " + item.unit));
    box.appendChild(val);
    var meta = el("p", "m-meta");
    if (item.scope === "not stated") { meta.appendChild(el("span", "unk", "! Scope not stated in the passage")); }
    else { meta.appendChild(document.createTextNode(item.scope)); }
    meta.appendChild(document.createTextNode(" · " + item.period));
    box.appendChild(meta);
    box.appendChild(claimNode("Show source", citationEvidence(brief, item.citation, section.passages)));
    node.appendChild(box);
  }
  if (status === "ok" && claims.length === 0 && items.length === 0) {
    node.appendChild(el("p", "sec-status", "Nothing was found to report here."));
  }
  var failed = section.failed_checks || [];
  if (failed.length) {
    var removed = el("details", "removed");
    removed.appendChild(el("summary", null, "! " + failed.length + " statement" + (failed.length > 1 ? "s" : "") + " removed: they failed the automatic checks"));
    for (var k = 0; k < failed.length; k++) {
      var f = failed[k];
      var why = f.problems && f.problems.length ? f.problems.join(", ") : "quote or numbers not found in the source";
      removed.appendChild(el("p", null, (f.text || (f.metric + " " + f.value)) + " [" + why + "]"));
    }
    node.appendChild(removed);
  }
  if (section.note && status !== "ok" && !/passages chosen by/i.test(section.note)) { node.appendChild(el("p", "sec-status", section.note)); }
  return node;
}

function docNode(label, doc) {
  var node = el("div", "doc");
  if (doc.status !== "ok") {
    node.appendChild(el("h3", null, label + ": not available"));
    node.appendChild(el("p", "d-meta", doc.reason || ""));
    (doc.skipped || []).forEach(function (item) { node.appendChild(el("p", "d-note", "Not used: " + (item.label || "listed document") + " on " + (item.host || "unknown host") + " · " + item.state)); });
    return node;
  }
  node.appendChild(el("h3", null, doc.title));
  var dated = doc.date_in_text ? "Dated " + doc.date_in_text : "Listed as " + (doc.date_label || "undated");
  node.appendChild(el("p", "d-meta", label + " · " + dated + " · " + doc.host));
  var fetchNote = doc.fetch.source === "fetched_now" ? "Fetched just now." : "Reused from the server cache, fetched " + fmtTime(doc.fetch.fetched_at) + ".";
  var ex = doc.extraction;
  node.appendChild(el("p", "d-note", fetchNote + " Text read from " + ex.pages_extracted + " of " + ex.page_count + " pages" + (ex.state === "ok" ? "." : " (" + ex.state + ").")));
  if (doc.source && doc.source.kind === "company_website") { node.appendChild(el("p", "d-note", "! Hosted on the company's own website, not an exchange.")); }
  var link = safeLink(doc.url, "Open original document ↗");
  if (link) { node.appendChild(link); }
  (doc.skipped || []).forEach(function (item) { node.appendChild(el("p", "d-note", "Not used: " + (item.label || "listed document") + " on " + (item.host || "unknown host") + " · " + item.state)); });
  return node;
}

function jumpNav(items) {
  var nav = el("nav", "jump");
  nav.setAttribute("aria-label", "Jump to a section");
  items.forEach(function (item) {
    var link = el("a", null, item[1]);
    link.href = "#" + item[0];
    link.addEventListener("click", function (event) {
      event.preventDefault();
      var target = byId(item[0]);
      if (target) { target.scrollIntoView({ block: "start" }); target.setAttribute("tabindex", "-1"); target.focus({ preventScroll: true }); }
    });
    nav.appendChild(link);
  });
  return nav;
}

function noticeNode(title, text, bad, retrySymbol) {
  var notice = el("div", bad ? "notice bad" : "notice");
  add(notice, el("b", null, title), el("span", null, text));
  if (retrySymbol) {
    var actions = el("div", "actions");
    actions.appendChild(button("Retry fresh research", "btn", function () { startResearch(retrySymbol); }, "Retry fresh research for " + retrySymbol));
    notice.appendChild(actions);
  }
  return notice;
}

function renderBrief(box, brief, prov) {
  clear(box);
  var company = brief.company || {};
  var symbol = company.nse_symbol || brief.ticker;
  if (prov.kind === "device") {
    box.appendChild(noticeNode("! Showing a saved result, not fresh research",
      "Fresh research failed: " + prov.failure + " This is the last result saved on this device, so nothing here is newer than " + fmtTime((brief.generated_at) || "") + ".", false, prov.retry));
  }
  var head = el("header", "r-head");
  head.appendChild(el("p", "kicker", "NSE · " + symbol));
  head.appendChild(el("h2", "serif", company.name || symbol));
  var line = el("p", "prov", prov.line);
  head.appendChild(line);
  box.appendChild(head);

  if (brief.outcome && brief.outcome !== "complete") {
    var found = (brief.sections_found || []).length;
    box.appendChild(noticeNode(brief.outcome === "partial" ? "! Partial: " + found + " of 3 sections found" : "✕ No section could be produced",
      "Missing parts say why below. Nothing was filled in.", false));
  }

  box.appendChild(jumpNav([["sec-business", "Business"], ["sec-outlook", "Outlook"], ["sec-financials", "Financials"], ["sec-sources", "Sources"]]));
  box.appendChild(briefSection(brief, "sec-business", "Business", brief.sections.overview));
  box.appendChild(briefSection(brief, "sec-outlook", "Outlook", brief.sections.outlook));
  box.appendChild(briefSection(brief, "sec-financials", "Financials", brief.sections.numbers));

  var sources = sectionShell("sec-sources", "Sources", null, false);
  sources.appendChild(docNode("Annual report", brief.documents.annual_report));
  sources.appendChild(docNode("Earnings call", brief.documents.concall));
  (brief.host_checks || []).forEach(function (hc) {
    if (!hc.allowed) { sources.appendChild(el("p", "d-note", "Transcript not fetched (" + hc.label + ", " + hc.host + "): " + hc.reason)); }
  });
  box.appendChild(sources);

  var about = el("details", "about");
  about.appendChild(el("summary", null, "About this analysis"));
  (brief.limitations || []).forEach(function (text) { about.appendChild(el("p", null, text)); });
  if (brief.notes && brief.notes.length) { about.appendChild(el("p", null, brief.notes.join(" "))); }
  if (brief.total_seconds !== undefined) { about.appendChild(el("p", null, "Took " + brief.total_seconds + " s when it was generated.")); }
  about.appendChild(el("p", null, brief.disclaimer || ""));
  box.appendChild(about);
}

function docLabelFromId(docId, short) {
  var id = docId || "";
  var year = (id.match(/(20\d\d)/) || [])[1];
  var month = (id.match(/(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*/) || [])[1];
  if (/CONCALL/.test(id)) { return { kind: "Earnings call", date: month ? month.charAt(0) + month.slice(1).toLowerCase() + " " + year : (year || null) }; }
  if (/_AR_/.test(id)) { return { kind: "Annual report", date: year ? "Financial year " + year : null }; }
  return { kind: short || "Document", date: year || null };
}

// Saved answers written by the builder (never model output). Mapped to the section they answer.
function renderOperator(box, symbol, records, prov) {
  clear(box);
  var entry = null;
  if (state.digest) {
    state.digest.portfolio.forEach(function (e) { if (e.symbol === symbol) { entry = e; } });
  }
  var newest = records.map(function (r) { return r.generated_at; }).sort().pop();
  box.appendChild(noticeNode("! Showing saved answers, not fresh research",
    prov.why + " These answers were written by the builder from the documents below on " + (newest || "an earlier date") +
    ". They are not model output, and they cover only two fixed questions.", false, prov.retry));
  var head = el("header", "r-head");
  head.appendChild(el("p", "kicker", "NSE · " + symbol));
  head.appendChild(el("h2", "serif", (entry && entry.name) || symbol));
  head.appendChild(el("p", "prov", "Saved answers · written by the builder · not model output"));
  box.appendChild(head);
  box.appendChild(jumpNav([["sec-business", "Business"], ["sec-outlook", "Outlook"], ["sec-financials", "Financials"], ["sec-sources", "Sources"]]));

  var byId_ = { outlook: [], financials: [], other: [] };
  records.forEach(function (r) {
    if (/guidance|outlook/i.test(r.id)) { byId_.outlook.push(r); }
    else if (/number|financ|headline/i.test(r.id)) { byId_.financials.push(r); }
    else { byId_.other.push(r); }
  });
  var biz = sectionShell("sec-business", "Business", "No saved answer exists for this part. Run fresh research to get it.", false);
  box.appendChild(biz);
  [["sec-outlook", "Outlook", byId_.outlook], ["sec-financials", "Financials", byId_.financials]].forEach(function (spec) {
    var section = sectionShell(spec[0], spec[1], spec[2].length ? null : "No saved answer exists for this part. Run fresh research to get it.", false);
    spec[2].forEach(function (record) { section.appendChild(operatorAnswer(record)); });
    box.appendChild(section);
  });
  if (byId_.other.length) {
    var others = sectionShell("sec-other", "Other saved answers", null, false);
    byId_.other.forEach(function (record) { others.appendChild(operatorAnswer(record)); });
    box.appendChild(others);
  }
  var sources = sectionShell("sec-sources", "Sources", null, false);
  records.forEach(function (record) {
    var label = docLabelFromId(record.doc, record.short);
    var doc = el("div", "doc");
    doc.appendChild(el("h3", null, label.kind + (label.date ? ", " + label.date : "")));
    doc.appendChild(el("p", "d-meta", "Used for: " + (record.short || record.id)));
    var link = safeLink(record.source_url, "Open original document ↗");
    if (link) { doc.appendChild(link); }
    sources.appendChild(doc);
  });
  box.appendChild(sources);
  var about = el("details", "about");
  about.appendChild(el("summary", null, "About these saved answers"));
  records.forEach(function (record) { about.appendChild(el("p", null, (record.short || record.id) + ": " + (record.verification || record.label || ""))); });
  about.appendChild(el("p", null, "Passages are retrieved excerpts, not the whole document, so a saved answer may omit relevant statements."));
  box.appendChild(about);
  jumpToResult();
}

function operatorAnswer(record) {
  var wrap = el("div");
  var result = record.result || {};
  wrap.appendChild(el("p", "sec-status", "Question: " + record.question));
  wrap.appendChild(el("p", "answer", result.answer || "No answer text."));
  if (result.not_stated) { wrap.appendChild(el("p", "sec-status", "The passages do not state an answer to this question.")); }
  var label = docLabelFromId(record.doc, record.short);
  var passages = record.passages || [];
  (result.claims || []).forEach(function (claim) {
    var passage = null;
    passages.forEach(function (p) { if (p.source_id === claim.source) { passage = p; } });
    wrap.appendChild(claimNode(claim.text, {
      kind: label.kind, date: label.date, page: passage ? passage.page : "?", speaker: passage && passage.speaker ? passage.speaker : null,
      url: record.source_url, title: record.short, quote: claim.quote || "", context: null, passage: passage ? passage.text : null
    }, null, { operator: true }));
  });
  return wrap;
}

// ------------------------------------------------------------------ guidance versus outcome

function loadGuidance() {
  state.guidanceLoaded = true;
  fetchJson("/api/guidance", {}, 15000, 3)
    .then(function (result) {
      var box = byId("r-guidance");
      clear(box);
      if (!result.ok || !result.body.examples || result.body.examples.length === 0) { return; }
      var wrap = el("section", "gcard");
      wrap.setAttribute("aria-labelledby", "g-h");
      var h = el("h2", "serif", "Worked example: guidance versus outcome");
      h.id = "g-h";
      wrap.appendChild(h);
      wrap.appendChild(el("p", "hint", "A saved example on a different company, shown to explain how FINN compares what management guided with what it later reported. It is not part of any research result above."));
      result.body.examples.forEach(function (example) { wrap.appendChild(guidanceNode(example)); });
      var checks = el("details", "ev-more");
      checks.appendChild(el("summary", null, "What was checked, and what was not"));
      checks.appendChild(el("p", "ev-note", "Checked: " + (result.body.verified_by || "")));
      checks.appendChild(el("p", "ev-note", "Not verified: " + (result.body.not_verified || "")));
      wrap.appendChild(checks);
      wrap.appendChild(el("p", "hint", "A comparison is only drawn when metric, period, unit and scope all match and are stated. Otherwise it says so."));
      box.appendChild(wrap);
    })
    .catch(function () { state.guidanceLoaded = false; });
}

function pct(low, high) {
  return low !== null && high !== null && low !== undefined ? low + "%–" + high + "%" : "";
}

function guidanceNode(example) {
  var wrap = el("div");
  var g = example.guidance;
  var o = example.outcome;
  var pair = el("div", "g-pair");
  var gs = el("div", "g-side");
  add(gs, el("p", "lbl", "Management guided · call of " + fmtDay(g.call_date)),
    el("p", "big", pct(example.guided_range.low, example.guided_range.high) || g.quote),
    el("p", "src", g.metric.label + ", " + g.period.key.replace("_", " ") + " · PDF page " + g.pdf_page));
  var os = el("div", "g-side");
  add(os, el("p", "lbl", "Reported · call of " + fmtDay(o.call_date)),
    el("p", "big", example.reported_value !== null && example.reported_value !== undefined ? example.reported_value + "%" : o.quote),
    el("p", "src", o.metric.label + ", " + o.period.label + " · PDF page " + o.pdf_page));
  add(pair, gs, os);
  wrap.appendChild(el("p", "g-title", example.company + ": " + example.title));
  wrap.appendChild(pair);

  var gate = example.gate;
  var verdict = el("div", gate.verdict === "comparable" ? "verdict ok" : "verdict");
  if (gate.verdict === "comparable") {
    verdict.appendChild(el("b", null, "✓ Comparable: metric, period, unit and scope match"));
    if (gate.position) { verdict.appendChild(el("span", null, "The reported figure is " + gate.position + " the guided range.")); }
  } else {
    verdict.appendChild(el("b", null, "◇ Not directly comparable"));
    var list = el("ul");
    (gate.reasons || []).forEach(function (reason) {
      var text = reason.why;
      if (reason.guidance && reason.outcome && /different/.test(reason.why)) {
        text = reason.field.charAt(0).toUpperCase() + reason.field.slice(1) + " differs: guided for the " + reason.guidance + "; reported for the " + reason.outcome + ".";
      } else {
        text = reason.field.charAt(0).toUpperCase() + reason.field.slice(1) + ": " + reason.why.replace(/^[a-z]+ is not stated/, "not stated") + ".";
      }
      list.appendChild(el("li", null, text));
    });
    verdict.appendChild(list);
  }
  wrap.appendChild(verdict);
  if (example.management_framing) { wrap.appendChild(el("p", "hint", example.management_framing)); }

  var more = el("details", "ev-more");
  more.appendChild(el("summary", null, "Evidence and sources"));
  [["Guidance", g], ["Reported outcome", o]].forEach(function (pairItem) {
    var side = pairItem[1];
    var ev = el("div", "evidence");
    ev.appendChild(el("p", "ev-label", pairItem[0] + " · call of " + fmtDay(side.call_date)));
    ev.appendChild(el("p", "ev-ctx", side.title));
    ev.appendChild(el("blockquote", null, "“" + side.quote + "”"));
    ev.appendChild(el("p", "ev-note", (side.quote_verbatim_in_stored_page ? "✓ verbatim" : "✕ NOT found") + " on PDF page " + side.pdf_page + " · metric: " + side.metric.label + " · period: " + side.period.label + " · unit: " + side.unit.label + " · scope: " + side.scope.label));
    (side.extra_quotes || []).forEach(function (extra) { ev.appendChild(el("p", "ev-ctx", "Also on page " + extra.pdf_page + (extra.verbatim ? " (✓ verbatim)" : " (✕ not found)") + ": “" + extra.quote + "”")); });
    var link = safeLink(side.url, "Open original document ↗");
    if (link) { ev.appendChild(link); }
    if (side.url_note) { ev.appendChild(el("p", "ev-note", side.url_note)); }
    if (side.page_text) {
      var page = el("details", "ev-more");
      page.appendChild(el("summary", null, "Show the page text"));
      var holder = el("p");
      highlightInto(holder, side.page_text, side.quote);
      page.appendChild(holder);
      ev.appendChild(page);
    }
    more.appendChild(ev);
  });
  (gate.caveats || []).forEach(function (c) { more.appendChild(el("p", "ev-note", "Note: " + c)); });
  wrap.appendChild(more);
  return wrap;
}

// ------------------------------------------------------------------ portfolio (one selection per demo name, in this browser only)
// The server never stores a selection. Each request carries the ticker list it should work on, and the answer is
// computed for that list only. Clearing browser data, or using another browser or device, starts again from the demo list.

var PORTFOLIO_PREFIX = "finn.portfolio.";
var MAX_PORTFOLIO = 40;
var SYMBOL_RE = /^[A-Z0-9&\-]{1,20}$/;
var QUERY_RE = /^[A-Za-z0-9 &.\-]{2,30}$/;

var COV = {
  available: { glyph: "●", cls: "st-live", word: "Available" },
  loading: { glyph: "↻", cls: "st-snap", word: "Loading" },
  unavailable: { glyph: "○", cls: "st-snap", word: "Unavailable" },
  failed: { glyph: "▲", cls: "st-failed", word: "Refresh failed" }
};

state.add = { busy: false };
state.searchSerial = 0;
state.searchTimer = null;

function portfolioKey(name) { return PORTFOLIO_PREFIX + encodeURIComponent(cleanName(name).toLowerCase()); }

function readPortfolio(name) {
  var raw = storeGet(portfolioKey(name));
  if (!raw) { return null; }
  try {
    var saved = JSON.parse(raw);
    if (!saved || !Array.isArray(saved.symbols)) { return null; }
    var seen = {};
    var list = [];
    saved.symbols.forEach(function (item) {
      var symbol = item && typeof item.symbol === "string" ? item.symbol.toUpperCase() : "";
      if (SYMBOL_RE.test(symbol) && !seen[symbol] && list.length < MAX_PORTFOLIO) {
        seen[symbol] = true;
        list.push({ symbol: symbol, name: typeof item.name === "string" ? item.name.slice(0, 80) : null });
      }
    });
    return { symbols: list };
  } catch (e) { return null; }  // a damaged value is ignored; the demo list is used instead
}

function savePortfolio() {
  var p = state.portfolio;
  if (!p || !state.profile) { return; }
  var key = portfolioKey(state.profile.name);
  if (p.mode === "demo") {
    storeRemove(key);  // the demo list is the default; nothing needs saving
    p.persisted = true;
    return;
  }
  p.persisted = storeSet(key, JSON.stringify({ v: 1, symbols: p.symbols, updated: new Date().toISOString() }));
}

function loadDemoList() {
  if (state.demoList) { return Promise.resolve(state.demoList); }
  return fetchJson("/api/portfolio/demo", {}, 12000, 3).then(function (result) {
    if (!result.ok || !result.body || !Array.isArray(result.body.symbols)) { throw new Error("HTTP " + result.status); }
    state.demoList = result.body.symbols
      .filter(function (item) { return item && SYMBOL_RE.test(String(item.symbol)); })
      .map(function (item) { return { symbol: item.symbol, name: item.name || null }; });
    return state.demoList;
  });
}

function initPortfolio() {
  state.portfolioError = null;
  var stored = state.profile ? readPortfolio(state.profile.name) : null;
  if (stored) {
    state.portfolio = { mode: "custom", symbols: stored.symbols, persisted: true };
    return Promise.resolve();
  }
  return loadDemoList().then(function (list) {
    state.portfolio = { mode: "demo", symbols: list.map(function (item) { return { symbol: item.symbol, name: item.name }; }), persisted: true };
  }).catch(function (error) {
    state.portfolio = null;
    state.portfolioError = error.message || "failed";
  });
}

function currentSymbols() {
  return state.portfolio ? state.portfolio.symbols.map(function (item) { return item.symbol; }) : [];
}

function inPortfolio(symbol) { return currentSymbols().indexOf(symbol) >= 0; }

function storedName(symbol) {
  if (!state.portfolio) { return null; }
  for (var i = 0; i < state.portfolio.symbols.length; i++) {
    if (state.portfolio.symbols[i].symbol === symbol) { return state.portfolio.symbols[i].name; }
  }
  return null;
}

function rememberName(symbol, name) {
  if (!state.portfolio || !name) { return; }
  var changed = false;
  state.portfolio.symbols.forEach(function (item) {
    if (item.symbol === symbol && !item.name) { item.name = String(name).slice(0, 80); changed = true; }
  });
  if (changed && state.portfolio.mode === "custom") { savePortfolio(); }
}

function entryFor(symbol) {
  if (!state.digest) { return null; }
  for (var i = 0; i < state.digest.portfolio.length; i++) {
    if (state.digest.portfolio[i].symbol === symbol) { return state.digest.portfolio[i]; }
  }
  return null;
}

function liveRowFor(symbol) {
  var rows = state.liveRows || [];
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].symbol === symbol && rows[i].ok) { return rows[i]; }
  }
  return null;
}

function displayName(symbol) {
  var entry = entryFor(symbol);
  var row = liveRowFor(symbol);
  return (entry && entry.name) || storedName(symbol) || (row && row.name) || "";
}

// ---- coverage: what FINN actually has for each company, per source. Words: available, loading, unavailable, refresh failed.

function priceCoverage(symbol) {
  var entry = entryFor(symbol);
  var row = liveRowFor(symbol);
  var source = row || (entry && entry.close !== null && entry.close !== undefined ? entry : null);
  var code = state.priceFail[symbol];
  if (source) {
    if (code) { return { s: "failed", text: "Refresh failed (" + code + "). Showing the end-of-day close of " + fmtBarDate(source.last_date) + "." }; }
    if (row) { return { s: "available", text: "End-of-day close of " + fmtBarDate(row.last_date) + ", checked on Yahoo " + fmtTime(row.fetched_utc || state.liveFetchedUtc) + (row.partial ? " (session open, intraday bar)" : "") + "." }; }
    if (entry.price_source === "live_cache") { return { s: "available", text: "End-of-day close of " + fmtBarDate(entry.last_date) + ", Yahoo, fetched " + fmtTime(entry.price_fetched_utc) + "." }; }
    return { s: "available", text: "Saved end-of-day close of " + fmtBarDate(entry.last_date) + ", not live." };
  }
  if (state.priceLoading[symbol] || !state.digest || state.digestLoading) { return { s: "loading", text: "Fetching the price from Yahoo…" }; }
  if (code === "not_found" || code === "not enough bars") { return { s: "unavailable", text: "Yahoo has no usable price history for this ticker." }; }
  if (code) { return { s: "failed", text: "The price fetch failed (" + code + "). No price to show." }; }
  return { s: "unavailable", text: "No price data yet." };
}

function filingsCoverage(symbol) {
  var fs = state.digest ? state.digest.filings_status : null;
  if (!fs) { return { s: "loading", text: "Loading…" }; }
  var st = fs.symbols[symbol];
  if (!st) { return state.digestLoading ? { s: "loading", text: "Loading…" } : { s: "unavailable", text: "No filings data." }; }
  if (st.refreshing) { return { s: "loading", text: "Checking NSE now…" }; }
  var at = st.fetched_utc || (st.attempt_utc ? st.attempt_utc : null);
  switch (st.state) {
    case "live": return { s: "available", text: "Live from NSE, " + fmtTime(st.fetched_utc) + "." };
    case "valid_empty": return { s: "available", text: "Checked live " + fmtTime(st.fetched_utc) + ": no filings in the last 7 days." };
    case "snapshot": return { s: "available", text: "Saved copy from " + dated(fs.snapshot_fetched_utc) + ", not live." };
    case "ambiguous_empty": return { s: "unavailable", text: "NSE returned nothing, even over 90 days. That cannot be read as no filings." };
    case "stale_live": return { s: "failed", text: "Refresh failed. Showing older live data" + (st.fetched_utc ? " from " + fmtTime(st.fetched_utc) : "") + "." };
    case "failed_snapshot": return { s: "failed", text: "Refresh failed. Showing the saved copy from " + fmtTime(fs.snapshot_fetched_utc) + "." };
    case "failed": return { s: "failed", text: "The NSE check failed" + (at ? " (" + fmtTime(at) + ")" : "") + ". There is no saved copy, so FINN has no filings data for this company." };
    case "unchecked":
      if (fs.running || state.filingsPending[symbol]) { return { s: "loading", text: "Waiting for the NSE check…" }; }
      return { s: "unavailable", text: "Not checked yet. FINN has no filings data for this company until a live check runs." };
    default: return { s: "unavailable", text: st.state };
  }
}

function coverageFor(symbol) {
  var p = priceCoverage(symbol);
  var f = filingsCoverage(symbol);
  var overall = "available";
  ["loading", "failed", "unavailable"].forEach(function (name) {
    if (overall === "available" && (p.s === name || f.s === name)) { overall = name; }
  });
  return { prices: p, filings: f, overall: overall };
}

function coverageSummary() {
  var counts = { available: 0, loading: 0, unavailable: 0, failed: 0 };
  currentSymbols().forEach(function (symbol) { counts[coverageFor(symbol).overall] += 1; });
  return counts;
}

function filingsGap() {
  // Companies whose filings are unknown (not checked, failed, or unverifiable), so an empty feed cannot mean "none".
  if (!state.digest) { return []; }
  return currentSymbols().filter(function (symbol) {
    var s = filingsCoverage(symbol);
    return s.s !== "available" && !(s.s === "failed" && state.digest.filings_status.symbols[symbol] && state.digest.filings_status.symbols[symbol].in_snapshot);
  });
}

function coverageWords(counts) {
  var parts = [];
  if (counts.loading) { parts.push(counts.loading + " loading"); }
  if (counts.unavailable) { parts.push(counts.unavailable + " unavailable"); }
  if (counts.failed) { parts.push(counts.failed + " refresh failed"); }
  return parts;
}

function renderCoverage() {
  var box = byId("coverage");
  clear(box);
  if (!state.digest || !state.portfolio || state.portfolio.symbols.length === 0) { box.hidden = true; return; }
  var counts = coverageSummary();
  var total = currentSymbols().length;
  var parts = coverageWords(counts);
  box.hidden = false;
  var words = el("span");
  add(words, el("b", null, "Coverage "), document.createTextNode(counts.available + " of " + total + " companies available" + (parts.length ? " · " + parts.join(" · ") : "") + "."));
  box.appendChild(words);
  var more = button("Details", "linkbtn", function () { showView("portfolio"); }, "Open the Portfolio view to see each company's coverage");
  box.appendChild(more);
}

// ---- empty and error states

function emptyPortfolioNode(onToday) {
  var box = el("div", "pf-emptybox");
  add(box, el("h2", "serif", "Your portfolio is empty"));
  add(box, el("p", null, "Today shows filings and prices only for the companies in your portfolio, so there is nothing to show yet."));
  var actions = el("div", "p-actions");
  if (onToday) { add(actions, button("Add companies", "btn primary", function () { showView("portfolio"); byId("pf-input").focus(); })); }
  add(actions, button("Reset to the demo portfolio", "btn", function () { openResetDemo(); }));
  box.appendChild(actions);
  return box;
}

function demoLoadFailedNode() {
  var box = el("div", "pf-emptybox");
  add(box, el("h2", "serif", "Could not load the demo portfolio"));
  add(box, el("p", null, "FINN could not reach the server for the starting list (" + (state.portfolioError || "no answer") + "). Nothing has been changed."));
  var actions = el("div", "p-actions");
  add(actions, button("Retry", "btn primary", function () { retryPortfolio(); }));
  box.appendChild(actions);
  return box;
}

function retryPortfolio() {
  initPortfolio().then(function () {
    renderToday();
    renderPortfolio();
    return loadDigest();
  });
}

// ---- the Portfolio view

function setMsg(text, kind) {
  var node = byId("pf-msg");
  if (!text) { node.hidden = true; node.textContent = ""; return; }
  node.textContent = text;
  node.className = "pf-msg " + (kind || "info");
  node.hidden = false;
}

function setAddBusy(busy) {
  state.add.busy = busy;
  var go = byId("pf-add");
  go.disabled = busy;
  go.textContent = busy ? "Checking…" : "Add";
  byId("pf-input").readOnly = busy;
}

function renderPortfolio() {
  var summary = byId("pf-summary");
  var list = byId("pf-list");
  if (!summary || byId("view-portfolio").hidden) { return; }
  clear(summary);
  clear(list);
  if (!state.portfolio) {
    list.appendChild(demoLoadFailedNode());
    return;
  }
  var p = state.portfolio;
  var total = p.symbols.length;
  var head = el("div", "pf-head");
  add(head, el("b", "pf-count", total + (total === 1 ? " company" : " companies")),
    el("span", "status", p.mode === "demo" ? "Demo portfolio" : "Your selection"));
  summary.appendChild(head);
  var about = p.mode === "demo"
    ? "The 16 companies chosen by the builder. Add or remove any to make it your own."
    : "Your own selection, saved in this browser for " + (state.profile ? "“" + state.profile.name + "”" : "this name") + " only.";
  summary.appendChild(el("p", "hint", about));
  if (!p.persisted) {
    summary.appendChild(el("p", "inline-error", "This browser blocks storage, so your selection will be lost when you reload."));
  }
  if (total > 0) {
    var counts = coverageSummary();
    var parts = coverageWords(counts);
    var line = el("p", "pf-cov-line");
    add(line, el("b", null, "Coverage "), document.createTextNode(counts.available + " of " + total + " available" + (parts.length ? " · " + parts.join(" · ") : "")));
    summary.appendChild(line);
  }
  var actions = el("div", "p-actions");
  var refreshAll = button("Refresh all", "btn", function () { refreshFilings(); refreshPrices(); }, "Refresh filings and prices for every company in the portfolio");
  refreshAll.disabled = total === 0;
  add(actions, refreshAll);
  if (p.mode === "custom") { add(actions, button("Reset to demo", "btn", openResetDemo)); }
  if (total > 0) { add(actions, button("Remove all", "btn danger", openRemoveAllCompanies)); }
  summary.appendChild(actions);

  if (total === 0) {
    list.appendChild(emptyPortfolioNode(false));
    return;
  }
  p.symbols.forEach(function (item) { list.appendChild(portfolioRow(item)); });
}

function covLine(label, cov) {
  var meta = COV[cov.s];
  var line = el("p", "pf-covrow");
  var text = el("span");
  add(text, el("b", null, label + " " + meta.word.toLowerCase() + ". "), el("span", "fr-detail", cov.text));
  add(line, el("span", "g " + meta.cls, meta.glyph), text);
  return line;
}

function portfolioRow(item) {
  var symbol = item.symbol;
  var cov = coverageFor(symbol);
  var row = el("article", "pf-row");
  row.setAttribute("data-symbol", symbol);
  var top = el("div", "pf-top");
  var id = el("div", "pf-id");
  add(id, el("span", "sym", symbol), el("span", "nm", displayName(symbol)));
  add(top, id);
  var entry = entryFor(symbol);
  var stats = entry ? currentStats(entry) : liveRowFor(symbol);
  if (stats && stats.close !== null && stats.close !== undefined) {
    var change = stats.change_pct;
    var price = el("div", "pf-price");
    add(price, el("span", "num", "₹" + formatNumber(stats.close, 2)),
      el("span", "chg " + (change > 0 ? "up" : (change < 0 ? "down" : "")), change === null || change === undefined ? "–" : arrow(change) + signed(change, 2) + "%"));
    top.appendChild(price);
  }
  row.appendChild(top);
  row.appendChild(covLine("Filings", cov.filings));
  row.appendChild(covLine("Prices", cov.prices));
  var actions = el("div", "pf-actions");
  add(actions, button("Research", "linkbtn", function () { researchCompany(symbol, null); }, "Research " + symbol));
  if (cov.overall === "failed" || cov.overall === "unavailable") {
    add(actions, button("Retry", "linkbtn", function () { state.autoChecked[symbol] = true; refreshFilings([symbol]); refreshPrices([symbol]); }, "Retry the data checks for " + symbol));
  }
  add(actions, button("Remove", "btn small", function () { removeCompany(symbol); }, "Remove " + symbol + " from the portfolio"));
  row.appendChild(actions);
  return row;
}

// ---- add

function onPortfolioInput() {
  setMsg("");
  var text = byId("pf-input").value.trim();
  window.clearTimeout(state.searchTimer);
  state.searchSerial = state.searchSerial + 1;
  if (text.length < 2 || !QUERY_RE.test(text)) { clear(byId("pf-results")); return; }
  var serial = state.searchSerial;
  state.searchTimer = window.setTimeout(function () { runSearch(text, serial); }, 350);
}

function searchNote(text, bad) {
  var box = byId("pf-results");
  clear(box);
  box.appendChild(el("p", bad ? "pf-note bad" : "pf-note", text));
}

function runSearch(text, serial) {
  searchNote("Searching NSE companies…", false);
  fetchJson("/api/symbols/search?q=" + encodeURIComponent(text), {}, 10000, 1)
    .then(function (result) {
      if (serial !== state.searchSerial) { return; }
      if (!result.ok) {
        searchNote("Name search is unavailable right now. You can still type the exact NSE ticker and press Add; FINN will confirm it first.", true);
        return;
      }
      var found = result.body.results || [];
      var box = byId("pf-results");
      clear(box);
      if (found.length === 0) {
        searchNote("No NSE company matched “" + text + "”. Check the spelling, or type the exact ticker and press Add.", false);
        return;
      }
      found.forEach(function (item) {
        var has = inPortfolio(item.symbol);
        var row = el("button", "pf-result");
        row.type = "button";
        row.setAttribute("role", "listitem");
        row.setAttribute("aria-label", (has ? item.symbol + ", already in your portfolio" : "Add " + item.symbol + ", " + item.name));
        var left = el("span");
        add(left, el("span", "sym", item.symbol), el("span", "nm", item.name));
        add(row, left, el("span", has ? "pf-in" : "pf-plus", has ? "✓ In portfolio" : "+ Add"));
        if (has) { row.disabled = true; }
        row.addEventListener("click", function () { addCompany(item.symbol, item.name); });
        box.appendChild(row);
      });
    })
    .catch(function () {
      if (serial !== state.searchSerial) { return; }
      searchNote("Name search is unavailable right now. You can still type the exact NSE ticker and press Add; FINN will confirm it first.", true);
    });
}

function onAddSubmit(event) {
  event.preventDefault();
  if (state.add.busy) { return; }
  var text = byId("pf-input").value.trim();
  if (!text) { setMsg("Enter a company name or an NSE ticker.", "bad"); byId("pf-input").focus(); return; }
  var symbol = text.toUpperCase();
  window.clearTimeout(state.searchTimer);
  state.searchSerial = state.searchSerial + 1;
  clear(byId("pf-results"));
  if (!SYMBOL_RE.test(symbol)) {
    setMsg("“" + text + "” is not an NSE ticker. Pick a company from the search results, or type its exact ticker (letters, digits, & or -).", "bad");
    return;
  }
  addCompany(symbol, null);
}

function addFailed(symbol, text) {
  setAddBusy(false);
  setMsg(text, "bad");
}

function addCompany(symbol, hintName) {
  if (!state.portfolio || state.add.busy) { return; }
  if (inPortfolio(symbol)) { setMsg(symbol + " is already in your portfolio.", "bad"); return; }
  if (state.portfolio.symbols.length >= MAX_PORTFOLIO) { setMsg("A portfolio holds up to " + MAX_PORTFOLIO + " companies. Remove one to add another.", "bad"); return; }
  setAddBusy(true);
  setMsg("Checking " + symbol + " on NSE…", "info");
  var owner = state.profile ? state.profile.name : null;
  fetchJson("/api/symbols/check?symbol=" + encodeURIComponent(symbol), {}, 12000, 2)
    .then(function (result) {
      if (!state.profile || state.profile.name !== owner) { setAddBusy(false); return; }  // signed out or switched while checking
      var body = result.body || {};
      if (result.status === 422) { addFailed(symbol, "“" + symbol + "” is not a valid NSE ticker. Nothing was added."); return; }
      if (result.ok && body.state === "valid") {
        setAddBusy(false);
        finishAdd(symbol, body.name || hintName || null);
        return;
      }
      if (result.ok && (body.state === "not_found" || body.state === "invalid_format")) {
        addFailed(symbol, symbol + " was not found as an NSE-listed company. Nothing was added. Search by name, or check the ticker.");
        return;
      }
      addFailed(symbol, "FINN could not confirm " + symbol + " right now" + (body.error ? " (" + body.error + ")" : "") + ", so it was not added. Try again in a moment.");
    })
    .catch(function () {
      addFailed(symbol, "FINN could not reach the server to confirm " + symbol + ", so it was not added. Try again in a moment.");
    });
}

function finishAdd(symbol, name) {
  if (inPortfolio(symbol)) { setMsg(symbol + " is already in your portfolio.", "bad"); return; }
  state.portfolio.mode = "custom";
  state.portfolio.symbols.unshift({ symbol: symbol, name: name });
  clearUndo();
  byId("pf-input").value = "";
  window.clearTimeout(state.searchTimer);
  state.searchSerial = state.searchSerial + 1;  // a search still pending for the text just added must not repopulate the list
  clear(byId("pf-results"));
  delete state.priceFail[symbol];
  delete state.autoPrice[symbol];
  delete state.autoChecked[symbol];
  portfolioChanged();
  setMsg("Added " + symbol + (name ? " (" + name + ")" : "") + ". Its prices and filings are being checked now.", "ok");
  byId("pf-input").focus();
}

// ---- remove, undo, reset

function clearUndo() {
  if (state.undo && state.undo.timer) { window.clearTimeout(state.undo.timer); }
  state.undo = null;
  var box = byId("pf-undo");
  box.hidden = true;
  clear(box);
}

function showUndo() {
  var box = byId("pf-undo");
  clear(box);
  if (!state.undo) { box.hidden = true; return; }
  box.hidden = false;
  var u = state.undo;
  add(box, el("span", null, u.text + " "), button("Undo", "linkbtn", undoRemove, "Undo: " + u.text));
}

function removeCompany(symbol) {
  if (!state.portfolio) { return; }
  var index = currentSymbols().indexOf(symbol);
  if (index < 0) { return; }
  var removed = state.portfolio.symbols.splice(index, 1);
  clearUndo();
  setMsg("");
  state.undo = { items: removed.map(function (item) { return { item: item, index: index }; }), text: "Removed " + symbol + ".", timer: null };
  state.undo.timer = window.setTimeout(clearUndo, 12000);
  state.portfolio.mode = "custom";
  portfolioChanged();
  showUndo();
  byId("pf-input").focus();
}

function undoRemove() {
  var u = state.undo;
  if (!u || !state.portfolio) { return; }
  u.items.slice().sort(function (a, b) { return a.index - b.index; }).forEach(function (entry) {
    if (!inPortfolio(entry.item.symbol)) {
      state.portfolio.symbols.splice(Math.min(entry.index, state.portfolio.symbols.length), 0, entry.item);
    }
  });
  clearUndo();
  portfolioChanged();
  setMsg("Restored.", "ok");
}

function openRemoveAllCompanies() {
  var count = currentSymbols().length;
  openSheet("Remove all " + count + " companies?", function (body) {
    body.appendChild(el("p", null, "Today will be empty until you add companies or reset to the demo portfolio. Research on your device is not affected."));
    var actions = el("div", "sheet-actions");
    add(actions, button("Cancel", "btn", closeSheet), button("Remove all", "btn danger", function () {
      var removed = state.portfolio.symbols.map(function (item, index) { return { item: item, index: index }; });
      state.portfolio.symbols = [];
      state.portfolio.mode = "custom";
      clearUndo();
      state.undo = { items: removed, text: "Removed all " + removed.length + " companies.", timer: null };
      state.undo.timer = window.setTimeout(clearUndo, 12000);
      closeSheet();
      portfolioChanged();
      showUndo();
    }));
    body.appendChild(actions);
  });
}

function openResetDemo() {
  var custom = state.portfolio && state.portfolio.mode === "custom";
  openSheet("Reset to the demo portfolio?", function (body) {
    body.appendChild(el("p", null, custom
      ? "This replaces your selection of " + currentSymbols().length + " companies with the 16 demo companies. Your selection is not kept."
      : "Your portfolio is empty. This puts back the 16 demo companies."));
    var actions = el("div", "sheet-actions");
    add(actions, button("Cancel", "btn", closeSheet), button("Reset to demo", "btn primary", function () { closeSheet(); resetToDemo(); }));
    body.appendChild(actions);
  });
}

function resetToDemo() {
  clearUndo();
  loadDemoList().then(function (list) {
    state.portfolio = { mode: "demo", symbols: list.map(function (item) { return { symbol: item.symbol, name: item.name }; }), persisted: true };
    savePortfolio();
    state.priceFail = {};
    state.autoChecked = {};
    state.autoPrice = {};
    renderPortfolio();
    setMsg("Back to the 16 demo companies.", "ok");
    state.digest = null;
    state.digestLoading = true;
    renderToday();
    loadDigest();
  }).catch(function () {
    setMsg("Could not load the demo list from the server. Your portfolio was not changed.", "bad");
    renderPortfolio();
  });
}

function portfolioChanged() {
  savePortfolio();
  state.digestFailed = false;
  if (currentSymbols().length === 0) {
    state.digest = null;
    state.digestSerial = state.digestSerial + 1;
  } else {
    state.digestLoading = true;
  }
  renderToday();
  renderPortfolio();
  if (currentSymbols().length) { loadDigest(); }
}

function movePortfolioForRename(oldName, newName) {
  // A rename keeps the selection: move it to the new name unless that name already has its own.
  var from = portfolioKey(oldName);
  var to = portfolioKey(newName);
  if (from === to) { return; }
  var raw = storeGet(from);
  if (raw && !storeGet(to)) { storeSet(to, raw); storeRemove(from); }
}

// ------------------------------------------------------------------ demo profile (device only)
// A display name saved in this browser's localStorage. No password, no account, nothing is sent to the server.

var PROFILE_KEY = "finn.profile";
state.profile = null;
state.profilePersisted = true;
state.editingName = false;

function getStoredProfile() {
  var raw = storeGet(PROFILE_KEY);
  if (!raw) { return null; }
  try {
    var saved = JSON.parse(raw);
    if (saved && typeof saved.name === "string" && cleanName(saved.name).length > 0) {
      return { name: cleanName(saved.name).slice(0, 40), created: saved.created || null };
    }
  } catch (e) { /* ignore a damaged value */ }
  return null;
}

function cleanName(raw) {
  return String(raw).replace(/[\u0000-\u001f\u007f]/g, "").replace(/\s+/g, " ").trim();
}

function nameError(name) {
  if (name.length < 1) { return "Enter a display name to continue."; }
  if (Array.from(name).length > 40) { return "Use 40 characters or fewer."; }
  return null;
}

function saveProfile(name, created) {
  state.profile = { name: name, created: created || new Date().toISOString() };
  state.profilePersisted = storeSet(PROFILE_KEY, JSON.stringify({ name: state.profile.name, created: state.profile.created, v: 1 }));
}

function initial(name) {
  var first = Array.from(name)[0] || "?";
  return first.toUpperCase();
}

function renderNavUser() {
  var box = byId("nav-user");
  clear(box);
  if (!state.profile) { return; }
  add(box, el("b", null, state.profile.name), document.createTextNode("Demo profile · this device only"));
}

function showLanding() {
  document.body.classList.add("is-landing");
  byId("shell").hidden = true;
  byId("landing").hidden = false;
  byId("skip").setAttribute("href", "#landing-main");
  byId("signin-name").value = "";
  byId("signin-error").hidden = true;
  window.scrollTo(0, 0);
}

function enterApp() {
  document.body.classList.remove("is-landing");
  byId("landing").hidden = true;
  byId("shell").hidden = false;
  byId("skip").setAttribute("href", "#main");
  renderNavUser();
  // A different name may have signed in on this page: drop the previous selection's data before loading this one's.
  state.digest = null;
  state.digestSerial = state.digestSerial + 1;
  state.digestLoading = false;
  state.digestFailed = false;
  state.priceFail = {};
  state.priceLoading = {};
  state.filingsPending = {};
  state.autoChecked = {};
  state.autoPrice = {};
  state.undo = null;
  showView("today");
  initPortfolio().then(function () {
    renderToday();
    renderPortfolio();
    return loadDigest();
  }).then(function () { renderOptions(); });
  byId("main").focus({ preventScroll: true });
}

function onSignIn(event) {
  event.preventDefault();
  var name = cleanName(byId("signin-name").value);
  var problem = nameError(name);
  var error = byId("signin-error");
  if (problem) {
    error.textContent = problem;
    error.hidden = false;
    byId("signin-name").focus();
    return;
  }
  error.hidden = true;
  saveProfile(name, null);
  enterApp();
}

function stopResearchRun() {
  window.clearTimeout(state.research.timer);
  state.research.token = state.research.token + 1;
  state.research.running = false;
  clear(byId("r-run"));
  setBusy(false);
}

function removeDeviceKeys(prefixes) {
  try {
    var doomed = [];
    for (var i = 0; i < window.localStorage.length; i++) {
      var key = window.localStorage.key(i);
      for (var j = 0; j < prefixes.length; j++) {
        if (key.indexOf(prefixes[j]) === 0) { doomed.push(key); }
      }
    }
    doomed.forEach(function (key) { window.localStorage.removeItem(key); });
  } catch (e) { /* storage unavailable: nothing to remove */ }
}

function signOut(alsoClear, everything) {
  stopResearchRun();
  var mine = state.profile ? portfolioKey(state.profile.name) : null;
  removeDeviceKeys([PROFILE_KEY]);
  if (alsoClear) {
    // Other names' portfolios on this browser stay unless the person chose to remove everything.
    removeDeviceKeys(["finn.profile", "finn.filters", "finn.recents", "finn.brief."]);
    if (everything) { removeDeviceKeys([PORTFOLIO_PREFIX]); } else if (mine) { storeRemove(mine); }
    state.filters = { hours: DEFAULT_FILTERS.hours, vol: DEFAULT_FILTERS.vol, move: DEFAULT_FILTERS.move, routine: DEFAULT_FILTERS.routine };
    state.focusFiling = null;
    clear(byId("r-result"));
    clear(byId("r-context"));
    clear(byId("r-recents"));
  }
  state.profile = null;
  state.portfolio = null;
  state.digest = null;
  state.digestSerial = state.digestSerial + 1;
  clearUndo();
  closeSheet();
  showLanding();
  byId("signin-name").focus();
}

function openSignOut() {
  openSheet("Sign out of the demo?", function (body) {
    body.appendChild(el("p", null, "This removes your display name from this browser. Nothing else changes on the server, because the server never had it."));
    var label = el("label", "check");
    var box = document.createElement("input");
    box.type = "checkbox";
    box.id = "signout-clear";
    add(label, box, el("span", null, "Also remove my saved filters, my portfolio selection, recent companies and saved research from this device"));
    body.appendChild(label);
    var actions = el("div", "sheet-actions");
    add(actions, button("Cancel", "btn", closeSheet), button("Sign out", "btn danger", function () { signOut(box.checked); }));
    body.appendChild(actions);
  });
}

function countDeviceResearch() {
  var briefs = 0;
  try {
    for (var i = 0; i < window.localStorage.length; i++) {
      if (window.localStorage.key(i).indexOf("finn.brief.") === 0) { briefs = briefs + 1; }
    }
  } catch (e) { briefs = 0; }
  return { recents: loadRecents().length, briefs: briefs };
}

function openClearResearch() {
  openSheet("Remove saved research?", function (body) {
    body.appendChild(el("p", null, "This deletes the research results and the recent-companies list stored in this browser. Your name and filters stay. Fresh research is unaffected."));
    var actions = el("div", "sheet-actions");
    add(actions, button("Cancel", "btn", closeSheet), button("Remove", "btn danger", function () {
      removeDeviceKeys(["finn.brief.", "finn.recents"]);
      closeSheet();
      renderProfile();
    }));
    body.appendChild(actions);
  });
}

function openRemoveAll() {
  openSheet("Remove all demo data?", function (body) {
    body.appendChild(el("p", null, "This signs you out and deletes everything FINN stored in this browser: every name's portfolio selection, filters, recent companies and saved research."));
    var actions = el("div", "sheet-actions");
    add(actions, button("Cancel", "btn", closeSheet), button("Remove all and sign out", "btn danger", function () { signOut(true, true); }));
    body.appendChild(actions);
  });
}

function prefRows() {
  var f = state.filters;
  var windows = { 24: "24 hours", 72: "3 days", 168: "7 days" };
  return [
    ["Filing window", windows[f.hours] + " (Today)"],
    ["Volume signal", f.vol.toFixed(1) + "× the 20-day average"],
    ["Price move", "±" + f.move + "% in a day"],
    ["Routine filings", f.routine ? "Shown" : "Hidden"]
  ];
}

function renderPrefs() {
  var box = byId("p-prefs");
  if (!box) { return; }
  clear(box);
  var list = el("dl", "p-list");
  prefRows().forEach(function (row) { add(list, el("dt", null, row[0]), el("dd", null, row[1])); });
  box.appendChild(list);
}

function renderProfile() {
  var box = byId("profile-body");
  clear(box);
  if (!state.profile) { return; }
  var profile = state.profile;

  var id = el("div", "p-id");
  var avatar = el("div", "avatar", initial(profile.name));
  avatar.setAttribute("aria-hidden", "true");
  var who = el("div");
  add(who, el("h2", "p-name", profile.name), el("span", "status", "Demo profile · this device only"));
  add(id, avatar, who);
  box.appendChild(id);

  var actions = el("div", "p-actions");
  var change = button("Change name", "btn", function () { state.editingName = true; renderProfile(); var f = byId("p-name-input"); if (f) { f.focus(); f.select(); } });
  change.id = "p-change";
  add(actions, change, button("Sign out", "btn", openSignOut));
  box.appendChild(actions);

  if (state.editingName) {
    var form = el("form", "p-form");
    form.setAttribute("aria-label", "Change display name");
    var label = el("label", "field-label", "Display name");
    label.setAttribute("for", "p-name-input");
    var input = document.createElement("input");
    input.id = "p-name-input";
    input.type = "text";
    input.maxLength = 40;
    input.autocomplete = "nickname";
    input.value = profile.name;
    input.setAttribute("aria-describedby", "p-name-error");
    var error = el("p", "inline-error");
    error.id = "p-name-error";
    error.setAttribute("role", "alert");
    error.hidden = true;
    var row = el("div", "p-actions");
    var save = button("Save name", "btn primary", null);
    save.type = "submit";
    add(row, save, button("Cancel", "btn", function () { state.editingName = false; renderProfile(); byId("p-change").focus(); }));
    add(form, label, input, error, row);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var next = cleanName(input.value);
      var problem = nameError(next);
      if (problem) { error.textContent = problem; error.hidden = false; input.focus(); return; }
      var before = profile.name;
      movePortfolioForRename(before, next);
      saveProfile(next, profile.created);
      state.editingName = false;
      renderNavUser();
      renderToday();
      renderProfile();
      byId("p-change").focus();
      if (portfolioKey(before) !== portfolioKey(next)) {
        // The new name may already have its own selection on this browser; show whichever belongs to it.
        state.digest = null;
        state.digestSerial = state.digestSerial + 1;
        initPortfolio().then(function () { renderToday(); renderProfile(); return loadDigest(); });
      }
    });
    box.appendChild(form);
  }

  var account = el("section", "p-sec");
  account.setAttribute("aria-labelledby", "p-h-account");
  var ah = el("h2", "serif", "Demo account");
  ah.id = "p-h-account";
  account.appendChild(ah);
  var dl = el("dl", "p-list");
  add(dl, el("dt", null, "Status"), el("dd", null, "Demo profile. Not a secure account."));
  add(dl, el("dt", null, "Password"), el("dd", null, "None. FINN never asks for one."));
  add(dl, el("dt", null, "Stored"), el("dd", null, state.profilePersisted
    ? "In this browser, on this device only. Nothing is sent to FINN's server."
    : "Not saved: this browser blocks storage, so the name lasts only until you close the page."));
  add(dl, el("dt", null, "Created"), el("dd", null, profile.created ? fmtDay(profile.created) : "unknown"));
  account.appendChild(dl);
  box.appendChild(account);

  var portfolio = el("section", "p-sec");
  portfolio.setAttribute("aria-labelledby", "p-h-portfolio");
  var ph = el("h2", "serif", "Portfolio");
  ph.id = "p-h-portfolio";
  portfolio.appendChild(ph);
  if (state.portfolio) {
    var pl = el("dl", "p-list");
    var n = state.portfolio.symbols.length;
    add(pl, el("dt", null, "Companies"), el("dd", null, n + " NSE-listed " + (n === 1 ? "company" : "companies")));
    add(pl, el("dt", null, "Kind"), el("dd", null, state.portfolio.mode === "demo"
      ? "The demo portfolio chosen by the builder. Add or remove companies in the Portfolio tab to make it your own."
      : "Your own selection for this name. Stored only in this browser: it does not sync to other devices or browsers."));
    portfolio.appendChild(pl);
    var symbols = el("p", "sym-list");
    state.portfolio.symbols.forEach(function (item) { symbols.appendChild(el("span", null, item.symbol)); });
    portfolio.appendChild(symbols);
    var pActions = el("div", "p-actions");
    add(pActions, button("Manage portfolio", "btn", function () { showView("portfolio"); }));
    portfolio.appendChild(pActions);
  } else if (state.portfolioError) {
    portfolio.appendChild(el("p", "p-note", "The demo portfolio could not be loaded."));
    portfolio.appendChild(button("Retry", "linkbtn", function () { initPortfolio().then(renderProfile); }));
  } else {
    portfolio.appendChild(el("p", "p-note", "Loading the portfolio…"));
  }
  box.appendChild(portfolio);

  var prefs = el("section", "p-sec");
  prefs.setAttribute("aria-labelledby", "p-h-prefs");
  var prh = el("h2", "serif", "Preferences");
  prh.id = "p-h-prefs";
  prefs.appendChild(prh);
  var holder = el("div");
  holder.id = "p-prefs";
  prefs.appendChild(holder);
  var prefActions = el("div", "p-actions");
  add(prefActions, button("Edit filters", "btn", function () {
    if (state.digest) { openFilters(); } else { loadDigest().then(openFilters); }
  }));
  prefs.appendChild(prefActions);
  prefs.appendChild(el("p", "p-note", "Preferences are your Today filters. They are saved in this browser only."));
  box.appendChild(prefs);

  var counts = countDeviceResearch();
  var research = el("section", "p-sec");
  research.setAttribute("aria-labelledby", "p-h-research");
  var rh = el("h2", "serif", "Research on this device");
  rh.id = "p-h-research";
  research.appendChild(rh);
  var rl = el("dl", "p-list");
  add(rl, el("dt", null, "Recent companies"), el("dd", null, String(counts.recents)));
  add(rl, el("dt", null, "Saved results"), el("dd", null, String(counts.briefs)));
  research.appendChild(rl);
  research.appendChild(el("p", "p-note", "A saved result is shown only if fresh research for the same company fails, and it is labelled with its date."));
  var clearBtn = button("Remove saved research", "btn", openClearResearch);
  clearBtn.disabled = counts.recents === 0 && counts.briefs === 0;
  var rowR = el("div", "p-actions");
  rowR.appendChild(clearBtn);
  research.appendChild(rowR);
  box.appendChild(research);

  var data = el("section", "p-sec");
  data.setAttribute("aria-labelledby", "p-h-data");
  var dh = el("h2", "serif", "Data on this device");
  dh.id = "p-h-data";
  data.appendChild(dh);
  data.appendChild(el("p", "p-note", "FINN stores your name, portfolio selection, filters, recent companies and saved research in this browser. You can delete all of it."));
  var rowD = el("div", "p-actions");
  rowD.appendChild(button("Remove all demo data", "btn danger", openRemoveAll));
  data.appendChild(rowD);
  box.appendChild(data);

  renderPrefs();
}

// ------------------------------------------------------------------ wiring

function wire() {
  byId("nav-today").addEventListener("click", function () { showView("today"); });
  byId("nav-portfolio").addEventListener("click", function () { showView("portfolio"); });
  byId("nav-research").addEventListener("click", function () { showView("research"); });
  byId("pf-form").addEventListener("submit", onAddSubmit);
  byId("pf-input").addEventListener("input", onPortfolioInput);
  byId("nav-profile").addEventListener("click", function () { showView("profile"); });
  byId("open-filters").addEventListener("click", openFilters);
  byId("sheet-close").addEventListener("click", closeSheet);
  byId("sheet").addEventListener("click", function (event) { if (event.target === byId("sheet")) { closeSheet(); } });
  byId("r-form").addEventListener("submit", onSubmit);
  byId("r-input").addEventListener("input", function () { byId("r-input-error").hidden = true; });
  byId("signin").addEventListener("submit", onSignIn);
  byId("signin-name").addEventListener("input", function () { byId("signin-error").hidden = true; });
}

wire();
var remembered = getStoredProfile();
if (remembered) {
  state.profile = remembered;
  enterApp();
} else {
  showLanding();
}
