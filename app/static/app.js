// FINN front end. Plain JavaScript, no libraries, no external requests.
// All text from the server is inserted with textContent (never innerHTML).

var state = {
  digest: null,
  liveRows: null,
  liveFetchedUtc: null,
  windowHours: 24,
  volThreshold: 2.0,
  moveThreshold: 3,
  questions: [],
  selectedQuestion: null,
  mode: "cached",
  focusFiling: null,
  filingsPolling: false,
  autoRefreshed: false,
  guidanceLoaded: false
};

// Network helper: every call has a timeout and a few quick retries. The 3 Nov measurements showed waits of
// 60 s and 120 s that were connection set-up timeouts, not slow answers (RESEARCH_NOTES.md, round 10), and
// the server's submit and refresh endpoints are safe to repeat.
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

function storeGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}

function storeSet(key, value) {
  try { window.localStorage.setItem(key, value); return true; } catch (e) { return false; }
}

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = text;
  }
  return node;
}

function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function formatNumber(value, digits) {
  if (value === null || value === undefined) {
    return "–";
  }
  return Number(value).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function compact(value) {
  if (value === null || value === undefined) {
    return "–";
  }
  if (value >= 10000000) {
    return (value / 10000000).toFixed(2) + " Cr";
  }
  if (value >= 100000) {
    return (value / 100000).toFixed(2) + " L";
  }
  return formatNumber(value, 0);
}

function formatTime(iso) {
  var date = new Date(iso);
  return date.toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }) + " IST";
}

// ------------------------------------------------------------------ data

function currentStats(entry) {
  if (state.liveRows) {
    for (var i = 0; i < state.liveRows.length; i++) {
      var row = state.liveRows[i];
      if (row.symbol === entry.symbol && row.ok) {
        return row;
      }
    }
  }
  return entry;
}

function filingsInWindow() {
  var asOf = new Date(state.digest.as_of_utc).getTime();
  var cutoff = asOf - state.windowHours * 3600 * 1000;
  var result = [];
  for (var i = 0; i < state.digest.filings.length; i++) {
    var filing = state.digest.filings[i];
    if (new Date(filing.at).getTime() >= cutoff) {
      result.push(filing);
    }
  }
  return result;
}

function loadDigest() {
  return fetchJson("/api/digest", {}, 15000, 3)
    .then(function (result) {
      if (!result.ok) {
        throw new Error("HTTP " + result.status);
      }
      state.digest = result.body;
      renderAll();
      var fs = state.digest.filings_status;
      if (fs && fs.running) {
        pollFilings();
      } else {
        maybeAutoRefreshFilings();
      }
    })
    .catch(function (error) {
      if (state.digest) {
        // keep what is on screen, dated, and say the reload failed
        var note = document.getElementById("filings-status");
        note.appendChild(el("p", "hint", "Could not reload the digest just now (" + error.message + "). The data above is what was loaded earlier."));
        return;
      }
      var status = document.getElementById("status");
      status.textContent = "Could not load the digest (" + error.message + "). Reload to retry.";
    });
}

// ------------------------------------------------------------------ digest rendering

function renderStatus() {
  // Prices only. Filings have their own status block (renderFilingsStatus); a live price refresh says nothing
  // about whether filings are fresh.
  var status = document.getElementById("status");
  status.className = "status";
  var meta = state.digest.meta;
  if (state.liveRows) {
    status.classList.add("live");
    var partial = false;
    for (var i = 0; i < state.liveRows.length; i++) {
      if (state.liveRows[i].partial) {
        partial = true;
      }
    }
    var text = "PRICES: LIVE, fetched " + formatTime(state.liveFetchedUtc) + " (Yahoo). This does not refresh filings; see the Filings section.";
    if (partial) {
      text = text + " Today's session may be in progress, so volume ratios can read low.";
    }
    status.textContent = text;
  } else {
    status.textContent = "PRICES: Snapshot, not live. To the " + meta.prices.last_bar + " close (Yahoo). Tap Refresh prices for live prices.";
    status.title = state.digest.label;
  }
}

var FSTATE_LABEL = {
  live: "live, filings found",
  valid_empty: "live check: no filings in 7 days",
  ambiguous_empty: "live check: empty, cannot be verified",
  stale_live: "refresh failed, older live data kept",
  failed_snapshot: "refresh failed, snapshot shown",
  snapshot: "snapshot only"
};

function renderFilingsStatus() {
  var box = document.getElementById("filings-status");
  clear(box);
  var fs = state.digest.filings_status;
  if (!fs) {
    box.textContent = "Filings status unavailable.";
    return;
  }
  var head = el("div", "head");
  var chipClass = "chip snapshot";
  var chipText = "SNAPSHOT";
  if (fs.mode === "live") { chipClass = "chip live"; chipText = "LIVE (NSE)"; }
  if (fs.mode === "partial") { chipClass = "chip snapshot"; chipText = "PARTLY LIVE"; }
  if (fs.mode === "refresh_failed") { chipClass = "chip failed"; chipText = "REFRESH FAILED"; }
  head.appendChild(el("span", chipClass, chipText));
  var when = "Saved copy fetched " + formatTime(fs.snapshot_fetched_utc);
  if (fs.last_success_utc) {
    when = "Last successful update " + formatTime(fs.last_success_utc);
  }
  head.appendChild(el("span", "when", when));
  box.appendChild(head);

  var run = fs.last_run;
  if (fs.mode === "snapshot") {
    box.appendChild(el("p", null, "These filings are a saved copy of NSE announcements (25 to 31 Oct), not live. Nothing has been refreshed since this server started."));
  } else if (fs.mode === "live") {
    box.appendChild(el("p", null, "All " + run.total + " symbols were fetched from NSE for the last 7 days."));
  } else if (fs.mode === "partial") {
    box.appendChild(el("p", null, run.ok + " of " + run.total + " symbols updated. " + run.failed + " failed (" + run.failed_symbols.join(", ") + "); those keep their older data, dated on each card."));
  } else {
    box.appendChild(el("p", null, "The last attempt (" + formatTime(fs.last_attempt_utc) + ") updated 0 of " + run.total + " symbols. Showing " +
      (fs.last_success_utc ? "the data from the last successful update." : "the saved copy from " + formatTime(fs.snapshot_fetched_utc) + ".")));
  }

  if (fs.running && fs.progress) {
    var bar = document.createElement("progress");
    bar.max = fs.progress.total;
    bar.value = fs.progress.done;
    box.appendChild(bar);
    box.appendChild(el("p", "hint", "Refreshing from NSE: " + fs.progress.done + " of " + fs.progress.total + " symbols · " + Math.round(fs.progress.elapsed_s) + " s. Each NSE call can take about 20 s from the free server."));
  } else {
    var button = el("button", "btn small ghost", fs.mode === "snapshot" ? "Refresh filings from NSE" : "Refresh filings again");
    button.type = "button";
    button.id = "filings-btn";
    box.appendChild(button);
    box.appendChild(el("span", "hint", " Filings only. Takes about 1 to 2 minutes."));
  }

  var details = el("details");
  details.appendChild(el("summary", null, "Per-symbol status"));
  var list = el("ul", "sym");
  for (var i = 0; i < state.digest.portfolio.length; i++) {
    var entry = state.digest.portfolio[i];
    var st = fs.symbols[entry.symbol] || { state: "snapshot" };
    var line = entry.symbol + ": " + (FSTATE_LABEL[st.state] || st.state);
    if (st.fetched_utc) { line = line + " · " + formatTime(st.fetched_utc); }
    if (st.detail && (st.state === "stale_live" || st.state === "failed_snapshot")) { line = line + " · " + st.detail; }
    list.appendChild(el("li", null, line));
  }
  details.appendChild(list);
  box.appendChild(details);
}

