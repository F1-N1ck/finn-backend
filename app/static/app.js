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
  mode: "cached"
};

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
  fetch("/api/digest")
    .then(function (response) {
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      return response.json();
    })
    .then(function (data) {
      state.digest = data;
      renderAll();
    })
    .catch(function (error) {
      var status = document.getElementById("status");
      status.textContent = "Could not load the digest (" + error.message + "). Pull to retry.";
    });
}

// ------------------------------------------------------------------ digest rendering

function renderStatus() {
  var status = document.getElementById("status");
  status.className = "status";
  if (state.liveRows) {
    status.classList.add("live");
    var partial = false;
    for (var i = 0; i < state.liveRows.length; i++) {
      if (state.liveRows[i].partial) {
        partial = true;
      }
    }
    var text = "LIVE prices, fetched " + formatTime(state.liveFetchedUtc) + ". Filings are still the snapshot from " +
      formatTime(state.digest.as_of_utc) + ".";
    if (partial) {
      text = text + " Today's session may be in progress, so volume ratios can read low.";
    }
    status.textContent = text;
  } else {
    var meta = state.digest.meta;
    status.textContent = "Snapshot, not live. Filings as of " + formatTime(state.digest.as_of_utc) +
      " (NSE). Prices to the " + meta.prices.last_bar + " close (Yahoo). Tap Refresh for live prices.";
    status.title = state.digest.label;
  }
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
  return card;
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

  content.appendChild(el("h2", "section", "Filings in snapshot (7 days)"));
  var stack = el("div", "stack");
  var found = 0;
  for (var j = 0; j < state.digest.filings.length; j++) {
    if (state.digest.filings[j].symbol === symbol) {
      stack.appendChild(filingCard(state.digest.filings[j], false));
      found = found + 1;
    }
  }
  if (found === 0) {
    var note = entry.no_filings_note || "No filings in the snapshot window.";
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
  box.appendChild(el("p", "note", "Analysing " + job.symbol + " · " + elapsed + " s elapsed. You can leave this tab open; nothing is sent twice."));
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
  clear(document.getElementById("brief"));
  setAnalyzeBusy(true);
  analyze.startedAt = Date.now();
  fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker: ticker })
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
        if (result.status === 422 && !detail.code) { code = "invalid_ticker"; }
        showAnalyzeError(describeError(code, detail.message));
        setAnalyzeBusy(false);
        return;
      }
      handleJob(result.body);
    })
    .catch(function () {
      showAnalyzeError("Network error. Try again.");
      setAnalyzeBusy(false);
    });
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
    showAnalyzeError(describeError(error.code, error.message));
    if (job.stages && job.stages.length) { document.getElementById("analyze-progress").appendChild(stageList(job.stages)); }
    return;
  }
  renderBrief(job.brief, job);
}

function pollJob() {
  if (Date.now() - analyze.startedAt > 8 * 60 * 1000) {
    showAnalyzeError("Still running after 8 minutes; stopped waiting. Reload and try again.");
    setAnalyzeBusy(false);
    return;
  }
  fetch("/api/analyze/" + analyze.jobId)
    .then(function (response) {
      return response.json().then(function (body) {
        return { ok: response.ok, body: body };
      });
    })
    .then(function (result) {
      if (!result.ok) {
        showAnalyzeError("The job was lost (the free server may have restarted). Try again.");
        setAnalyzeBusy(false);
        return;
      }
      handleJob(result.body);
    })
    .catch(function () {
      analyze.timer = window.setTimeout(pollJob, 5000);
    });
}

function docNode(label, doc) {
  var box = el("div", "doc");
  if (doc.status !== "ok") {
    box.appendChild(el("b", null, label + ": not available"));
    box.appendChild(el("span", null, doc.reason || ""));
    return box;
  }
  box.appendChild(el("b", null, doc.title));
  box.appendChild(el("span", null, "Date label: " + (doc.date_label || "not listed") +
    (doc.date_in_text ? " · call dated " + doc.date_in_text + " in the transcript" : "") +
    " · " + doc.host));
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
  var ex = doc.extraction;
  chips.appendChild(el("span", ex.state === "ok" ? "chip" : "chip warn", "text: " + ex.state + " · " + ex.pages_extracted + "/" + ex.page_count + " pages"));
  box.appendChild(chips);
  if (doc.skipped && doc.skipped.length) {
    var states = [];
    for (var i = 0; i < doc.skipped.length; i++) { states.push((doc.skipped[i].label || "") + " " + doc.skipped[i].state); }
    box.appendChild(el("p", "note", "Skipped newer candidate: " + states.join("; ")));
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
      var line = (f.text || (f.metric + " " + f.value)) + " [" + (f.problems ? f.problems.join(", ") : "quote or numbers not in source") + "]";
      details.appendChild(el("p", "note", line));
    }
    box.appendChild(details);
  }
  if (section.note) { box.appendChild(el("p", "note", section.note)); }
  return box;
}

function renderBrief(brief, job) {
  var box = document.getElementById("brief");
  clear(box);
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
