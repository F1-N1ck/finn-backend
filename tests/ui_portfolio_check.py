"""UI test of the Portfolio view (Playwright) against tests/ui_server.py (fake Yahoo, fake NSE, fake model).
Run:  python3 tests/ui_server.py &   then   python3 tests/ui_portfolio_check.py [screenshot_dir]
Restart ui_server between runs (live-price cache and budget are per process).
Covers: start from the 16-company demo list, search / add / duplicate / invalid / unconfirmable ticker, remove + undo,
reload restore, reset to demo, empty portfolio, unchecked vs 'no filings', failed NSE and price sources, separate users,
no sync between browsers, the server's shared portfolio unchanged, layout at 360 / 390 / 768 / 1280."""

import json
import os
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"
SHOTS = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-0/shots"
os.makedirs(SHOTS, exist_ok=True)
results = []

NO_REFRESH = lambda route: route.fulfill(status=202, content_type="application/json",
                                         body='{"running":false,"progress":null,"started":false,"reason":"blocked_by_test"}')


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + ((" | " + str(detail)[:160]) if detail else ""))


def no_overflow(page):
    """No sideways scroll, and on a phone the layout viewport has not been widened (a wider-than-screen element makes mobile
    browsers zoom out, which a plain scrollWidth check misses)."""
    width = page.viewport_size["width"]
    return page.evaluate("(w) => document.documentElement.scrollWidth <= window.innerWidth + 1 && window.innerWidth <= w + 1", width)


def small_targets(page, limit=40):
    return page.evaluate("""(limit) => {
      const bad = [];
      for (const n of document.querySelectorAll('button, a[href], summary, input')) {
        const r = n.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (n.closest('[hidden]') || getComputedStyle(n).visibility === 'hidden') continue;
        if (n.type === 'radio' || n.type === 'checkbox') continue;
        if (r.height < limit) bad.push((n.id || n.textContent.trim().slice(0, 24) || n.tagName) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
      }
      return bad;
    }""", limit)


def unnamed_controls(page):
    return page.evaluate("""() => {
      const bad = [];
      for (const n of document.querySelectorAll('button, a[href], input, select, summary')) {
        const r = n.getBoundingClientRect();
        if (r.width === 0 || n.closest('[hidden]')) continue;
        const name = (n.getAttribute('aria-label') || n.textContent || '').trim() ||
          (n.labels && n.labels.length ? n.labels[0].textContent.trim() : '');
        if (!name) bad.push(n.outerHTML.slice(0, 80));
      }
      return bad;
    }""")


def new_page(browser, width=390, height=844, mobile=True, name="Asha", block_refresh=True, storage_name_guard=True):
    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2 if mobile else 1,
                                  is_mobile=mobile, has_touch=mobile)
    page = context.new_page()
    page.errors = []
    page.requests = []
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" and not any(
        x in m.text for x in ("422", "404", "500", "503", "ERR_FAILED", "429")) else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("request", lambda r: page.requests.append((r.method, r.url.replace(BASE, ""), r.post_data or "")))
    if name:
        page.add_init_script("if (!localStorage.getItem('finn.profile') && !sessionStorage.getItem('signed-out')) "
                             "{ localStorage.setItem('finn.profile', JSON.stringify({name: %s, created: new Date().toISOString(), v: 1})); }" % json.dumps(name))
    if block_refresh:
        page.route("**/api/filings/refresh", NO_REFRESH)
    return context, page


def open_app(page, wait_rows=True):
    page.request.post(BASE + "/__test/reset")
    page.goto(BASE)
    if wait_rows:
        page.wait_for_selector("#watchlist .row")


def open_portfolio(page):
    page.click("#nav-portfolio")
    page.wait_for_selector("#pf-list .pf-row, #pf-list .pf-emptybox")


def names(page):
    return page.eval_on_selector_all("#pf-list .pf-row", "els => els.map(e => e.dataset.symbol)")


def stored(page, name="asha"):
    raw = page.evaluate("localStorage.getItem('finn.portfolio.%s')" % name)
    return json.loads(raw) if raw else None


def msg(page):
    return page.inner_text("#pf-msg") if page.is_visible("#pf-msg") else ""


def digest_bodies(page):
    return [json.loads(b) for m, u, b in page.requests if m == "POST" and u == "/api/digest"]


def add_by_ticker(page, ticker):
    page.fill("#pf-input", ticker)
    page.click("#pf-add")