function maybeAutoRefreshFilings() {
  // One automatic refresh per page load, only when there is no live update from the last 30 minutes.
  if (state.autoRefreshed) { return; }
  state.autoRefreshed = true;
  var fs = state.digest.filings_status;
  var fresh = fs && fs.last_success_utc && (Date.now() - new Date(fs.last_success_utc).getTime()) < 30 * 60 * 1000;
  if (!fresh) { refreshFilings(); }
}

function refreshFilings() {
  fetchJson("/api/filings/refresh", { method: "POST" }, 15000, 3)
    .then(function () { return pollFilings(); })
    .catch(function () {
      var box = document.getElementById("filings-status");
      box.appendChild(el("p", "hint", "Could not reach the server to start the refresh. The filings shown are unchanged and keep their dates."));
    });
}

function pollFilings() {
  if (state.filingsPolling) { return; }
  state.filingsPolling = true;
  var misses = 0;
  function tick() {
    fetchJson("/api/filings/status", {}, 10000, 1)
      .then(function (result) {
        misses = 0;
        var status = result.body;
        if (state.digest && state.digest.filings_status) {
          state.digest.filings_status.running = status.running;
          state.digest.filings_status.progress = status.progress;
          renderFilingsStatus();
        }
        if (status.running) {
          window.setTimeout(tick, 2000);
        } else {
          state.filingsPolling = false;
          loadDigest();
        }
      })
      .catch(function () {
        misses = misses + 1;
        if (misses >= 8) {
          state.filingsPolling = false;
          var box = document.getElementById("filings-status");
          box.appendChild(el("p", "hint", "Lost contact with the server while refreshing. Reload to see the result."));
          return;
        }
        window.setTimeout(tick, 2000);
      });
  }
  tick();
}

function renderAlerts() {
  var box = document.getElementById("alerts");
  clear(box);
  var count = 0;

  for (var i = 0; i < state.digest.portfolio.length; i++) {
    var entry = state.digest.portfolio[i];
    var stats = currentStats(entry);

    if (stats.volume_ratio !== null && stats.volume_ratio !== undefined && stats.volume_ratio >= state.volThreshold) {
      var volCard = el("div", "item alert-vol");
      var volMeta = el("div", "meta");
      volMeta.appendChild(el("span", "chip", entry.symbol));
      volMeta.appendChild(el("span", null, "Volume alert · bar of " + stats.last_date));
      volCard.appendChild(volMeta);
      volCard.appendChild(el("p", "title", "Volume " + stats.volume_ratio.toFixed(1) + "× its 20-day average"));
      volCard.appendChild(el("p", "body", compact(stats.volume) + " shares traded vs " + compact(stats.avg_volume_20d) + " average"));
      box.appendChild(volCard);
      count = count + 1;
    }

    if (stats.change_pct !== null && stats.change_pct !== undefined && Math.abs(stats.change_pct) >= state.moveThreshold) {
      var moveCard = el("div", "item alert-move");
      var moveMeta = el("div", "meta");
      moveMeta.appendChild(el("span", "chip", entry.symbol));
      moveMeta.appendChild(el("span", null, "Price move · bar of " + stats.last_date));
      moveCard.appendChild(moveMeta);
      var sign = stats.change_pct > 0 ? "+" : "";
      moveCard.appendChild(el("p", "title", sign + stats.change_pct.toFixed(2) + "% on the day"));
      moveCard.appendChild(el("p", "body", "Close ₹" + formatNumber(stats.close, 2) + " vs ₹" + formatNumber(stats.prev_close, 2) + " the session before"));
      box.appendChild(moveCard);
      count = count + 1;
    }
  }

  var inWindow = filingsInWindow();
  for (var j = 0; j < inWindow.length; j++) {
    var filing = inWindow[j];
    if (filing.tier === "high") {
      box.appendChild(filingCard(filing, true));
      count = count + 1;
    }
  }

  if (count === 0) {
    box.appendChild(el("div", "empty", "No alerts in this window at these thresholds."));
  }
  document.getElementById("alert-count").textContent = count + " triggered";
}

function filingCard(filing, asAlert) {
  var card = el("div", asAlert ? "item alert-high" : "item");
  var meta = el("div", "meta");
  meta.appendChild(el("span", "chip", filing.symbol));
  meta.appendChild(el("span", "chip " + filing.tier, filing.tier));
  meta.appendChild(el("span", null, formatTime(filing.at)));
  if (filing.source === "live") {
    meta.appendChild(el("span", "chip live", "live · fetched " + formatTime(filing.fetched_utc)));
  } else {
    meta.appendChild(el("span", "chip snapshot", "snapshot"));
  }
  if (filing.dup_count > 1) {
    meta.appendChild(el("span", null, "×" + filing.dup_count + " filed"));
  }
  card.appendChild(meta);
  card.appendChild(el("p", "title", filing.desc));
  card.appendChild(el("p", "body", filing.text));
  var reason = el("p", "hint", "Tier: " + filing.tier_reason);
  card.appendChild(reason);
  if (filing.url) {
    var link = el("a", "link", "Open filing PDF ↗");
    link.href = filing.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    card.appendChild(link);
  }
  var go = el("button", "btn small", "Research this change");
  go.type = "button";
  go.setAttribute("data-research", filing.symbol);
  go.addEventListener("click", function () { researchChange(filing); });
  card.appendChild(go);
  return card;
}

// ------------------------------------------------------------------ research this change

function researchChange(filing) {
  state.focusFiling = filing;
  closeSheet();
  showTab("research");
  document.getElementById("ticker-input").value = filing.symbol;
  renderFilingContext();
  var ctx = document.getElementById("filing-context");
  if (ctx.scrollIntoView) { ctx.scrollIntoView({ block: "start" }); }
}

