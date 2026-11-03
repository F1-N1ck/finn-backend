"""UI test of the redesigned front end (Playwright) against tests/ui_server.py (fake model, fake prices, fake NSE).
Run:  python3 tests/ui_server.py &   then   python3 tests/ui_redesign_check.py [screenshot_dir]
Restart ui_server between runs (the live-price budget is per minute).
Covers: loading, empty, failed, stale, saved-result fallback, claim evidence, guidance, accessibility, 360/390/desktop."""

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
    print(("PASS " if condition else "FAIL ") + name + ((" | " + str(detail)[:140]) if detail else ""))


def no_overflow(page):
    """No sideways scroll, and on a phone the layout viewport has not been widened (a wider-than-screen element makes mobile
    browsers zoom out, which a plain scrollWidth check misses)."""
    width = page.viewport_size["width"]
    return page.evaluate("(w) => document.documentElement.scrollWidth <= window.innerWidth + 1 && window.innerWidth <= w + 1", width)


def small_targets(page, limit=40):
    return page.evaluate("""(limit) => {
      const bad = [];
      for (const n of document.querySelectorAll('button, a[href], summary, input, label.check')) {
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


def clipped_text(page):
    """Text that is cut off: an element whose content is wider/taller than its box while overflow is hidden."""
    return page.evaluate("""() => {
      const bad = [];
      for (const n of document.querySelectorAll('.c-text, .c-meta, .f-text, .f-title, .answer, blockquote, .m-label, .nm')) {
        const r = n.getBoundingClientRect();
        if (r.width === 0) continue;
        const s = getComputedStyle(n);
        if ((s.overflow !== 'visible' || s.textOverflow === 'ellipsis' || s.webkitLineClamp !== 'none') &&
            (n.scrollWidth > n.clientWidth + 1 || n.scrollHeight > n.clientHeight + 1)) bad.push(n.className + ': ' + n.textContent.slice(0, 40));
        if (s.textOverflow === 'ellipsis') bad.push('ellipsis on ' + n.className);
      }
      return bad;
    }""")


def keyboard_focus_visible(page, selector):
    page.focus(selector)
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    return page.evaluate("""(sel) => {
      const n = document.querySelector(sel);
      if (document.activeElement !== n) return 'focus not on element';
      const s = getComputedStyle(n);
      const ok = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) >= 2;
      return ok ? 'ok' : 'no visible outline: ' + s.outlineStyle + ' ' + s.outlineWidth + ' / ' + s.boxShadow;
    }""", selector)


def new_page(browser, width, height, scheme="light", mobile=True):
    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2 if mobile else 1,
                                  is_mobile=mobile, has_touch=mobile, color_scheme=scheme)
    page = context.new_page()
    page.errors = []
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" and not any(
        x in m.text for x in ("422", "404", "500", "503", "ERR_FAILED", "429")) else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    return context, page


SIGNED_IN = ("if (!localStorage.getItem('finn.profile')) { localStorage.setItem('finn.profile', "
             "JSON.stringify({name: 'Asha', created: new Date().toISOString(), v: 1})); }")


def open_today(page, block_refresh=True):
    page.add_init_script(SIGNED_IN)  # the demo sign-in itself is tested in the landing/profile block below
    page.request.post(BASE + "/__test/reset")
    if block_refresh:
        page.route("**/api/filings/refresh", NO_REFRESH)
    page.goto(BASE)
    page.wait_for_selector("#watchlist .row")


def run_research(page, ticker, wait_running=True):
    page.fill("#r-input", ticker)
    page.click("#r-go")
    if wait_running:
        page.wait_for_selector("#r-run .run", timeout=5000)


with sync_playwright() as p:
    browser = p.chromium.launch()

    # ------------------------------------------------------------------ Today at each size
    for width, height, mobile, tag in [(390, 844, True, "390"), (360, 740, True, "360"), (1280, 800, False, "1280")]:
        context, page = new_page(browser, width, height, mobile=mobile)
        open_today(page)
        text = page.inner_text("#freshness")
        check(tag + " Today: filings freshness says saved copy, not live", "Filings" in text and "not live" in text, text[:120])
        check(tag + " Today: prices say end-of-day and not live", "end-of-day close of 30 Oct" in text and "not live" in text)
        check(tag + " Today: filings and prices are separate rows", page.locator("#freshness .fresh-row").count() == 2)
        check(tag + " Today: 16 watchlist rows", page.locator("#watchlist .row").count() == 16)
        check(tag + " Today: priority filings shown first", page.locator("#priority .filing").count() >= 1)
        check(tag + " Today: no horizontal overflow", no_overflow(page))
        titles = page.eval_on_selector_all(".filing", "els => els.map(e => e.querySelector('.f-meta .sym').textContent + '|' + e.querySelector('.f-title').textContent + '|' + (e.querySelector('.f-text')||{textContent:''}).textContent)")
        check(tag + " Today: no filing appears twice in the feed", len(titles) == len(set(titles)), len(titles))
        check(tag + " Today: threshold controls are not on the page (in filter sheet)", page.locator("#view-today input[type=range], #view-today .stepper").count() == 0)
        check(tag + " Today: no console errors", not page.errors, page.errors)
        check(tag + " Today: all controls named", not unnamed_controls(page), unnamed_controls(page))
        check(tag + " Today: tap targets >= 40px", not small_targets(page), small_targets(page))
        page.screenshot(path=SHOTS + "/" + tag + "_today.png", full_page=(width < 500))

        # filter sheet
        page.click("#open-filters")
        page.wait_for_selector("#sheet[open]")
        check(tag + " Filters: sheet has window, thresholds and routine toggle",
              page.locator("#sheet input[name=window]").count() == 3 and page.locator("#sheet .stepper").count() == 2 and
              page.locator("#sheet input[type=checkbox]").count() == 1)
        check(tag + " Filters: sheet has dialog name", page.get_attribute("#sheet", "aria-labelledby") == "sheet-title")
        check(tag + " Filters: tap targets", not small_targets(page), small_targets(page))
        page.screenshot(path=SHOTS + "/" + tag + "_filters.png")
        page.click("#sheet label:has-text('7 days')")
        page.click("#sheet button[aria-label='Raise the volume signal']")
        page.click("#sheet-close")
        check(tag + " Filters: badge counts changed filters", page.inner_text("#filter-badge") == "2", page.inner_text("#filter-badge"))
        check(tag + " Filters: wider window shows more filings", "7 days" in page.inner_text("#window-note"))
        page.reload()
        page.wait_for_selector("#watchlist .row")
        check(tag + " Filters: choices persist after reload", page.inner_text("#filter-badge") == "2")
        context.close()

    # ------------------------------------------------------------------ loading, failed digest, empty
    context, page = new_page(browser, 390, 844)
    page.add_init_script(SIGNED_IN)
    page.request.post(BASE + "/__test/reset")
    page.route("**/api/filings/refresh", NO_REFRESH)
    page.route("**/api/digest", lambda route: route.abort())
    page.goto(BASE)
    page.wait_for_selector("#freshness button:has-text('Retry')", timeout=30000)
    check("failed: digest failure is shown with Retry, no fake data", "Could not load the digest" in page.inner_text("#freshness") and
          page.locator("#watchlist .row").count() == 0)
    page.screenshot(path=SHOTS + "/390_failed_digest.png")
    page.unroute("**/api/digest")
    page.click("#freshness button:has-text('Retry')")
    page.wait_for_selector("#watchlist .row")
    check("failed: Retry recovers", page.locator("#watchlist .row").count() == 16)
    context.close()

    context, page = new_page(browser, 390, 844)
    page.add_init_script(SIGNED_IN)
    page.request.post(BASE + "/__test/reset")
    page.route("**/api/filings/refresh", NO_REFRESH)
    page.route("**/api/digest", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(dict(json.loads(route.fetch().text()), filings=[]))))
    page.goto(BASE)
    page.wait_for_selector("#watchlist .row")
    check("empty: no priority filings message", "No priority filings" in page.inner_text("#priority"), page.inner_text("#priority")[:80])
    check("empty: no other filings message", "No other filings" in page.inner_text("#others"))
    page.screenshot(path=SHOTS + "/390_empty_feed.png")
    page.click("#open-filters")
    for _ in range(9):
        page.click("#sheet button[aria-label='Raise the volume signal']")
    for _ in range(18):
        page.click("#sheet button[aria-label='Raise the price move threshold']")
    page.click("#sheet-close")
    check("empty: no market signals at extreme thresholds", "No signals" in page.inner_text("#signals"), page.inner_text("#signals")[:60])
    context.close()

    # ------------------------------------------------------------------ live refresh: live, partial, stale, company sheet
    context, page = new_page(browser, 390, 844)
    open_today(page, block_refresh=False)
    page.wait_for_function("document.querySelector('#freshness').innerText.includes('Refreshing') || document.querySelector('#freshness progress')", timeout=15000)
    check("refresh: progress is visible while filings refresh", True)
    page.wait_for_function("document.querySelector('#freshness').innerText.includes('partly updated')", timeout=60000)
    text = page.inner_text("#freshness")
    check("refresh: partial result names the failed company", "SBIN" in text and "15 of 16" in text, text[:200])
    check("refresh: live filings are labelled live", page.locator(".f-src.st-live").count() >= 1)
    check("refresh: live ITC board outcome is in priority", "ITC" in page.inner_text("#priority"))
    check("refresh: Prices row still says not live", "not live" in page.locator("#freshness .fresh-row").nth(1).inner_text())
    page.screenshot(path=SHOTS + "/390_partial_refresh.png", full_page=True)
    page.locator("#freshness .fr-text").first.click()
    page.wait_for_selector("#sheet[open]")
    sheet = page.inner_text("#sheet-body")
    check("stale: freshness sheet lists failed SBIN (snapshot kept)", "SBIN" in sheet and "refresh failed" in sheet.lower(), sheet[:300])
    check("stale: valid-empty and unverifiable-empty are told apart", "none in 7 days" in sheet and "cannot be verified" in sheet)
    check("freshness sheet: prices explained as not a live feed", "not a live exchange feed" in sheet)
    page.screenshot(path=SHOTS + "/390_freshness_sheet.png")
    page.click("#sheet-close")
    # digest reload failure after data is on screen: dated data stays, the problem is visible
    page.route("**/api/digest", lambda route: route.abort())
    page.evaluate("loadDigest()")
    page.wait_for_function("document.querySelector('#freshness').innerText.includes('Could not reload')", timeout=30000)
    check("stale: failed reload keeps data and says so", page.locator("#watchlist .row").count() == 16 and "last successful load" in page.inner_text("#freshness"))
    page.unroute("**/api/digest")
    # company sheet for SBIN
    page.click("#watchlist .row:has-text('SBIN')")
    page.wait_for_selector("#sheet[open]")
    body = page.inner_text("#sheet-body")
    check("company sheet: shows price, filing status and Research company", "Research company" in body and "SBIN" in body and "saved copy" in body, body[:200])
    page.screenshot(path=SHOTS + "/390_company_sheet.png")
    page.click("#sheet-close")
    # price refresh (live path) and price failure
    page.click("#freshness .fresh-row:nth-child(2) button:has-text('Refresh')")
    page.wait_for_function("document.querySelector('#freshness').innerText.includes('checked on Yahoo') || document.querySelector('#freshness').innerText.includes('Prices, intraday')", timeout=30000)
    check("prices: refreshed row says end-of-day, not live feed",
          "not a live feed" in page.inner_text("#freshness") or "not an exchange feed" in page.inner_text("#freshness"), page.inner_text("#freshness")[-200:])
    page.route("**/api/prices/refresh", lambda route: route.fulfill(status=502, content_type="application/json", body='{"detail":{"code":"upstream_failed"}}'))
    page.click("#freshness .fresh-row:nth-child(2) button:has-text('Refresh')")
    page.wait_for_function("document.querySelector('#freshness').innerText.includes('refresh failed')", timeout=30000)
    check("failed: price refresh failure is visible and keeps the dated close", "end-of-day close of" in page.inner_text("#freshness"))
    context.close()

    # ------------------------------------------------------------------ Research: from a filing, validation, run, evidence
    for width, height, mobile, tag in [(390, 844, True, "390"), (360, 740, True, "360"), (1280, 800, False, "1280")]:
        context, page = new_page(browser, width, height, mobile=mobile)
        open_today(page)
        first_button = page.locator("#priority .filing button:has-text('Research company')").first
        check(tag + " filing button is called Research company", first_button.count() == 1 and "Research this change" not in page.content())
        filing_sym = page.locator("#priority .filing .sym").first.inner_text()
        first_button.click()
        page.wait_for_selector("#view-research:not([hidden])")
        check(tag + " Research: ticker prefilled, research NOT started", page.input_value("#r-input") == filing_sym and page.locator("#r-run .run").count() == 0)
        ctx = page.inner_text("#r-context")
        check(tag + " Research: filing context kept, with link and honesty note",
              "has not analysed this filing" in ctx and page.locator("#r-context a[href^='https://nsearchives']").count() == 1, ctx[:160])
        check(tag + " Research: search comes first (form above context)", page.evaluate("document.querySelector('#r-form').getBoundingClientRect().top < document.querySelector('#r-context').getBoundingClientRect().top"))
        page.screenshot(path=SHOTS + "/" + tag + "_research_start.png")
        page.fill("#r-input", "bad ticker!")
        page.click("#r-go")
        check(tag + " Research: invalid ticker gets inline error", page.is_visible("#r-input-error") and page.locator("#r-run .run").count() == 0)
        context.close()

    context, page = new_page(browser, 390, 844)
    open_today(page)
    page.click("#nav-research")
    check("Research: recents/watchlist suggestions when empty", "From your portfolio" in page.inner_text("#r-recents"))
    run_research(page, "kpittech")
    running = page.inner_text("#r-run")
    check("Research: running state shows who, elapsed time and plain step names",
          "Researching KPITTECH" in running and "elapsed" in running and not any(w in running for w in ("discover", "annual_report", "concall")), running[:160])
    page.screenshot(path=SHOTS + "/390_research_running.png")
    page.wait_for_selector("#r-result .r-head", timeout=120000)
    head = page.inner_text("#r-result .r-head")
    check("result: provenance says fresh analysis with date", "Fresh analysis" in head and "generated" in head, head)
    check("result: four parts in order Business, Outlook, Financials, Sources",
          page.eval_on_selector_all("#r-result .jump a", "els => els.map(e => e.textContent)") == ["Business", "Outlook", "Financials", "Sources"])
    check("result: claims present", page.locator("#r-result .claim").count() >= 1, page.locator("#r-result .claim").count())
    claim = page.locator("#r-result .claim").first
    check("result: claim evidence hidden until opened", not claim.locator(".evidence").is_visible())
    claim.locator("summary").first.click()
    ev = claim.locator(".evidence").inner_text()
    check("result: evidence has quote, document, date, page and original link",
          "supporting quote" in ev.lower() and "PDF page" in ev and "dated" in ev.lower() and claim.locator("a[href^='https://']").count() == 1, ev[:300])
    page.screenshot(path=SHOTS + "/390_research_result.png")
    check("result: no horizontal overflow", no_overflow(page))
    check("result: long claims are not clipped", not clipped_text(page), clipped_text(page))
    check("result: tap targets", not small_targets(page), small_targets(page))
    check("result: all controls named", not unnamed_controls(page), unnamed_controls(page))
    check("result: no console errors", not page.errors, page.errors)
    check("result: no implementation words in main flow", not any(w in page.inner_text("#r-result .rsec").lower() for w in ["groq", "qwen", "token", "llm", "pipeline"]))
    check("a11y: keyboard focus ring visible on the ticker field", keyboard_focus_visible(page, "#r-go") == "ok", keyboard_focus_visible(page, "#r-go"))
    check("a11y: keyboard focus ring visible on a nav button", keyboard_focus_visible(page, "#nav-today") == "ok")
    page.evaluate("document.querySelector('#sec-sources').scrollIntoView()")
    page.screenshot(path=SHOTS + "/390_research_sources.png")
    saved = page.evaluate("localStorage.getItem('finn.brief.KPITTECH') !== null")
    check("result: brief saved on this device after success", saved)

    # fallback 1: fresh research fails, same-company saved brief on this device is shown and labelled
    page.route("**/api/analyze", lambda route: route.fulfill(status=503, content_type="application/json", body='{"detail":{"code":"busy"}}'))
    run_research(page, "KPITTECH", wait_running=False)
    page.wait_for_selector("#r-result .r-head", timeout=30000)
    notice = page.inner_text("#r-result .notice")
    check("fallback: device-saved result is labelled with date and says fresh research failed",
          "saved result, not fresh research" in notice and "Fresh research failed" in notice and "Another analysis" in notice, notice[:200])
    check("fallback: provenance line says saved on this device", "Saved on this device" in page.inner_text("#r-result .prov"))
    check("fallback: Retry is offered", page.locator("#r-result button:has-text('Retry fresh research')").count() == 1)
    page.screenshot(path=SHOTS + "/390_fallback_device.png")

    # fallback 2: nothing on the device -> KPIT operator answers, labelled not model output
    page.evaluate("localStorage.clear()")
    run_research(page, "KPITTECH", wait_running=False)
    page.wait_for_selector("#r-result .r-head", timeout=30000)
    notice = page.inner_text("#r-result .notice")
    prov = page.inner_text("#r-result .prov")
    check("fallback: operator answers say written by the builder, not model output",
          "not model output" in notice.lower() and "written by the builder" in notice and "not model output" in prov.lower(), notice[:240])
    check("fallback: operator answers are dated", "2026-10-31" in notice)
    opclaim = page.locator("#r-result .claim").first
    opclaim.locator("summary").first.click()
    oev = opclaim.locator(".evidence").inner_text()
    check("fallback: operator claim opens quote, page and original link",
          "PDF page" in oev and opclaim.locator("a[href^='https://www.bseindia.com']").count() == 1 and "by hand" in oev, oev[:300])
    check("fallback: sources section lists documents with links", page.locator("#sec-sources a[href^='https://www.bseindia.com']").count() >= 1)
    page.screenshot(path=SHOTS + "/390_fallback_operator.png", full_page=False)
    check("fallback: no overflow", no_overflow(page))

    # fallback 3: other company, nothing saved -> honest error and Retry (no KPIT answer shown for another company)
    page.evaluate("localStorage.clear()")
    run_research(page, "TCS", wait_running=False)
    page.wait_for_selector("#r-result .notice.bad", timeout=30000)
    err = page.inner_text("#r-result")
    check("error: no saved result for another company; honest error with Retry",
          "did not complete" in err and "No saved result exists" in err and page.locator("#r-result button:has-text('Retry')").count() == 1 and "KPIT" not in err, err[:200])
    page.screenshot(path=SHOTS + "/390_error_retry.png")
    page.unroute("**/api/analyze")
    context.close()

    # unknown ticker through the real (fake-network) path
    context, page = new_page(browser, 390, 844)
    open_today(page)
    page.click("#nav-research")
    run_research(page, "ZZZZNOTREAL", wait_running=False)
    page.wait_for_selector("#r-result .notice.bad", timeout=90000)
    check("error: unknown ticker gives an explained error, not a result", "did not complete" in page.inner_text("#r-result"), page.inner_text("#r-result")[:160])
    context.close()

    # ------------------------------------------------------------------ guidance versus outcome
    for width, height, mobile, tag in [(390, 844, True, "390"), (360, 740, True, "360"), (1280, 800, False, "1280")]:
        context, page = new_page(browser, width, height, mobile=mobile)
        open_today(page)
        page.click("#nav-research")
        page.wait_for_selector("#r-guidance .gcard")
        g = page.inner_text("#r-guidance")
        check(tag + " guidance: shows guided vs reported and 'Not directly comparable'", "Not directly comparable" in g and "22.5%" in g and "23" in g, g[:200])
        check(tag + " guidance: keeps the period and scope reasons", "Period differs" in g and "Scope" in g, g[:400])
        check(tag + " guidance: evidence collapsed by default", not page.locator("#r-guidance .ev-more .evidence").first.is_visible())
        page.locator("#r-guidance .ev-more > summary").first.click()
        page.wait_for_selector("#r-guidance .ev-more .evidence")
        ev = page.inner_text("#r-guidance .ev-more")
        check(tag + " guidance: evidence has quotes, page numbers and verbatim marks", "verbatim" in ev and "page 14" in ev and "page 6" in ev, ev[:200])
        check(tag + " guidance: no overflow", no_overflow(page))
        page.screenshot(path=SHOTS + "/" + tag + "_guidance.png", full_page=(width < 500))
        context.close()

    # ------------------------------------------------------------------ landing, demo sign-in, Profile
    SIZES = [(360, 740, True, "360"), (390, 844, True, "390"), (768, 1024, True, "768"), (1280, 800, False, "1280")]
    for width, height, mobile, tag in SIZES:
        context, page = new_page(browser, width, height, mobile=mobile)
        sent = []
        page.on("request", lambda r: sent.append((r.url, r.post_data or "")))
        page.route("**/api/filings/refresh", NO_REFRESH)
        page.request.post(BASE + "/__test/reset")
        page.goto(BASE)
        page.wait_for_selector("#signin-name")
        check(tag + " landing: shows landing, not the app", page.is_visible("#landing") and not page.is_visible("#shell"))
        text = page.inner_text("#landing")
        check(tag + " landing: says demo, no password, device only",
              "no password" in text.lower() and "only in this browser" in text.lower() and "not a secure login" in text.lower(), text[:100])
        check(tag + " landing: independent, not an ENAM product", "not an ENAM product" in text and "not affiliated" in text)
        check(tag + " landing: no password field and no ENAM logo", page.locator("input[type=password]").count() == 0 and
              page.locator("img[src*='enam']").count() == 0)
        check(tag + " landing: sign-in is above the fold on phones", page.evaluate("document.querySelector('#signin button').getBoundingClientRect().bottom <= window.innerHeight + 4") or width >= 640,
              page.evaluate("Math.round(document.querySelector('#signin button').getBoundingClientRect().bottom)"))
        check(tag + " landing: no horizontal overflow", no_overflow(page))
        check(tag + " landing: controls named", not unnamed_controls(page), unnamed_controls(page))
        check(tag + " landing: tap targets", not small_targets(page), small_targets(page))
        check(tag + " landing: focus ring visible on the name field", keyboard_focus_visible(page, "#signin-name") == "ok")
        page.screenshot(path=SHOTS + "/" + tag + "_landing.png", full_page=True)
        page.click("#signin button[type=submit]")
        check(tag + " sign-in: empty name is refused with a message", page.is_visible("#signin-error") and page.is_visible("#signin-name"))
        page.fill("#signin-name", "  Asha   Rao ")
        page.click("#signin button[type=submit]")
        page.wait_for_selector("#watchlist .row")
        check(tag + " sign-in: enters the app on Today", page.is_visible("#view-today") and not page.is_visible("#landing"))
        stored = page.evaluate("JSON.parse(localStorage.getItem('finn.profile'))")
        check(tag + " sign-in: name tidied and stored on this device", stored and stored["name"] == "Asha Rao" and "password" not in json.dumps(stored), stored)
        check(tag + " sign-in: Today greets by name", "asha rao" in page.inner_text("#today-kicker").lower(), page.inner_text("#today-kicker"))
        check(tag + " nav: Today, Portfolio, Research, Profile", page.eval_on_selector_all(".nav-item", "els => els.map(e => e.textContent.trim())") == ["Today", "Portfolio", "Research", "Profile"])
        leaked = [u for u, body in sent if "Asha" in u or "Asha" in body or "Rao" in u or "Rao" in body]
        check(tag + " privacy: the name is never sent to the server", not leaked, leaked)
        if width >= 900:
            check(tag + " nav: desktop rail shows the demo name", "Asha Rao" in page.inner_text("#nav-user") and "this device only" in page.inner_text("#nav-user"))
        page.screenshot(path=SHOTS + "/" + tag + "_today_signed_in.png")

        page.click("#nav-profile")
        page.wait_for_selector("#profile-body .p-name")
        prof = page.inner_text("#profile-body")
        check(tag + " profile: display name and demo status", "Asha Rao" in prof and "Demo profile" in prof and "Not a secure account" in prof, prof[:120])
        check(tag + " profile: no password, device-only storage stated", "None. FINN never asks" in prof and "only. Nothing is sent to FINN" in prof)
        page.wait_for_function("document.querySelector('#profile-body').innerText.includes('NSE-listed companies')")
        check(tag + " profile: portfolio count from the digest (16)", "16 NSE-listed companies" in page.inner_text("#profile-body"))
        check(tag + " profile: preferences listed", all(w in page.inner_text("#p-prefs") for w in ("Filing window", "Volume signal", "Price move", "Routine filings")))
        check(tag + " profile: no horizontal overflow", no_overflow(page))
        check(tag + " profile: controls named", not unnamed_controls(page), unnamed_controls(page))
        check(tag + " profile: tap targets", not small_targets(page), small_targets(page))
        check(tag + " profile: nav shows Profile as current", page.get_attribute("#nav-profile", "aria-current") == "page")
        page.screenshot(path=SHOTS + "/" + tag + "_profile.png", full_page=True)

        # edit filters from Profile: preference summary follows
        page.click("#profile-body button:has-text('Edit filters')")
        page.wait_for_selector("#sheet[open]")
        page.click("#sheet label:has-text('7 days')")
        page.click("#sheet-close")
        check(tag + " profile: preference summary follows the filter sheet", "7 days" in page.inner_text("#p-prefs"))

        # change name
        page.click("#p-change")
        page.fill("#p-name-input", "")
        page.click("#profile-body form button[type=submit]")
        check(tag + " profile: empty name is refused", page.is_visible("#p-name-error"))
        page.fill("#p-name-input", "Meera")
        page.click("#profile-body form button[type=submit]")
        check(tag + " profile: name changed everywhere", "Meera" in page.inner_text("#profile-body .p-name") and
              page.evaluate("JSON.parse(localStorage.getItem('finn.profile')).name") == "Meera" and "meera" in page.inner_text("#today-kicker").lower())
        page.reload()
        page.wait_for_selector("#watchlist .row")
        check(tag + " remembered: reload goes straight to the app with the saved name", not page.is_visible("#landing") and "meera" in page.inner_text("#today-kicker").lower())

        # sign out keeps filters unless asked
        page.click("#nav-profile")
        page.click("#profile-body .p-actions button:has-text('Sign out')")
        page.wait_for_selector("#sheet[open]")
        check(tag + " sign-out: sheet explains what is removed", "removes your display name" in page.inner_text("#sheet-body"))
        page.click("#sheet-body button:has-text('Sign out')")
        page.wait_for_selector("#signin-name")
        keys = page.evaluate("Object.keys(localStorage)")
        check(tag + " sign-out: back on landing, name removed, filters kept", page.is_visible("#landing") and "finn.profile" not in keys and "finn.filters" in keys, keys)
        check(tag + " sign-out: name field is empty again", page.input_value("#signin-name") == "")
        page.fill("#signin-name", "Dev")
        page.click("#signin button[type=submit]")
        page.wait_for_selector("#watchlist .row")
        page.click("#nav-profile")
        page.click("#profile-body button:has-text('Remove all demo data')")
        page.wait_for_selector("#sheet[open]")
        page.click("#sheet-body button:has-text('Remove all and sign out')")
        page.wait_for_selector("#signin-name")
        check(tag + " remove all: no FINN data left on this device", page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('finn.')).length") == 0)
        check(tag + " no console errors across landing/profile", not page.errors, page.errors)
        context.close()

    # storage blocked (private mode): sign-in still works and says the name will not be kept
    context, page = new_page(browser, 390, 844)
    page.add_init_script("Object.defineProperty(window, 'localStorage', {get() { throw new Error('blocked'); }})")
    page.route("**/api/filings/refresh", NO_REFRESH)
    page.goto(BASE)
    page.fill("#signin-name", "Temp")
    page.click("#signin button[type=submit]")
    page.wait_for_selector("#watchlist .row")
    page.click("#nav-profile")
    check("storage blocked: sign-in works and Profile says the name is not saved", "Not saved" in page.inner_text("#profile-body"))
    context.close()

    # tablet and desktop Today / Research / guidance layouts
    for width, height, mobile, tag in [(768, 1024, True, "768")]:
        context, page = new_page(browser, width, height, mobile=mobile)
        open_today(page)
        check(tag + " Today: no overflow", no_overflow(page))
        page.screenshot(path=SHOTS + "/" + tag + "_today.png", full_page=True)
        page.click("#nav-research")
        page.wait_for_selector("#r-guidance .gcard")
        check(tag + " Research: no overflow", no_overflow(page))
        page.screenshot(path=SHOTS + "/" + tag + "_research.png", full_page=True)
        context.close()

    browser.close()

failed = [r for r in results if not r[1]]
print("\n%d checks, %d failed" % (len(results), len(failed)))
for name, _, detail in failed:
    print("FAILED:", name, detail)
sys.exit(1 if failed else 0)