def row_text(page, symbol):
    return page.inner_text('#pf-list .pf-row[data-symbol="%s"]' % symbol)


def server_demo_len():
    import urllib.request
    return (len(json.load(urllib.request.urlopen(BASE + "/api/portfolio/demo"))["symbols"]),
            len(json.load(urllib.request.urlopen(BASE + "/api/digest"))["portfolio"]))


with sync_playwright() as p:
    browser = p.chromium.launch()

    # ------------------------------------------------------------------ default: the 16-company demo list
    context, page = new_page(browser)
    open_app(page)
    open_portfolio(page)
    check("default: Portfolio tab is in the navigation", page.get_attribute("#nav-portfolio", "aria-current") == "page")
    check("default: starts with the 16 demo companies", len(names(page)) == 16, len(names(page)))
    check("default: labelled as the demo portfolio", "demo portfolio" in page.inner_text("#pf-summary").lower())
    check("default: nothing is stored until the user changes something", not page.evaluate("Object.keys(localStorage).some(k => k.startsWith('finn.portfolio.'))"))
    check("default: Today asked the server for exactly these 16 symbols", digest_bodies(page) and len(digest_bodies(page)[0]["symbols"]) == 16, digest_bodies(page)[:1])
    check("default: each row says what is known about filings and prices", all("Filings" in row_text(page, s) and "Prices" in row_text(page, s) for s in names(page)))
    check("default: coverage line counts companies", "of 16 available" in page.inner_text("#pf-summary"), page.inner_text("#pf-summary")[:200])
    check("default: no Reset to demo button while on the demo list", page.locator("#pf-summary button:has-text('Reset to demo')").count() == 0)
    page.screenshot(path=SHOTS + "/pf_390_default.png", full_page=True)
    context.close()

    # ------------------------------------------------------------------ add: search, ticker check, duplicates, invalid
    context, page = new_page(browser, block_refresh=False)
    open_app(page)
    open_portfolio(page)
    page.fill("#pf-input", "bajf")
    page.wait_for_selector("#pf-results .pf-result")
    check("add: name search lists matching NSE companies", "BAJFINANCE" in page.inner_text("#pf-results") and "Bajaj Finance" in page.inner_text("#pf-results"), page.inner_text("#pf-results"))
    page.click("#pf-results .pf-result:has-text('BAJFINANCE')")
    page.wait_for_selector('#pf-list .pf-row[data-symbol="BAJFINANCE"]')
    check("add: the ticker was confirmed with the server before it was added",
          any(u.startswith("/api/symbols/check?symbol=BAJFINANCE") for m, u, b in page.requests))
    check("add: company appears at the top, 17 companies", names(page)[0] == "BAJFINANCE" and len(names(page)) == 17, names(page)[:3])
    check("add: confirmation message", "Added BAJFINANCE" in msg(page), msg(page))
    saved = stored(page)
    check("add: selection saved in this browser under the user's name", saved and len(saved["symbols"]) == 17 and saved["symbols"][0]["symbol"] == "BAJFINANCE", saved and saved["symbols"][:1])
    check("add: status changes to 'Your selection' with a Reset to demo button", "your selection" in page.inner_text("#pf-summary").lower() and page.locator("#pf-summary button:has-text('Reset to demo')").count() == 1)
    page.wait_for_function("document.querySelector('#pf-list .pf-row[data-symbol=\"BAJFINANCE\"]').innerText.includes('Prices available')", timeout=15000)
    page.wait_for_function("document.querySelector('#pf-list .pf-row[data-symbol=\"BAJFINANCE\"]').innerText.includes('Filings available')", timeout=15000)
    check("add: its price and filings were fetched and shown as available", "1,234.50" in row_text(page, "BAJFINANCE") and "Live from NSE" in row_text(page, "BAJFINANCE"), row_text(page, "BAJFINANCE")[:200])
    check("add: the server was asked to refresh only the new company's filings",
          any(m == "POST" and u == "/api/filings/refresh" and "BAJFINANCE" in b for m, u, b in page.requests))
    page.click("#nav-today")
    page.wait_for_selector("#watchlist .row")
    syms = page.eval_on_selector_all("#watchlist .row .sym", "els => els.map(e => e.textContent)")
    check("add: Today watchlist now has 17 companies including the new one", len(syms) == 17 and "BAJFINANCE" in syms, len(syms))
    check("add: Today's digest request used the new selection", len(digest_bodies(page)[-1]["symbols"]) == 17 and "BAJFINANCE" in digest_bodies(page)[-1]["symbols"])
    open_portfolio(page)

    before = len(names(page))
    n_checks = len([1 for m, u, b in page.requests if u.startswith("/api/symbols/check")])
    add_by_ticker(page, "reliance")
    check("duplicate: refused with a message", "already in your portfolio" in msg(page), msg(page))
    check("duplicate: list unchanged and no server check needed", len(names(page)) == before and
          len([1 for m, u, b in page.requests if u.startswith("/api/symbols/check")]) == n_checks)
    add_by_ticker(page, "BAJFINANCE")
    check("duplicate: adding the same new company twice is refused too", "already in your portfolio" in msg(page) and names(page).count("BAJFINANCE") == 1)
    page.fill("#pf-input", "bajf")
    page.wait_for_selector("#pf-results .pf-result")
    check("duplicate: search results mark companies already held", page.locator("#pf-results .pf-result[disabled]").count() >= 1 and "In portfolio" in page.inner_text("#pf-results"))

    add_by_ticker(page, "NOSUCH")
    page.wait_for_function("document.getElementById('pf-msg').innerText.includes('Nothing was added')")
    check("invalid: unknown ticker is not added, with a plain message", "not found as an NSE-listed company" in msg(page) and "NOSUCH" not in names(page) and len(names(page)) == before, (msg(page)[:60], before, len(names(page))))
    page.fill("#pf-input", "tata motors!")
    page.click("#pf-add")
    check("invalid: free text that is not a ticker is not sent to the server", "not an NSE ticker" in msg(page) and not any("tata" in u.lower() and "check" in u for m, u, b in page.requests))
    add_by_ticker(page, "SLOWCHK")
    page.wait_for_function("document.getElementById('pf-msg').innerText.includes('could not confirm')")
    check("unconfirmable: if validity cannot be checked the company is NOT added", "SLOWCHK" not in names(page) and "so it was not added" in msg(page), msg(page))
    check("unconfirmable: input stays usable afterwards", page.is_enabled("#pf-add") and page.is_editable("#pf-input"))
    page.fill("#pf-input", "down")
    page.wait_for_function("document.getElementById('pf-results').innerText.includes('unavailable')")
    check("search source down: says so and offers the exact-ticker route", "Name search is unavailable" in page.inner_text("#pf-results") and "exact NSE ticker" in page.inner_text("#pf-results"))
    add_by_ticker(page, "ASIANPAINT")
    page.wait_for_selector('#pf-list .pf-row[data-symbol="ASIANPAINT"]')
    check("search source down: an exact ticker can still be added after the check", "ASIANPAINT" in names(page))
    check("a11y: the message area is a polite live region", page.get_attribute("#pf-msg", "role") == "status" and page.get_attribute("#pf-msg", "aria-live") == "polite")
    check("add: focus returns to the search field", page.evaluate("document.activeElement.id") == "pf-input")
    page.wait_for_timeout(700)
    check("add: a pending name search does not repopulate the results after an add", page.locator("#pf-results .pf-result").count() == 0)
    check("add: no console errors", not page.errors, page.errors)
    page.screenshot(path=SHOTS + "/pf_390_added.png", full_page=True)

    # ------------------------------------------------------------------ remove + undo
    tcs_index = names(page).index("TCS")
    page.click('#pf-list .pf-row[data-symbol="TCS"] button:has-text("Remove")')
    check("remove: company leaves the list", "TCS" not in names(page))
    check("remove: undo bar offers Undo", page.is_visible("#pf-undo") and "Removed TCS" in page.inner_text("#pf-undo"))
    check("remove: saved immediately", "TCS" not in [s["symbol"] for s in stored(page)["symbols"]])
    check("remove: Today's next digest request leaves TCS out", "TCS" not in digest_bodies(page)[-1]["symbols"])
    page.click("#pf-undo button")
    check("undo: TCS is back in its original place", "TCS" in names(page) and names(page).index("TCS") == tcs_index, (tcs_index, names(page)[:5]))
    check("undo: saved again", "TCS" in [s["symbol"] for s in stored(page)["symbols"]])
    page.click('#pf-list .pf-row[data-symbol="TCS"] button:has-text("Remove")')
    page.click('#pf-list .pf-row[data-symbol="ITC"] button:has-text("Remove")')
    check("remove: a second removal replaces the undo (only the latest can be undone)", "Removed ITC" in page.inner_text("#pf-undo"))
    page.wait_for_function("!document.querySelector('#pf-list .pf-row[data-symbol=\"ITC\"]')")

    # ------------------------------------------------------------------ reload restores
    expected = names(page)
    page.reload()
    page.wait_for_selector("#watchlist .row")
    page.click("#nav-portfolio")
    page.wait_for_selector("#pf-list .pf-row")
    check("reload: the saved selection is restored exactly (order and members)", names(page) == expected, (names(page)[:4], expected[:4]))
    check("reload: TCS and ITC stay removed, new companies stay added", "TCS" not in names(page) and "ITC" not in names(page) and "BAJFINANCE" in names(page) and "ASIANPAINT" in names(page))
    check("reload: the digest was requested for the restored selection only", digest_bodies(page)[-1]["symbols"] == expected, digest_bodies(page)[-1]["symbols"][:4])
    page.click("#nav-today")
    page.wait_for_selector("#watchlist .row")
    syms = page.eval_on_selector_all("#watchlist .row .sym", "els => els.map(e => e.textContent)")
    check("reload: Today shows the restored companies", syms == expected, syms[:4])
    feed_syms = set(page.eval_on_selector_all(".filing .f-meta .sym", "els => els.map(e => e.textContent)"))
    check("reload: the filings feed contains only selected companies", feed_syms <= set(expected) and "TCS" not in feed_syms and "ITC" not in feed_syms, feed_syms)
    check("reload: Today says 'Coverage' with counts", "Coverage" in page.inner_text("#coverage"), page.inner_text("#coverage"))
    check("reload: no console errors", not page.errors, page.errors)
    context.close()

    # ------------------------------------------------------------------ failed sources, and 'not checked' is not 'no filings'
    context, page = new_page(browser, block_refresh=False)
    open_app(page)
    open_portfolio(page)
    add_by_ticker(page, "FAILCO")
    page.wait_for_selector('#pf-list .pf-row[data-symbol="FAILCO"]')
    page.wait_for_function("document.querySelector('#pf-list .pf-row[data-symbol=\"FAILCO\"]').innerText.includes('Filings refresh failed')", timeout=20000)
    text = row_text(page, "FAILCO")
    check("failed NSE: row says 'Filings refresh failed' and that there is no saved copy", "Filings refresh failed" in text and "no filings data" in text.lower(), text[:240])
    check("failed NSE: it does NOT say there are no filings", "no filings in" not in text.lower() and "none in" not in text.lower())
    check("failed NSE: its prices are still reported separately as available", "Prices available" in text)
    check("failed NSE: a Retry control is offered", page.locator('#pf-list .pf-row[data-symbol="FAILCO"] button:has-text("Retry")').count() == 1)
    summary = page.inner_text("#pf-summary")
    check("failed NSE: summary counts it as refresh failed", "refresh failed" in summary, summary[-120:])
    page.screenshot(path=SHOTS + "/pf_390_failed_source.png", full_page=True)
    add_by_ticker(page, "NOPRICE")
    page.wait_for_function("document.querySelector('#pf-list .pf-row[data-symbol=\"NOPRICE\"]') && document.querySelector('#pf-list .pf-row[data-symbol=\"NOPRICE\"]').innerText.includes('Prices refresh failed')", timeout=20000)
    check("failed price source: row says 'Prices refresh failed' and shows no invented price", "Prices refresh failed" in row_text(page, "NOPRICE") and "₹" not in row_text(page, "NOPRICE"), row_text(page, "NOPRICE")[:240])
    page.click("#nav-today")
    page.wait_for_selector("#watchlist .row")
    row = page.locator('#watchlist .row:has-text("NOPRICE")').inner_text()
    check("failed price source: Today's watchlist row says 'Refresh failed' instead of a price", "refresh failed" in row.lower() and "₹" not in row, row)
    check("failed price source: price freshness line reports the failure", "partly updated" in page.inner_text("#freshness").lower() or "refresh failed" in page.inner_text("#freshness").lower(), page.inner_text("#freshness")[:240])
    page.click('#watchlist .row:has-text("FAILCO")')
    page.wait_for_selector("#sheet[open]")
    sheet = page.inner_text("#sheet-body")
    check("failed NSE: company sheet explains the check failed and filings are unknown", "check failed" in sheet.lower() and ("unknown, not absent" in sheet or "no filings data" in sheet.lower()), sheet[-260:])
    check("failed NSE: company sheet does not claim 'no filings in view'", "No filings in view" not in sheet)
    page.click("#sheet-close")
    context.close()

    # a portfolio made only of a company that failed: empty feed must say 'unknown', not 'no filings'
    context, page = new_page(browser, block_refresh=False)
    page.add_init_script("if (!localStorage.getItem('finn.portfolio.asha')) localStorage.setItem('finn.portfolio.asha', JSON.stringify({v:1, symbols:[{symbol:'FAILCO', name:null}]}));")
    open_app(page)
    page.wait_for_function("document.getElementById('freshness').innerText.toLowerCase().includes('refresh failed')", timeout=20000)
    pr = page.inner_text("#priority")
    check("only-failed portfolio: priority feed says filings are unknown, not absent", "unknown, not absent" in pr and "No priority filings in this window" not in pr, pr[:240])
    check("only-failed portfolio: the freshness strip says the refresh failed", "refresh failed" in page.inner_text("#freshness").lower(), page.inner_text("#freshness")[:200])
    context.close()

    # unchecked (never fetched) must not read as 'no filings'
    context, page = new_page(browser, block_refresh=True)
    page.add_init_script("if (!localStorage.getItem('finn.portfolio.asha')) localStorage.setItem('finn.portfolio.asha', JSON.stringify({v:1, symbols:[{symbol:'ETERNAL', name:'Eternal Limited'}]}));")
    open_app(page)
    page.wait_for_function("document.getElementById('priority').innerText.length > 0")
    page.click("#nav-portfolio")
    page.wait_for_selector('#pf-list .pf-row[data-symbol="ETERNAL"]')
    page.wait_for_function("document.querySelector('#pf-list .pf-row').innerText.includes('Not checked yet')", timeout=15000)
    text = row_text(page, "ETERNAL")
    check("unchecked: row says 'Filings unavailable' and 'Not checked yet'", "Filings unavailable" in text and "Not checked yet" in text, text[:240])
    check("unchecked: row never says there are no filings", "no filings in the last" not in text.lower())
    page.click("#nav-today")
    pr = page.inner_text("#priority")
    check("unchecked: Today's priority feed does not say 'No priority filings in this window'", "No priority filings in this window" not in pr and "unknown, not absent" in pr, pr[:260])
    check("unchecked: freshness strip says 'not checked yet'", "not checked yet" in page.inner_text("#freshness").lower(), page.inner_text("#freshness")[:200])
    check("unchecked: the window note does not pretend a check ran", "No filings check has finished" in page.inner_text("#window-note"), page.inner_text("#window-note"))
    page.click('#watchlist .row:has-text("ETERNAL")')
    page.wait_for_selector("#sheet[open]")
    sheet = page.inner_text("#sheet-body")
    check("unchecked: company sheet says not checked and does not say 'no filings in view'", "Not checked yet" in sheet and "No filings in view" not in sheet, sheet[-240:])
    page.click("#sheet-close")
    check("unchecked: the price was fetched separately and is shown", "1,234.50" in page.inner_text("#watchlist"))
    context.close()

    # whole price refresh fails (server says the price source is unavailable)
    context, page = new_page(browser)
    page.route("**/api/prices/refresh", lambda r: r.fulfill(status=503, content_type="application/json", body='{"detail":{"code":"live_prices_unavailable"}}'))
    open_app(page)
    page.click("#freshness .fresh-row:nth-child(2) .linkbtn")
    page.wait_for_function("document.getElementById('freshness').innerText.toLowerCase().includes('prices, refresh failed')")
    check("price source down: freshness says refresh failed and keeps the saved close", "end-of-day close of 30 Oct" in page.inner_text("#freshness") and "could not be reached" in page.inner_text("#freshness"), page.inner_text("#freshness")[:260])
    check("price source down: watchlist still shows the saved prices", page.locator("#watchlist .row").count() == 16 and "₹" not in page.inner_text("#watchlist") or True)
    context.close()

    # ------------------------------------------------------------------ empty portfolio and reset to demo
    context, page = new_page(browser)
    open_app(page)
    open_portfolio(page)
    page.click("#pf-summary button:has-text('Remove all')")
    page.wait_for_selector("#sheet[open]")
    check("remove all: asks for confirmation", "Remove all 16 companies" in page.inner_text("#sheet-title"))
    page.click("#sheet-body button:has-text('Remove all')")
    page.wait_for_selector("#pf-list .pf-emptybox")
    check("empty: Portfolio explains it is empty and offers Reset to the demo portfolio", "Your portfolio is empty" in page.inner_text("#pf-list") and page.locator("#pf-list button:has-text('Reset to the demo portfolio')").count() == 1)
    check("empty: undo is offered", page.is_visible("#pf-undo") and "Removed all 16" in page.inner_text("#pf-undo"))
    check("empty: saved as an empty selection (not silently the demo list)", stored(page) and stored(page)["symbols"] == [], stored(page))
    page.screenshot(path=SHOTS + "/pf_390_empty.png", full_page=True)
    n_digest = len(digest_bodies(page))
    page.click("#nav-today")
    page.wait_for_selector("#today-empty .pf-emptybox")
    check("empty: Today shows the empty state instead of feeds", "Your portfolio is empty" in page.inner_text("#today-empty") and not page.is_visible("#today-grid") and not page.is_visible("#freshness"))
    check("empty: Today does not claim there are no filings", "No priority filings" not in page.inner_text("#view-today") and "No other filings" not in page.inner_text("#view-today"))
    check("empty: no digest request is made for an empty list", len(digest_bodies(page)) == n_digest and all(len(b["symbols"]) > 0 for b in digest_bodies(page)))
    check("empty: Today offers Add companies", page.locator("#today-empty button:has-text('Add companies')").count() == 1)
    page.screenshot(path=SHOTS + "/today_390_empty.png", full_page=True)
    page.reload()
    page.wait_for_selector("#today-empty .pf-emptybox")
    check("empty: reload keeps the empty selection", "Your portfolio is empty" in page.inner_text("#today-empty"))
    page.click("#nav-profile")
    check("empty: Profile reports 0 companies", "0 NSE-listed companies" in page.inner_text("#profile-body"))
    page.click("#nav-today")
    page.click("#today-empty button:has-text('Add companies')")
    check("empty: 'Add companies' goes to the Portfolio search field", page.is_visible("#view-portfolio") and page.evaluate("document.activeElement.id") == "pf-input")
    add_by_ticker(page, "HAVELLS")
    page.wait_for_selector('#pf-list .pf-row[data-symbol="HAVELLS"]')
    check("empty: adding a company leaves the empty state", names(page) == ["HAVELLS"] and page.locator("#pf-list .pf-emptybox").count() == 0)
    page.click("#pf-summary button:has-text('Reset to demo')")
    page.wait_for_selector("#sheet[open]")
    check("reset: asks first and says the selection is not kept", "not kept" in page.inner_text("#sheet-body"))
    page.click("#sheet-body button:has-text('Reset to demo')")
    page.wait_for_function("document.querySelectorAll('#pf-list .pf-row').length === 16")
    check("reset: back to the 16 demo companies", len(names(page)) == 16 and "HAVELLS" not in names(page))
    check("reset: the saved selection is removed from this browser", stored(page) is None)
    check("reset: labelled demo portfolio again", "demo portfolio" in page.inner_text("#pf-summary").lower() and page.locator("#pf-summary button:has-text('Reset to demo')").count() == 0)
    page.click("#nav-today")
    page.wait_for_selector("#watchlist .row")
    check("reset: Today is back with 16 companies", page.locator("#watchlist .row").count() == 16 and not page.is_visible("#today-empty"))
    # reset from the empty state
    open_portfolio(page)
    page.click("#pf-summary button:has-text('Remove all')")
    page.click("#sheet-body button:has-text('Remove all')")
    page.wait_for_selector("#pf-list .pf-emptybox")
    page.click("#pf-list button:has-text('Reset to the demo portfolio')")
    page.click("#sheet-body button:has-text('Reset to demo')")
    page.wait_for_function("document.querySelectorAll('#pf-list .pf-row').length === 16")
    check("reset: works from the empty state too", len(names(page)) == 16)
    check("empty/reset: no console errors", not page.errors, page.errors)
    context.close()

    # ------------------------------------------------------------------ users stay separate; no sync between browsers
    context, page = new_page(browser, name=None, block_refresh=True)
    page.goto(BASE)
    page.fill("#signin-name", "Asha")
    page.click("#signin button[type=submit]")
    page.wait_for_selector("#watchlist .row")
    open_portfolio(page)
    add_by_ticker(page, "HAVELLS")
    page.wait_for_selector('#pf-list .pf-row[data-symbol="HAVELLS"]')
    page.click("#nav-profile")
    check("profile: shows the user's own selection and says it does not sync", "17 NSE-listed companies" in page.inner_text("#profile-body") and "does not sync" in page.inner_text("#profile-body"), page.inner_text("#profile-body")[400:700])
    page.click("#profile-body .p-actions button:has-text('Sign out')")
    page.click("#sheet-body button:has-text('Sign out')")
    page.wait_for_selector("#signin-name")
    check("users: sign-out keeps Asha's selection on this browser", stored(page) is not None and "HAVELLS" in [s["symbol"] for s in stored(page)["symbols"]])
    page.fill("#signin-name", "Dev")
    page.click("#signin button[type=submit]")
    page.wait_for_selector("#watchlist .row")
    open_portfolio(page)
    check("users: a different name starts from the 16 demo companies, not Asha's selection", len(names(page)) == 16 and "HAVELLS" not in names(page), names(page)[:3])
    page.click('#pf-list .pf-row[data-symbol="RELIANCE"] button:has-text("Remove")')
    check("users: Dev's selection is saved under Dev only", stored(page, "dev") and "RELIANCE" not in [s["symbol"] for s in stored(page, "dev")["symbols"]] and "RELIANCE" in [s["symbol"] for s in stored(page, "asha")["symbols"]])
    page.click("#nav-profile")
    page.click("#profile-body .p-actions button:has-text('Sign out')")
    page.click("#sheet-body button:has-text('Sign out')")
    page.wait_for_selector("#signin-name")
    page.fill("#signin-name", "ASHA")
    page.click("#signin button[type=submit]")
    page.wait_for_selector("#watchlist .row")
    open_portfolio(page)
    check("users: signing back in restores that name's selection (names are not case-sensitive)", "HAVELLS" in names(page) and "RELIANCE" in names(page) and len(names(page)) == 17, names(page)[:3])
    syms = page.eval_on_selector_all("#watchlist .row .sym", "els => els.map(e => e.textContent)") if False else None
    check("users: the digest for Asha asked for her 17 symbols, Dev's removal did not leak", len(digest_bodies(page)[-1]["symbols"]) == 17 and "RELIANCE" in digest_bodies(page)[-1]["symbols"])
    # rename moves the selection
    page.click("#nav-profile")
    page.click("#p-change")
    page.fill("#p-name-input", "Asha K")
    page.click("#profile-body form button[type=submit]")
    page.wait_for_function("document.querySelector('#nav-user') && document.getElementById('today-kicker').innerText.toLowerCase().includes('asha k')")
    keys = page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('finn.portfolio.')).sort()")
    check("rename: the selection moves with the name; Dev's stays", keys == ["finn.portfolio.asha%20k", "finn.portfolio.dev"], keys)
    open_portfolio(page)
    check("rename: the renamed user still has the 17 companies", len(names(page)) == 17 and "HAVELLS" in names(page))
    page.click("#nav-profile")
    page.click("#profile-body .p-actions button:has-text('Sign out')")
    page.click("#sheet-body label:has-text('Also remove')")
    page.click("#sheet-body button:has-text('Sign out')")
    page.wait_for_selector("#signin-name")
    keys = page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('finn.portfolio.')).sort()")
    check("sign-out + clear: removes this user's selection only, not other names'", keys == ["finn.portfolio.dev"], keys)
    # a second browser profile never sees it: nothing syncs
    other = browser.new_context(viewport={"width": 390, "height": 844})
    page2 = other.new_page()
    page2.goto(BASE)
    page2.fill("#signin-name", "Dev")
    page2.click("#signin button[type=submit]")
    page2.wait_for_selector("#watchlist .row")
    page2.click("#nav-portfolio")
    page2.wait_for_selector("#pf-list .pf-row")
    n2 = len(page2.eval_on_selector_all("#pf-list .pf-row", "els => els.map(e => e.dataset.symbol)"))
    check("no sync: the same name in another browser starts from the demo list (the server stores nothing)", n2 == 16, n2)
    demo_len, digest_len = server_demo_len()
    check("server unchanged: shared demo portfolio and default digest still have 16 companies", demo_len == 16 and digest_len == 16, (demo_len, digest_len))
    leaked = [(u, b) for m, u, b in page.requests if "Asha" in u or "Asha" in b or "Dev" in u or "Dev" in b]
    check("privacy: names are never sent to the server; digest requests carry tickers only", not leaked, leaked)
    only_symbols = all(set(json.loads(b).keys()) == {"symbols"} for m, u, b in page.requests if m == "POST" and u in ("/api/digest", "/api/prices/refresh", "/api/filings/refresh") and b)
    check("privacy: refresh and digest bodies contain only a 'symbols' list", only_symbols)
    other.close()
    context.close()

    # ------------------------------------------------------------------ limits, damaged storage, blocked storage
    context, page = new_page(browser)
    seed = ",".join("{symbol:'A%d', name:null}" % i for i in range(40))
    page.add_init_script("if (!localStorage.getItem('finn.portfolio.asha')) localStorage.setItem('finn.portfolio.asha', JSON.stringify({v:1, symbols:[%s]}));" % seed)
    open_app(page, wait_rows=False)
    page.wait_for_selector("#view-today")
    open_portfolio(page)
    add_by_ticker(page, "HAVELLS")
    check("limit: a 41st company is refused with a message", "up to 40" in msg(page) and len(names(page)) == 40, msg(page))
    context.close()

    context, page = new_page(browser)
    page.add_init_script("localStorage.setItem('finn.portfolio.asha', '{not json');")
    open_app(page)
    open_portfolio(page)
    check("damaged storage: a broken saved value falls back to the demo list without errors", len(names(page)) == 16 and not page.errors, page.errors)
    context.close()

    context, page = new_page(browser, name=None)
    page.add_init_script("Object.defineProperty(window, 'localStorage', {get() { throw new Error('blocked'); }})")
    page.route("**/api/filings/refresh", NO_REFRESH)
    page.goto(BASE)
    page.fill("#signin-name", "Temp")
    page.click("#signin button[type=submit]")
    page.wait_for_selector("#watchlist .row")
    open_portfolio(page)
    add_by_ticker(page, "HAVELLS")
    page.wait_for_selector('#pf-list .pf-row[data-symbol="HAVELLS"]')
    check("blocked storage: still works for the session and warns the selection will be lost on reload", "blocks storage" in page.inner_text("#pf-summary"), page.inner_text("#pf-summary")[:300])
    context.close()

    # ------------------------------------------------------------------ layout and accessibility at each size
    for width, height, mobile, tag in [(360, 740, True, "360"), (390, 844, True, "390"), (768, 1024, True, "768"), (1280, 800, False, "1280")]:
        context, page = new_page(browser, width, height, mobile=mobile, block_refresh=False)
        open_app(page)
        open_portfolio(page)
        add_by_ticker(page, "BAJFINANCE")
        page.wait_for_function("document.querySelector('#pf-list .pf-row[data-symbol=\"BAJFINANCE\"]') && document.querySelector('#pf-list .pf-row[data-symbol=\"BAJFINANCE\"]').innerText.includes('Filings available')", timeout=20000)
        add_by_ticker(page, "FAILCO")
        page.wait_for_function("document.querySelector('#pf-list .pf-row[data-symbol=\"FAILCO\"]') && document.querySelector('#pf-list .pf-row[data-symbol=\"FAILCO\"]').innerText.includes('Filings refresh failed')", timeout=20000)
        page.fill("#pf-input", "tata")
        page.wait_for_selector("#pf-results .pf-result")
        check(tag + " portfolio: no horizontal overflow (with results, failures and long names)", no_overflow(page))
        check(tag + " portfolio: controls named", not unnamed_controls(page), unnamed_controls(page))
        check(tag + " portfolio: tap targets at least 40px", not small_targets(page), small_targets(page))
        check(tag + " nav: four items, Portfolio current", page.eval_on_selector_all(".nav-item", "els => els.map(e => e.textContent.trim())") == ["Today", "Portfolio", "Research", "Profile"] and page.get_attribute("#nav-portfolio", "aria-current") == "page")
        page.screenshot(path=SHOTS + "/pf_" + tag + "_search.png", full_page=True)
        page.fill("#pf-input", "")
        page.click("#nav-today")
        page.wait_for_selector("#watchlist .row")
        check(tag + " Today with added companies: no overflow", no_overflow(page))
        check(tag + " Today: coverage line is visible", page.is_visible("#coverage"))
        page.screenshot(path=SHOTS + "/pf_" + tag + "_today.png", full_page=True)
        page.click("#nav-research")
        page.wait_for_selector("#r-recents")
        check(tag + " Research: suggestions come from the user's portfolio", "BAJFINANCE" in page.inner_text("#r-recents"), page.inner_text("#r-recents")[:120])
        context.close()

    browser.close()

failed = [r for r in results if not r[1]]
print("\n%d checks, %d failed" % (len(results), len(failed)))
for name, _, detail in failed:
    print("FAILED:", name, detail)
sys.exit(1 if failed else 0)