function renderFilingContext() {
  var box = document.getElementById("filing-context");
  clear(box);
  var filing = state.focusFiling;
  if (!filing) { return; }
  var card = el("div", "fctx");
  card.appendChild(el("h3", null, "Researching this change: " + filing.symbol));
  var meta = el("div", "meta");
  meta.appendChild(el("span", "chip " + filing.tier, filing.tier));
  meta.appendChild(el("span", null, "Filed " + formatTime(filing.at)));
  meta.appendChild(el("span", filing.source === "live" ? "chip live" : "chip snapshot",
    filing.source === "live" ? "live · fetched " + formatTime(filing.fetched_utc) : "snapshot"));
  card.appendChild(meta);
  card.appendChild(el("p", "title", filing.desc));
  card.appendChild(el("p", "body", filing.text));
  card.appendChild(el("p", "hint", "Why this tier: " + filing.tier_reason));
  if (filing.url) {
    var link = el("a", "link", "Open the filing on NSE ↗");
    link.href = filing.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    card.appendChild(link);
  }
  card.appendChild(el("p", "hint", "The brief below is built from the company's latest annual report and latest earnings-call transcript. This filing is shown for context: FINN does not read its PDF, and the brief cannot reflect it unless the filing is one of those two documents."));
  var actions = el("div", "row-actions");
  var run = el("button", "btn small", "Analyse " + filing.symbol);
  run.type = "button";
  run.id = "ctx-analyse";
  run.addEventListener("click", function (event) { startAnalysis(event); });
  actions.appendChild(run);
  var clearButton = el("button", "btn small ghost", "Clear");
  clearButton.type = "button";
  clearButton.addEventListener("click", function () { state.focusFiling = null; renderFilingContext(); });
  actions.appendChild(clearButton);
  card.appendChild(actions);
  box.appendChild(card);
}

function docDate(doc) {
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
    var date = docDate(docs[i]);
    if (date && (newest === null || date > newest)) { newest = date; }
  }
  if (newest === null) {
    return "The filing is dated " + formatTime(filing.at) + ". The dates of the documents in this brief could not be read, so FINN cannot say which is newer.";
  }
  if (filed > newest) {
    return "This filing (" + formatTime(filing.at) + ") is newer than the newest document in the brief (about " + newest.toLocaleDateString("en-IN", { month: "short", year: "numeric" }) + "), so the brief does not cover it.";
  }
  return "This filing (" + formatTime(filing.at) + ") is not newer than the documents in the brief, so the brief may discuss it. Open the filing and check.";
}

function renderFilings() {
  var box = document.getElementById("filings");
  clear(box);
  var inWindow = filingsInWindow();
  var groups = { high: [], medium: [], low: [] };
  for (var i = 0; i < inWindow.length; i++) {
    groups[inWindow[i].tier].push(inWindow[i]);
  }
  document.getElementById("filing-count").textContent = inWindow.length + " in window";

  if (inWindow.length === 0) {
    box.appendChild(el("div", "empty", "No filings in this window."));
    return;
  }

  var order = ["high", "medium", "low"];
  var titles = { high: "Material", medium: "Other updates", low: "Routine" };
  for (var g = 0; g < order.length; g++) {
    var tier = order[g];
    var list = groups[tier];
    if (list.length === 0) {
      continue;
    }
    var details = el("details", "group");
    if (tier === "high") {
      details.open = true;
    }
    var summary = el("summary");
    summary.appendChild(el("span", null, titles[tier]));
    summary.appendChild(el("span", "hint", list.length + (tier === "high" ? "" : " (tap to expand)")));
    details.appendChild(summary);
    var inner = el("div", "stack");
    for (var k = 0; k < list.length; k++) {
      inner.appendChild(filingCard(list[k], false));
    }
    details.appendChild(inner);
    box.appendChild(details);
  }
}

function renderPortfolio() {
  var box = document.getElementById("portfolio");
  clear(box);
  for (var i = 0; i < state.digest.portfolio.length; i++) {
    var entry = state.digest.portfolio[i];
    var stats = currentStats(entry);
    var row = el("button", "stock");
    row.type = "button";
    row.setAttribute("data-symbol", entry.symbol);

    var left = el("span", "sym", entry.symbol);
    if (entry.has_research) {
      left.appendChild(el("span", "badge", "docs"));
    }
    left.appendChild(el("span", "nm", entry.name || ""));
    row.appendChild(left);

    row.appendChild(el("span", "num", "₹" + formatNumber(stats.close, 2)));

    var change = stats.change_pct;
    var cls = "num";
    var label = "–";
    if (change !== null && change !== undefined) {
      cls = "num " + (change >= 0 ? "up" : "down");
      label = (change > 0 ? "+" : "") + change.toFixed(2) + "%";
    }
    row.appendChild(el("span", cls, label));
    box.appendChild(row);
  }
}

function renderAll() {
  renderStatus();
  renderFilingsStatus();
  renderAlerts();
  renderFilings();
  renderPortfolio();
}

// ------------------------------------------------------------------ stock sheet

function openSheet(symbol) {
  var entry = null;
  for (var i = 0; i < state.digest.portfolio.length; i++) {
    if (state.digest.portfolio[i].symbol === symbol) {
      entry = state.digest.portfolio[i];
    }
  }
  if (!entry) {
    return;
  }
  var stats = currentStats(entry);
  document.getElementById("sheet-title").textContent = entry.symbol + " · " + (entry.name || "");
  var content = document.getElementById("sheet-content");
  clear(content);

  var summary = el("div", "card");
  summary.appendChild(el("p", "title", "₹" + formatNumber(stats.close, 2)));
  var line = "Bar of " + stats.last_date + ": ";
  if (stats.change_pct !== null && stats.change_pct !== undefined) {
    line = line + (stats.change_pct > 0 ? "+" : "") + stats.change_pct.toFixed(2) + "% · ";
  }
  line = line + "volume " + compact(stats.volume) + " (" + (stats.volume_ratio ? stats.volume_ratio.toFixed(2) + "× 20-day avg" : "no average") + ")";
  summary.appendChild(el("p", "body", line));
  content.appendChild(summary);

  if (entry.has_research) {
    var ask = el("button", "btn", "Open research for " + entry.symbol);
    ask.type = "button";
    ask.id = "sheet-research";
    ask.addEventListener("click", function () {
      closeSheet();
      showTab("research");
    });
    content.appendChild(ask);
  } else {
    content.appendChild(el("p", "note", "No primary-source documents are loaded for " + entry.symbol + ". The research demo covers KPITTECH only."));
  }

  content.appendChild(el("h2", "section", "Filings (7 days)"));
  var fstat = state.digest.filings_status && state.digest.filings_status.symbols[symbol];
  if (fstat) {
    var statusLine = "Source status: " + (FSTATE_LABEL[fstat.state] || fstat.state);
    if (fstat.fetched_utc) { statusLine = statusLine + " · fetched " + formatTime(fstat.fetched_utc); }
    if (fstat.detail && (fstat.state === "stale_live" || fstat.state === "failed_snapshot")) {
      statusLine = statusLine + " · live refresh failed (" + fstat.detail + ")";
    }
    content.appendChild(el("p", "hint", statusLine));
  }
  var stack = el("div", "stack");
  var found = 0;
  for (var j = 0; j < state.digest.filings.length; j++) {
    if (state.digest.filings[j].symbol === symbol) {
      stack.appendChild(filingCard(state.digest.filings[j], false));
      found = found + 1;
    }
  }
  if (found === 0) {
    var note = entry.no_filings_note || "No filings in the window. This symbol has not been refreshed live.";
    stack.appendChild(el("div", "empty", note));
  }
  content.appendChild(stack);

  document.getElementById("sheet").hidden = false;
  document.getElementById("sheet-close").focus();
}

