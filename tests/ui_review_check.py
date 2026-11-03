"""Investor-on-a-phone review checks (Playwright) against tests/ui_server.py. Restart the server between runs.
Covers: the whole journey on a phone, the claim-chevron overflow regression, stale-data wording, filing context that belongs to another
company, cut-off filing text, the saved-research fallback (same company only, dated, tamper-proof), cold-start wording, focus on tab change."""

import json
import os
import sys
import time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"
SHOTS = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-0/shots"
os.makedirs(SHOTS, exist_ok=True)
results = []
NO_REFRESH = lambda route: route.fulfill(status=202, content_type="application/json",
                                         body='{"running":false,"progress":null,"started":false,"reason":"blocked_by_test"}')
FAIL_ANALYZE = lambda route: route.fulfill(status=503, content_type="application/json", body='{"detail":{"code":"busy"}}')


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + ((" | " + str(detail)[:160]) if detail else ""))


def no_overflow(page):
    width = page.viewport_size["width"]
    return page.evaluate("(w) => document.documentElement.scrollWidth <= window.innerWidth + 1 && window.innerWidth <= w + 1", width)


def new_page(browser, width=390, height=844, mobile=True, name="Asha"):
    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2 if mobile else 1,
                                  is_mobile=mobile, has_touch=mobile)
    page = context.new_page()
    page.errors = []
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" and not any(
        x in m.text for x in ("422", "404", "500", "503", "ERR_FAILED", "429")) else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    if name:
        page.add_init_script("if (!localStorage.getItem('finn.profile')) localStorage.setItem('finn.profile', JSON.stringify({name: %s, created: new Date().toISOString(), v: 1}));" % json.dumps(name))
    return context, page


def open_app(page, refresh_blocked=True):
    page.request.post(BASE + "/__test/reset")
    if refresh_blocked:
        page.route("**/api/filings/refresh", NO_REFRESH)
    page.goto(BASE)
    page.wait_for_selector("#watchlist .row")


def research(page, ticker):
    page.fill("#r-input", ticker)
    page.click("#r-go")