function closeSheet() {
  document.getElementById("sheet").hidden = true;
}

// ------------------------------------------------------------------ live refresh

function refreshPrices() {
  var button = document.getElementById("refresh-btn");
  var note = document.getElementById("refresh-note");
  button.disabled = true;
  note.textContent = "Fetching…";
  fetch("/api/prices/refresh", { method: "POST" })
    .then(function (response) {
      return response.json().then(function (body) {
        return { ok: response.ok, body: body };
      });
    })
    .then(function (result) {
      if (!result.ok) {
        var code = result.body.detail && result.body.detail.code ? result.body.detail.code : "error";
        note.textContent = "Live refresh failed (" + code + "). Showing snapshot.";
        return;
      }
      state.liveRows = result.body.rows;
      state.liveFetchedUtc = result.body.fetched_utc;
      note.textContent = result.body.ok_count + " of " + result.body.rows.length + " symbols updated";
      renderAll();
    })
    .catch(function () {
      note.textContent = "Live refresh failed (network). Showing snapshot.";
    })
    .then(function () {
      button.disabled = false;
    });
}

// ------------------------------------------------------------------ tabs

function showTab(name) {
  var tabs = ["digest", "research"];
  for (var i = 0; i < tabs.length; i++) {
    var panel = document.getElementById("tab-" + tabs[i]);
    var button = document.getElementById("btn-" + tabs[i]);
    var active = tabs[i] === name;
    panel.hidden = !active;
    button.classList.toggle("on", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  window.scrollTo(0, 0);
  if (name === "research" && state.questions.length === 0) {
    loadQuestions();
  }
  if (name === "research" && !state.guidanceLoaded) {
    loadGuidance();
  }
}

// ------------------------------------------------------------------ research

function loadQuestions() {
  fetch("/api/questions")
    .then(function (response) {
      return response.json();
    })
    .then(function (data) {
      state.questions = data.questions;
      renderQuestions();
    })
    .catch(function () {
      var box = document.getElementById("questions");
      clear(box);
      box.appendChild(el("div", "error", "Could not load the question list."));
    });
}

function renderQuestions() {
  var box = document.getElementById("questions");
  clear(box);
  for (var i = 0; i < state.questions.length; i++) {
    var question = state.questions[i];
    var button = el("button", "qbtn");
    button.type = "button";
    button.setAttribute("data-id", question.id);
    button.appendChild(el("span", null, (question.symbol || "") + " · " + (question.short || question.id)));
    button.appendChild(el("small", null, question.question));
    if (state.selectedQuestion === question.id) {
      button.classList.add("on");
    }
    box.appendChild(button);
  }
  updateRunButton();
}

function updateRunButton() {
  var run = document.getElementById("run-btn");
  if (!state.selectedQuestion) {
    run.disabled = true;
    run.textContent = "Choose a question";
    return;
  }
  run.disabled = false;
  run.textContent = state.mode === "live" ? "Run live analysis" : "Show cached answer";
}

function findPassage(record, sourceId) {
  var passages = record.passages || [];
  for (var i = 0; i < passages.length; i++) {
    if (passages[i].source_id === sourceId) {
      return passages[i];
    }
  }
  return null;
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function renderPassageWithQuote(container, passageText, quote) {
  var words = quote.trim().split(/\s+/);
  var escaped = [];
  for (var i = 0; i < words.length; i++) {
    escaped.push(escapeRegExp(words[i]));
  }
  var found = null;
  if (words.length > 0 && words[0] !== "") {
    var pattern = new RegExp(escaped.join("\\s+"), "i");
    found = pattern.exec(passageText);
  }
  if (!found) {
    container.textContent = passageText;
    return false;
  }
  container.appendChild(document.createTextNode(passageText.slice(0, found.index)));
  container.appendChild(el("mark", null, found[0]));
  container.appendChild(document.createTextNode(passageText.slice(found.index + found[0].length)));
  return true;
}

function renderResult(record, targetBox) {
  var box = targetBox || document.getElementById("result");
  clear(box);

  var bannerClass = "banner cached";
  var heading = "CACHED demo answer";
  if (record.mode === "live") {
    bannerClass = "banner live";
    heading = "LIVE analysis";
  } else if (record.source_type && record.source_type !== "model") {
    bannerClass = "banner op";
    heading = "CACHED · operator-written extract, not model output";
  }
  var banner = el("div", bannerClass);
  banner.appendChild(el("b", null, heading));
  banner.appendChild(el("span", null, record.label));
  box.appendChild(banner);

  if (record.fallback_reason) {
    box.appendChild(el("p", "note", "Showing cached because live was not used: " + record.fallback_reason.code + "."));
  }

  var result = record.result || {};
  box.appendChild(el("p", "answer", result.answer || "No answer text."));
  if (result.not_stated) {
    box.appendChild(el("p", "note", "The passages do not state an answer."));
  }

  var claims = result.claims || [];
  var checks = (record.auto_check && record.auto_check.claims) ? record.auto_check.claims : [];
  for (var i = 0; i < claims.length; i++) {
    box.appendChild(claimNode(record, claims[i], checks[i]));
  }

  if (record.auto_check) {
    var summary = record.auto_check.auto_pass_count + " of " + record.auto_check.claim_count +
      " claims passed the mechanical check (source exists, quote is verbatim, numbers appear in the source).";
    box.appendChild(el("p", "note", summary));
  }
  box.appendChild(el("p", "note", record.disclaimer || ""));
}

function claimNode(record, claim, check) {
  var wrapper = el("div", "claim");
  var button = el("button");
  button.type = "button";
  button.setAttribute("aria-expanded", "false");
  button.appendChild(el("span", null, claim.text));

  var tags = el("div", "tags");
  var passage = findPassage(record, claim.source);
  var where = claim.source;
  if (passage) {
    where = claim.source + " · PDF page " + passage.page;
    if (passage.speaker) {
      where = where + " · " + passage.speaker;
    }
  }
  tags.appendChild(el("span", "chip", where));
  if (check) {
    if (check.auto_pass) {
      tags.appendChild(el("span", "ok", "✓ quote verbatim in source"));
    } else {
      tags.appendChild(el("span", "bad", "✗ check failed"));
    }
  }
  tags.appendChild(el("span", "hint", "tap to see source"));
  button.appendChild(tags);
  wrapper.appendChild(button);

  var passageBox = el("div", "passage");
  passageBox.hidden = true;
  if (passage) {
    var located = renderPassageWithQuote(passageBox, passage.text, claim.quote || "");
    if (!located) {
      passageBox.appendChild(el("p", "note", "Quote not located in this passage."));
    }
  } else {
    passageBox.textContent = "Cited source is not in the returned passages.";
  }
  wrapper.appendChild(passageBox);

  button.addEventListener("click", function () {
    var open = passageBox.hidden;
    passageBox.hidden = !open;
    button.setAttribute("aria-expanded", open ? "true" : "false");
  });
  return wrapper;
}

function runResearch() {
  var box = document.getElementById("result");
  var run = document.getElementById("run-btn");
  clear(box);
  run.disabled = true;
  run.textContent = state.mode === "live" ? "Asking the model…" : "Loading…";
  fetch("/api/research", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question_id: state.selectedQuestion, mode: state.mode })
  })
    .then(function (response) {
      return response.json().then(function (body) {
        return { ok: response.ok, status: response.status, body: body };
      });
    })
    .then(function (result) {
      if (!result.ok) {
        var detail = result.body.detail || {};
        var code = detail.code || ("HTTP " + result.status);
        var message = "Could not get an answer (" + code + ").";
        if (state.mode === "live") {
          message = message + " Live analysis is unavailable right now; switch to Cached.";
        }
        box.appendChild(el("div", "error", message));
        return;
      }
      renderResult(result.body);
    })
    .catch(function () {
      box.appendChild(el("div", "error", "Network error. Try again."));
    })
    .then(function () {
      updateRunButton();
    });
}


// ------------------------------------------------------------------ guidance vs outcome

function loadGuidance() {
  state.guidanceLoaded = true;
  fetchJson("/api/guidance", {}, 15000, 3)
    .then(function (result) {
      var box = document.getElementById("guidance");
      clear(box);
      if (!result.ok || !result.body.examples || result.body.examples.length === 0) {
        box.appendChild(el("div", "empty", "No guidance comparison is loaded."));
        return;
      }
      for (var i = 0; i < result.body.examples.length; i++) {
        box.appendChild(guidanceNode(result.body.examples[i]));
      }
      box.appendChild(el("p", "note", "Checked: " + result.body.verified_by));
      box.appendChild(el("p", "note", "Not checked: " + result.body.not_verified));
    })
    .catch(function () {
      state.guidanceLoaded = false;
      var box = document.getElementById("guidance");
      clear(box);
      box.appendChild(el("div", "error", "Could not load the guidance comparison."));
    });
}

function fieldLines(side) {
  var list = el("ul", "gfields");
  var names = ["metric", "period", "unit", "scope"];
  for (var i = 0; i < names.length; i++) {
    var item = el("li");
    item.appendChild(el("b", null, names[i] + ": "));
    var label = side[names[i]].label;
    if (names[i] === "metric" && side.metric.basis === "inferred") { label = label + " (inferred)"; }
    item.appendChild(document.createTextNode(label));
    list.appendChild(item);
  }
  return list;
}

function sideNode(heading, side) {
  var block = el("div", "gblock");
  block.appendChild(el("h3", null, heading));
  block.appendChild(el("blockquote", null, "\u201c" + side.quote + "\u201d"));
  block.appendChild(el("p", "hint", side.title + " · PDF page " + side.pdf_page +
    (side.quote_verbatim_in_stored_page ? " · ✓ quote verbatim in stored page" : " · ✗ quote NOT found in stored page")));
  block.appendChild(fieldLines(side));
  var row = el("div");
  row.appendChild(safeLink(side.url, "Open source PDF"));
  block.appendChild(row);
  if (side.url_note) { block.appendChild(el("p", "hint", side.url_note)); }
  if (side.page_text) {
    var details = el("details");
    details.appendChild(el("summary", null, "Show the page text"));
    var holder = el("p", "body");
    renderPassageWithQuote(holder, side.page_text, side.quote);
    details.appendChild(holder);
    block.appendChild(details);
  }
  if (side.extra_quotes) {
    for (var i = 0; i < side.extra_quotes.length; i++) {
      var extra = side.extra_quotes[i];
      block.appendChild(el("p", "hint", "Also, PDF page " + extra.pdf_page + (extra.verbatim ? " (✓ verbatim)" : " (✗ not found)") + ": \u201c" + extra.quote + "\u201d"));
    }
  }
  return block;
}

function guidanceNode(example) {
  var wrap = el("div", "stack");
  var head = el("div", "meta");
  head.appendChild(el("span", "chip", example.symbol));
  head.appendChild(el("span", null, example.title));
  wrap.appendChild(head);
  wrap.appendChild(sideNode("What management guided (" + example.guidance.call_date + ")", example.guidance));
  wrap.appendChild(sideNode("What was reported (" + example.outcome.call_date + ")", example.outcome));
  var gate = example.gate;
  var verdict = el("div", gate.verdict === "comparable" ? "gverdict ok" : "gverdict");
  if (gate.verdict === "comparable") {
    verdict.appendChild(el("b", null, "Comparable: metric, period, unit and scope match"));
    if (gate.position) {
      verdict.appendChild(el("span", null, "Reported " + example.reported_value + " is " + gate.position + " the guided range " + example.guided_range.low + " to " + example.guided_range.high + "."));
    }
  } else {
    verdict.appendChild(el("b", null, "Not directly comparable"));
    verdict.appendChild(el("span", null, "FINN draws no conclusion from these two numbers because:"));
    var list = el("ul");
    for (var i = 0; i < gate.reasons.length; i++) {
      var reason = gate.reasons[i];
      list.appendChild(el("li", null, reason.field + ": " + reason.why + (reason.guidance && reason.outcome && reason.guidance !== reason.outcome ? " (guided: " + reason.guidance + "; reported: " + reason.outcome + ")" : "")));
    }
    verdict.appendChild(list);
  }
  for (var c = 0; c < (gate.caveats || []).length; c++) {
    verdict.appendChild(el("span", null, "Note: " + gate.caveats[c]));
  }
  wrap.appendChild(verdict);
  if (example.management_framing) {
    wrap.appendChild(el("p", "note", example.management_framing));
  }
  return wrap;
}

// ------------------------------------------------------------------ generic research (any ticker)

var analyze = { timer: null, jobId: null, startedAt: 0 };