with sync_playwright() as p:
    browser = p.chromium.launch()

    # ------------------------------------------------------------------ copy and labels
    context, page = new_page(browser, name=None)
    page.goto(BASE)
    body = page.inner_text("#landing")
    check("landing: no stale '16-company watchlist' wording now that the portfolio is editable", "16-company watchlist" not in body and "your own portfolio" in body)
    check("landing: demo, no password, device-only are stated", "no password" in body.lower() and "only in this browser" in body.lower() and "not a secure login" in body.lower())
    page.fill("#signin-name", "Asha")
    page.click("#signin button[type=submit]")
    page.wait_for_selector("#watchlist .row")
    check("today: the price list is called 'Your portfolio', not 'Watchlist'", "Your portfolio" in page.inner_text("#blk-watch h2") and "Watchlist" not in page.inner_text("#view-today"))
    check("today: focus moves to the page title when the tab changes (screen readers)", True)
    page.click("#nav-portfolio")
    check("a11y: switching tab puts focus on the new page title", page.evaluate("document.activeElement.tagName + ':' + document.activeElement.textContent") == "H1:Portfolio")
    check("portfolio: placeholder example is not a company already in the demo list", "Bajaj Finance" in page.get_attribute("#pf-input", "placeholder"))
    context.close()

    # ------------------------------------------------------------------ stale-data wording and cut-off text
    context, page = new_page(browser)
    open_app(page)
    fresh = page.inner_text("#freshness")
    check("stale: saved filings and prices say how old they are", fresh.count(" old") >= 2 and "not live" in fresh, fresh[:240])
    check("stale: the filing window says when the last update was and how old it is", "old)" in page.inner_text("#window-note"), page.inner_text("#window-note"))
    texts = page.eval_on_selector_all(".f-text", "els => els.map(e => e.textContent)")
    cut = [t for t in texts if "summary cut short" in t]
    mid_word = [t for t in texts if len(t) >= 215 and "summary cut short" not in t and not t.rstrip().endswith((".", "!", "?"))]
    check("filings: text that the feed cut short says so instead of ending mid-word", len(cut) >= 1 and not mid_word, (len(cut), mid_word[:1]))
    context.close()

    # ------------------------------------------------------------------ the phone journey, with the chevron overflow regression
    for width, height, tag in [(390, 844, "390"), (360, 740, "360")]:
        context, page = new_page(browser, width, height)
        open_app(page)
        first = page.locator("#priority .filing button:has-text('Research company')").first
        first_sym = page.locator("#priority .filing .sym").first.inner_text()
        first.click()
        page.wait_for_selector("#view-research:not([hidden])")
        research(page, "KPITTECH")
        check(tag + " research: a filing context from another company is dropped when you research a different one (" + first_sym + " -> KPITTECH)",
              first_sym == "KPITTECH" or page.locator("#r-context .ctx").count() == 0, first_sym)
        page.wait_for_selector("#r-result .r-head", timeout=120000)
        for i in range(page.locator("#r-result .claim > summary").count()):
            page.locator("#r-result .claim > summary").nth(i).click()
        more = page.locator("#r-result .ev-more > summary")
        for i in range(more.count()):
            more.nth(i).click()
        check(tag + " journey: opening every claim and every 'surrounding text' does not widen the page", no_overflow(page))
        wide = page.evaluate("""() => [...document.querySelectorAll('#r-result *')].filter(n => !n.closest('.jump') && n.getBoundingClientRect().right > window.innerWidth + 1).map(n => n.className).slice(0, 5)""")
        check(tag + " journey: no result element pokes past the screen edge", not wide, wide)
        page.locator("#nav-profile").click(timeout=5000)
        check(tag + " journey: bottom nav is still tappable with all evidence open", page.is_visible("#view-profile"))
        page.wait_for_selector("#profile-body .p-name")
        check(tag + " profile: no overflow", no_overflow(page))
        page.screenshot(path=SHOTS + "/review_" + tag + "_profile.png", full_page=True)
        context.close()

    context, page = new_page(browser, 390, 844)
    open_app(page)
    page.click("#nav-research")
    research(page, "KPITTECH")
    page.wait_for_selector("#r-result .r-head", timeout=120000)
    page.locator("#r-result .claim > summary").first.click()
    ev = page.locator("#r-result .claim").first.locator(".evidence").inner_text()
    check("evidence: an annual report with no filing date says so instead of calling the report's year a date", "no filing date is shown" in ev, ev[:300])
    g = page.inner_text("#r-guidance")
    check("guidance: labelled a worked example on a different company, not part of the research above",
          "Worked example" in g and "different company" in g and "not part of any research result" in g, g[:200])
    check("research: owns up that missing or unreadable parts exist (Sources say how many pages were read)", "pages" in page.inner_text("#sec-sources"))
    context.close()

    # ------------------------------------------------------------------ saved research is a dated fallback for the SAME company only
    context, page = new_page(browser, 390, 844)
    open_app(page)
    page.click("#nav-research")
    research(page, "KPITTECH")
    page.wait_for_selector("#r-result .r-head", timeout=120000)
    page.route("**/api/analyze", FAIL_ANALYZE)
    research(page, "KPITTECH")
    page.wait_for_selector("#r-result .notice", timeout=30000)
    note = page.inner_text("#r-result .notice")
    prov = page.inner_text("#r-result .prov")
    check("fallback: same company, labelled 'saved result, not fresh research' with the failure reason", "saved result, not fresh research" in note and "Fresh research failed" in note, note[:200])
    check("fallback: the saved result carries its own generation date", "Saved on this device" in prov and "model-generated" in prov and "Nov" in prov, prov)
    check("fallback: shown for KPITTECH only", "KPIT" in page.inner_text("#r-result"))
    # a different company must never get it, even though KPIT's brief is on this device
    for other in ("TCS", "ETERNAL", "KPIT"):
        research(page, other)
        page.wait_for_selector("#r-result .notice.bad", timeout=30000)
        txt = page.inner_text("#r-result")
        check("fallback: " + other + " gets an honest error, never KPIT's saved brief", "did not complete" in txt and "KPIT Technologies" not in txt and "saved result, not fresh research" not in txt.lower(), txt[:160])
    # tampered storage: KPIT's brief stored under TCS's key
    page.evaluate("localStorage.setItem('finn.brief.TCS', localStorage.getItem('finn.brief.KPITTECH'))")
    research(page, "TCS")
    page.wait_for_selector("#r-result .notice.bad", timeout=30000)
    txt = page.inner_text("#r-result")
    check("fallback: a brief for another company stored under this ticker is ignored", "KPIT Technologies" not in txt and "did not complete" in txt, txt[:160])
    # a company added to the portfolio but never researched has nothing to fall back on
    open_portfolio = page.evaluate("1")
    page.click("#nav-portfolio")
    page.fill("#pf-input", "ETERNAL")
    page.click("#pf-add")
    page.wait_for_selector('#pf-list .pf-row[data-symbol="ETERNAL"]')
    page.click('#pf-list .pf-row[data-symbol="ETERNAL"] button:has-text("Research")')
    page.wait_for_selector("#view-research:not([hidden])")
    research(page, "ETERNAL")
    page.wait_for_selector("#r-result .notice.bad", timeout=30000)
    txt = page.inner_text("#r-result")
    check("fallback: a portfolio-added company with no saved research shows the error and Retry, no borrowed answer",
          "No saved result exists" in txt and page.locator("#r-result button:has-text('Retry')").count() == 1 and "KPIT" not in txt, txt[:200])
    page.unroute("**/api/analyze")
    check("fallback: no console errors", not page.errors, page.errors)
    context.close()

    # KPIT operator-written answers are labelled, dated and are KPIT only
    context, page = new_page(browser, 390, 844)
    open_app(page)
    page.click("#nav-research")
    page.route("**/api/analyze", FAIL_ANALYZE)
    research(page, "KPITTECH")
    page.wait_for_selector("#r-result .notice", timeout=30000)
    note = page.inner_text("#r-result .notice")
    check("operator fallback: says written by the builder, not model output, with a date", "not model output" in note.lower() and "2026-10-31" in note, note[:220])
    research(page, "TCS")
    page.wait_for_selector("#r-result .notice.bad", timeout=30000)
    check("operator fallback: not offered for another company", "not model output" not in page.inner_text("#r-result").lower())
    context.close()

    # ------------------------------------------------------------------ slow first load says why
    context, page = new_page(browser, 390, 844)
    page.request.post(BASE + "/__test/reset")
    page.route("**/api/filings/refresh", NO_REFRESH)

    def slow(route):
        time.sleep(9)
        route.continue_()
    page.route("**/api/digest", slow)
    page.goto(BASE)
    page.wait_for_function("document.querySelector('#freshness .hint') && document.querySelector('#freshness .hint').innerText.includes('waking up')", timeout=20000)
    check("loading: after a few seconds the page says the free server may be waking up", True)
    page.screenshot(path=SHOTS + "/review_390_slow_load.png")
    page.wait_for_selector("#watchlist .row", timeout=30000)
    check("loading: the data still arrives and the notice goes away", "waking up" not in page.inner_text("#freshness"))
    context.close()

    browser.close()

failed = [r for r in results if not r[1]]
print("\n%d checks, %d failed" % (len(results), len(failed)))
for name, _, detail in failed:
    print("FAILED:", name, detail)
sys.exit(1 if failed else 0)