function safeLink(url, text) {
  if (typeof url === "string" && url.indexOf("https://") === 0) {
    var link = el("a", null, text);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }
  return el("span", null, text);
}

function stageList(stages) {
  var list = el("ul", "progress");
  for (var i = 0; i < stages.length; i++) {
    var stage = stages[i];
    var item = el("li", stage.status);
    var mark = "•";
    if (stage.status === "running") { mark = "…"; }
    if (stage.status === "ok") { mark = "✓"; }
    if (stage.status === "failed" || stage.status === "unverified") { mark = "✗"; }
    if (stage.status === "not_found") { mark = "–"; }
    item.appendChild(el("span", "dot", mark));
    var text = stage.name + (stage.seconds !== null && stage.seconds !== undefined ? " (" + stage.seconds + " s)" : "");
    if (stage.note) { text = text + ": " + stage.note; }
    item.appendChild(el("span", null, text));
    list.appendChild(item);
  }
  return list;
}

function setAnalyzeBusy(busy) {
  document.getElementById("analyze-btn").disabled = busy;
  document.getElementById("ticker-input").disabled = busy;
}

function showProgress(job) {
  var box = document.getElementById("analyze-progress");
  clear(box);
  var elapsed = Math.round((Date.now() - analyze.startedAt) / 1000);
  box.appendChild(el("p", "note", "Analysing " + job.symbol + " · " + elapsed + " s elapsed (the server stops at 270 s). You can leave this tab open; nothing is sent twice."));
  var bar = document.createElement("progress");
  bar.max = 270;
  bar.value = Math.min(elapsed, 270);
  bar.style.width = "100%";
  box.appendChild(bar);
  if (elapsed > 50) {
    box.appendChild(el("p", "hint", "Waiting is normal here: the model call is paced to the provider's per-minute limit (up to about 60 s per section on the free plan)."));
  }
  box.appendChild(stageList(job.stages || []));
}

function showAnalyzeError(message) {
  var box = document.getElementById("analyze-progress");
  clear(box);
  box.appendChild(el("div", "error", message));
}

var ERROR_TEXT = {
  invalid_ticker: "That is not a valid NSE ticker. Use letters, digits, & or - (up to 20 characters).",
  busy: "Another analysis is running on the free server. Try again in a minute.",
  rate_limited_hourly: "The hourly limit for live analyses has been reached. Try again later.",
  rate_limited_daily: "The daily limit for live analyses has been reached. Try again tomorrow.",
  model_not_configured: "The live model is not configured on this server.",
  ticker_not_found: "No company page was found for that ticker. Check the NSE symbol.",
  source_unreachable: "The document index (Screener) could not be reached. Try again later.",
  company_not_resolved: "The page did not identify a company for that ticker.",
  ticker_mismatch: "The page found belongs to a different NSE symbol.",
  documents_not_listed: "The company page lists no annual report or earnings-call documents.",
  job_timeout: "The analysis took too long and was stopped. No result is shown. Try again later."
};

function describeError(code, fallback) {
  return ERROR_TEXT[code] || fallback || ("Request failed (" + code + ").");
}

function startAnalysis(event) {
  event.preventDefault();
  var input = document.getElementById("ticker-input");
  var ticker = input.value.trim();
  if (ticker === "") {
    showAnalyzeError(describeError("invalid_ticker"));
    return;
  }
  // Keep the previous result on screen (dated) until the new one arrives; it is replaced on success and
  // kept, with a note, if this attempt fails.
  state.pendingTicker = ticker.toUpperCase();
  setAnalyzeBusy(true);
  analyze.startedAt = Date.now();
  // Submitting the same ticker twice is safe on the server (it returns the running job), so a stuck
  // connection is abandoned after 15 s and retried instead of waiting out a 60 to 120 s connect timeout.
  fetchJson("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker: ticker })
  }, 15000, 4)
    .then(function (result) {
      if (!result.ok) {
        var detail = result.body.detail || {};
        var code = detail.code || ("HTTP " + result.status);
        if (result.status === 422 && !detail.code) { code = "invalid_ticker"; }
        analysisFailed(describeError(code, detail.message));
        return;
      }
      handleJob(result.body);
    })
    .catch(function () {
      analysisFailed("Could not reach the server after 4 tries.");
    });
}

function storedBriefKey(symbol) {
  return "finn.brief." + symbol;
}

function saveBrief(brief) {
  var symbol = brief && (brief.ticker || (brief.company || {}).nse_symbol);
  if (!symbol || brief.answer_source === "snapshot_fallback") { return; }
  var text = JSON.stringify({ saved_at: new Date().toISOString(), brief: brief });
  if (text.length < 1500000) { storeSet(storedBriefKey(symbol), text); }
}

function analysisFailed(message) {
  setAnalyzeBusy(false);
  var box = document.getElementById("analyze-progress");
  clear(box);
  box.appendChild(el("div", "error", message));
  var symbol = state.pendingTicker;
  var raw = symbol ? storeGet(storedBriefKey(symbol)) : null;
  if (!raw) { return; }
  var saved = null;
  try { saved = JSON.parse(raw); } catch (e) { saved = null; }
  if (!saved || !saved.brief) { return; }
  var holder = document.getElementById("brief");
  clear(holder);
  var note = el("div", "banner op stale-note");
  note.appendChild(el("b", null, "SAVED ON THIS DEVICE, " + formatTime(saved.saved_at)));
  note.appendChild(el("span", null, "This is your last result for " + symbol + ". The new analysis did not finish, so nothing here is newer than that time."));
  holder.appendChild(note);
  var inner = el("div");
  holder.appendChild(inner);
  renderBrief(saved.brief, null, inner);
}

function handleJob(job) {
  analyze.jobId = job.job_id;
  if (job.status === "running") {
    showProgress(job);
    window.clearTimeout(analyze.timer);
    analyze.timer = window.setTimeout(pollJob, 3000);
    return;
  }
  setAnalyzeBusy(false);
  var box = document.getElementById("analyze-progress");
  clear(box);
  if (job.status === "failed") {
    var error = job.error || {};
    analysisFailed(describeError(error.code, error.message));
    if (job.stages && job.stages.length) { document.getElementById("analyze-progress").appendChild(stageList(job.stages)); }
    return;
  }
  saveBrief(job.brief);
  renderBrief(job.brief, job);
}

var pollMisses = 0;

function pollJob() {
  if (Date.now() - analyze.startedAt > 8 * 60 * 1000) {
    analysisFailed("Still running after 8 minutes; stopped waiting. Reload and try again.");
    return;
  }
  fetchJson("/api/analyze/" + analyze.jobId, {}, 10000, 2)
    .then(function (result) {
      pollMisses = 0;
      if (!result.ok) {
        analysisFailed("The job was lost (the free server may have restarted). Try again.");
        return;
      }
      handleJob(result.body);
    })
    .catch(function () {
      pollMisses = pollMisses + 1;
      var box = document.getElementById("analyze-progress");
      box.appendChild(el("p", "hint", "No answer from the server (" + pollMisses + "). Still trying; the analysis keeps running there."));
      analyze.timer = window.setTimeout(pollJob, 3000);
    });
}

function skippedText(item) {
  var parts = [(item.label || "listed document") + " on " + (item.host || "unknown host"),
    item.state];
  if (item.http_status) { parts.push("HTTP " + item.http_status); }
  if (item.detail && item.detail.chars !== undefined) {
    parts.push(item.detail.chars + " characters, " + item.detail.pages + " page(s)");
  }
  return parts.join(" · ");
}

function skippedNode(doc) {
  var wrap = el("div");
  var list = doc.skipped || [];
  for (var i = 0; i < list.length; i++) {
    wrap.appendChild(el("p", "note", "Not used: " + skippedText(list[i])));
  }
  return wrap;
}

function sourceText(doc) {
  var src = doc.source || {};
  if (src.kind === "company_website") {
    return "Source: company website " + doc.host + ". " + (src.detail || "");
  }
  return "Source: exchange filing host " + doc.host;
}

function docNode(label, doc) {
  var box = el("div", "doc");
  if (doc.status !== "ok") {
    box.appendChild(el("b", null, label + ": not available"));
    box.appendChild(el("span", null, doc.reason || ""));
    box.appendChild(skippedNode(doc));
    return box;
  }
  box.appendChild(el("b", null, doc.title));
  box.appendChild(el("span", null, "Date label: " + (doc.date_label || "not listed") +
    (doc.date_in_text ? " · call dated " + doc.date_in_text + " in the transcript" : "") +
    " · " + doc.host));
  box.appendChild(document.createElement("br"));
  box.appendChild(el("span", "note", sourceText(doc)));
  box.appendChild(document.createElement("br"));
  box.appendChild(safeLink(doc.url, "Open source PDF"));
  var chips = el("div", "chips");
  var source = doc.fetch.source;
  if (source === "fetched_now") {
    chips.appendChild(el("span", "chip fresh", "fetched now"));
  } else if (source === "disk_cache") {
    chips.appendChild(el("span", "chip stale", "from server cache · fetched " + doc.fetch.fetched_at));
  } else {
    chips.appendChild(el("span", "chip stale", "cached with brief · fetched " + doc.fetch.fetched_at));
  }
  if (doc.source && doc.source.kind === "company_website") {
    chips.appendChild(el("span", "chip warn", "company-hosted"));
  }
  var ex = doc.extraction;
  chips.appendChild(el("span", ex.state === "ok" ? "chip" : "chip warn", "text: " + ex.state + " · " + ex.pages_extracted + "/" + ex.page_count + " pages"));
  box.appendChild(chips);
  if (doc.skipped && doc.skipped.length) {
    box.appendChild(skippedNode(doc));
  }
  return box;
}

function citeNode(citation, passages) {
  var box = el("div", "passage cite");
  box.hidden = true;
  box.appendChild(el("div", null, citation.doc_title));
  box.appendChild(el("div", null, "Date: " + citation.doc_date));
  box.appendChild(el("div", null, "PDF page " + citation.pdf_page + (citation.speaker ? " · speaker " + citation.speaker : "")));
  var linkRow = el("div");
  linkRow.appendChild(safeLink(citation.url, "Open source document"));
  box.appendChild(linkRow);
  if (citation.context) {
    box.appendChild(el("div", "hint", "Said just before: \u201c" + citation.context + "\u201d"));
  }
  var quote = el("blockquote");
  var found = null;
  for (var i = 0; i < (passages || []).length; i++) {
    if (passages[i].source_id === citation.source_id) { found = passages[i]; }
  }
  var located = false;
  if (found) {
    located = renderPassageWithQuote(quote, found.text, citation.excerpt || "");
  }
  if (!located) {
    quote.textContent = citation.excerpt || "";
  }
  box.appendChild(quote);
  return box;
}

function briefClaimNode(headline, tags, citation, passages, extraClass) {
  var wrapper = el("div", "claim");
  var button = el("button");
  button.type = "button";
  button.setAttribute("aria-expanded", "false");
  button.appendChild(el("span", extraClass, headline));
  var tagBox = el("div", "tags");
  for (var i = 0; i < tags.length; i++) { tagBox.appendChild(el("span", "chip", tags[i])); }
  if (citation) {
    tagBox.appendChild(el("span", "chip", "PDF page " + citation.pdf_page));
  }
  tagBox.appendChild(el("span", "ok", "✓ quote verbatim"));
  tagBox.appendChild(el("span", "hint", "tap for citation"));
  button.appendChild(tagBox);
  wrapper.appendChild(button);
  var cite = null;
  if (citation) {
    cite = citeNode(citation, passages);
    wrapper.appendChild(cite);
  }
  button.addEventListener("click", function () {
    if (!cite) { return; }
    var open = cite.hidden;
    cite.hidden = !open;
    button.setAttribute("aria-expanded", open ? "true" : "false");
  });
  return wrapper;
}

function sectionNode(title, section) {
  var box = el("div", "brief-section");
  var heading = el("h3", null, title + " ");
  var statusText = { ok: "found", not_found: "not found", unverified: "unverified", unavailable: "unavailable" }[section.status] || section.status;
  heading.appendChild(el("span", section.status === "ok" ? "chip fresh" : "chip warn", statusText));
  box.appendChild(heading);
  if (section.status !== "ok") {
    var why = section.status === "not_found" ? "Not found: " : (section.status === "unavailable" ? "Unavailable: " : "Unverified: ");
    box.appendChild(el("div", "notfound", why + (section.reason || "")));
  }
  var claims = section.claims || [];
  for (var i = 0; i < claims.length; i++) {
    box.appendChild(briefClaimNode(claims[i].text, [], claims[i].citation, section.passages));
  }
  var items = section.items || [];
  for (var j = 0; j < items.length; j++) {
    var item = items[j];
    var headline = item.metric + ": " + item.value + " " + item.unit;
    var tags = [item.scope === "not stated" ? "scope: not stated in passage" : item.scope, item.period];
    box.appendChild(briefClaimNode(headline, tags, item.citation, section.passages, "metric"));
  }
  var failed = section.failed_checks || [];
  if (failed.length) {
    var details = el("details", "failed");
    details.appendChild(el("summary", null, failed.length + " statement(s) failed the automatic check and are not shown as findings"));
    for (var k = 0; k < failed.length; k++) {
      var f = failed[k];
      var line = (f.text || (f.metric + " " + f.value)) + " [" + (f.problems && f.problems.length ? f.problems.join(", ") : "quote or numbers not in source") + "]";
      details.appendChild(el("p", "note", line));
    }
    box.appendChild(details);
  }
  if (section.note) { box.appendChild(el("p", "note", section.note)); }
  return box;
}

function renderBrief(brief, job, target) {
  var box = target || document.getElementById("brief");
  clear(box);
  var ctxNote = filingVsBriefNote(brief);
  if (ctxNote) {
    var ctxBanner = el("div", "banner op");
    ctxBanner.appendChild(el("b", null, "About the filing you came from"));
    ctxBanner.appendChild(el("span", null, ctxNote));
    box.appendChild(ctxBanner);
  }
  if (brief.answer_source === "snapshot_fallback") {
    var banner = el("div", "banner op");
    banner.appendChild(el("b", null, "SAVED SNAPSHOT, not a fresh analysis"));
    banner.appendChild(el("span", null, brief.label));
    box.appendChild(banner);
    var reason = brief.fallback_reason || {};
    box.appendChild(el("p", "note", "Live analysis was not used: " + (reason.code || "unknown") + (reason.message ? " (" + reason.message + ")" : "") + "."));
    for (var a = 0; a < (brief.snapshot_answers || []).length; a++) {
      var holder = el("div");
      box.appendChild(holder);
      renderResult(brief.snapshot_answers[a], holder);
    }
    return;
  }
  var cached = brief.answer_source === "cached_brief";
  var head = el("div", cached ? "banner cached" : "banner live");
  head.appendChild(el("b", null, cached ? "CACHED brief" : "LIVE brief"));
  head.appendChild(el("span", null, brief.label));
  box.appendChild(head);
  if (brief.outcome && brief.outcome !== "complete") {
    var warn = el("div", "banner op");
    var found = (brief.sections_found || []).length;
    warn.appendChild(el("b", null, brief.outcome === "partial" ? "PARTIAL brief: " + found + " of 3 sections found" : "NO sections could be produced"));
    warn.appendChild(el("span", null, "Missing sections are marked below with the reason; nothing was filled in."));
    box.appendChild(warn);
  }

  var company = brief.company || {};
  box.appendChild(el("h2", "section", (company.name || brief.ticker) + " · NSE " + (company.nse_symbol || brief.ticker)));
  var docs = el("div", "docs");
  docs.appendChild(docNode("Annual report", brief.documents.annual_report));
  docs.appendChild(docNode("Earnings call", brief.documents.concall));
  box.appendChild(docs);
  var checks = brief.host_checks || [];
  for (var h = 0; h < checks.length; h++) {
    var hc = checks[h];
    if (!hc.allowed) {
      box.appendChild(el("p", "note", "Transcript not fetched (" + hc.label + ", " + hc.host + "): " + hc.reason));
    }
  }

  box.appendChild(sectionNode("Business overview", brief.sections.overview));
  box.appendChild(sectionNode("Management outlook", brief.sections.outlook));
  box.appendChild(sectionNode("Selected reported numbers", brief.sections.numbers));

  if (brief.notes && brief.notes.length) {
    box.appendChild(el("p", "note", brief.notes.join(" ")));
  }
  var limits = el("details", "failed");
  limits.appendChild(el("summary", null, "Limitations and timings"));
  for (var i = 0; i < (brief.limitations || []).length; i++) {
    limits.appendChild(el("p", "note", brief.limitations[i]));
  }
  limits.appendChild(el("p", "note", "Total " + brief.total_seconds + " s (when generated)."));
  limits.appendChild(stageList(brief.stages || []));
  box.appendChild(limits);
  box.appendChild(el("p", "note", brief.disclaimer || ""));
}

// ------------------------------------------------------------------ wiring

function setSegmentOn(groupId, attribute, value) {
  var buttons = document.getElementById(groupId).querySelectorAll("button");
  for (var i = 0; i < buttons.length; i++) {
    var isOn = buttons[i].getAttribute(attribute) === String(value);
    buttons[i].classList.toggle("on", isOn);
  }
}

function wire() {
  document.getElementById("btn-digest").addEventListener("click", function () {
    showTab("digest");
  });
  document.getElementById("btn-research").addEventListener("click", function () {
    showTab("research");
  });

  document.getElementById("window-seg").addEventListener("click", function (event) {
    var target = event.target.closest("button");
    if (!target) {
      return;
    }
    state.windowHours = Number(target.getAttribute("data-hours"));
    setSegmentOn("window-seg", "data-hours", state.windowHours);
    renderAlerts();
    renderFilings();
  });

  document.getElementById("vol-minus").addEventListener("click", function () {
    state.volThreshold = Math.max(1.0, Math.round((state.volThreshold - 0.5) * 10) / 10);
    document.getElementById("vol-out").textContent = state.volThreshold.toFixed(1) + "×";
    renderAlerts();
  });
  document.getElementById("vol-plus").addEventListener("click", function () {
    state.volThreshold = Math.min(10, Math.round((state.volThreshold + 0.5) * 10) / 10);
    document.getElementById("vol-out").textContent = state.volThreshold.toFixed(1) + "×";
    renderAlerts();
  });
  document.getElementById("move-minus").addEventListener("click", function () {
    state.moveThreshold = Math.max(1, state.moveThreshold - 1);
    document.getElementById("move-out").textContent = "±" + state.moveThreshold + "%";
    renderAlerts();
  });
  document.getElementById("move-plus").addEventListener("click", function () {
    state.moveThreshold = Math.min(20, state.moveThreshold + 1);
    document.getElementById("move-out").textContent = "±" + state.moveThreshold + "%";
    renderAlerts();
  });

  document.getElementById("refresh-btn").addEventListener("click", refreshPrices);
  document.getElementById("filings-status").addEventListener("click", function (event) {
    if (event.target.id === "filings-btn") {
      refreshFilings();
    }
  });

  document.getElementById("portfolio").addEventListener("click", function (event) {
    var target = event.target.closest("button.stock");
    if (target) {
      openSheet(target.getAttribute("data-symbol"));
    }
  });
  document.getElementById("sheet-close").addEventListener("click", closeSheet);
  document.getElementById("sheet").addEventListener("click", function (event) {
    if (event.target.id === "sheet") {
      closeSheet();
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeSheet();
    }
  });

  document.getElementById("questions").addEventListener("click", function (event) {
    var target = event.target.closest("button.qbtn");
    if (!target) {
      return;
    }
    state.selectedQuestion = target.getAttribute("data-id");
    renderQuestions();
  });
  document.getElementById("mode-seg").addEventListener("click", function (event) {
    var target = event.target.closest("button");
    if (!target) {
      return;
    }
    state.mode = target.getAttribute("data-mode");
    setSegmentOn("mode-seg", "data-mode", state.mode);
    updateRunButton();
  });
  document.getElementById("run-btn").addEventListener("click", runResearch);
  document.getElementById("analyze-form").addEventListener("submit", startAnalysis);
}

wire();
loadDigest();
