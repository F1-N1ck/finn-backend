"""Phone-viewport UI test with Playwright against tests/ui_server.py (fake model, fake live prices).
Run:  python tests/ui_server.py &   then   python tests/ui_phone_check.py"""

import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"
SHOTS = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-0/shots"
results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


def no_overflow(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def small_targets(page):
    script = """
    () => {
      const bad = [];
      const nodes = document.querySelectorAll('button, a.link, summary');
      for (const n of nodes) {
        const r = n.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const style = getComputedStyle(n);
        if (style.visibility === 'hidden') continue;
        if (r.height < 40 || r.width < 40) bad.push((n.id || n.className || n.tagName) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
      }
      return bad;
    }
    """
    return page.evaluate(script)


with sync_playwright() as p:
    browser = p.chromium.launch()
    for width, height, scheme in [(390, 844, "light"), (360, 740, "dark")]:
        context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2,
                                      is_mobile=True, has_touch=True, color_scheme=scheme)
        page = context.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "422" not in m.text and "404" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        tag = str(width) + scheme
        page.goto(BASE)
        page.wait_for_selector("#portfolio .stock")
        check(tag + " status says Snapshot, not live", "Snapshot, not live" in page.inner_text("#status"), page.inner_text("#status")[:90])
        check(tag + " 16 stocks", page.locator("#portfolio .stock").count() == 16)
        check(tag + " no horizontal overflow (digest)", no_overflow(page))
        page.screenshot(path=SHOTS + "/" + tag + "_digest.png")

        alerts = page.inner_text("#alerts")
        check(tag + " volume alert SUNPHARMA 3.4x", "SUNPHARMA" in alerts and "3.4×" in alerts)
        check(tag + " move alert INFY +3.36%", "INFY" in alerts and "+3.36%" in alerts)
        check(tag + " high filing alert in 24h (HDFCBANK)", "HDFCBANK" in alerts)

        before = int(page.inner_text("#filing-count").split()[0])
        page.click("#window-seg button[data-hours='168']")
        after = int(page.inner_text("#filing-count").split()[0])
        check(tag + " 7d window shows more filings than 24h", after > before, str(before) + " -> " + str(after))
        check(tag + " 7d window = 80 filings minus duplicates", after == 80, str(after))
        page.click("#window-seg button[data-hours='24']")

        page.click("#vol-plus")
        page.click("#vol-plus")
        page.click("#vol-plus")
        text_35 = page.inner_text("#alerts")
        check(tag + " at 3.5x SUNPHARMA volume alert disappears", "3.4×" not in text_35, page.inner_text("#vol-out"))
        for _ in range(3):
            page.click("#vol-minus")

        page.click("#portfolio .stock[data-symbol='TCS']")
        check(tag + " TCS sheet explains empty window", "45 filings since 1 Aug 2026" in page.inner_text("#sheet-content"))
        check(tag + " sheet no overflow", no_overflow(page))
        page.screenshot(path=SHOTS + "/" + tag + "_sheet_tcs.png")
        page.click("#sheet-close")
        check(tag + " sheet closes", page.locator("#sheet").is_hidden())

        page.click("#portfolio .stock[data-symbol='KPITTECH']")
        check(tag + " KPIT sheet offers research", page.locator("#sheet-research").count() == 1)
        page.click("#sheet-research")
        check(tag + " research tab opened from sheet", page.locator("#tab-research").is_visible())

        page.wait_for_selector(".qbtn")
        check(tag + " 2 questions listed", page.locator(".qbtn").count() == 2)
        check(tag + " run disabled before choosing", page.locator("#run-btn").is_disabled())
        page.click(".qbtn[data-id='Q1_concall_guidance']")
        page.click("#run-btn")
        page.wait_for_selector(".banner")
        banner = page.inner_text(".banner")
        check(tag + " cached answer labelled operator-written, not model", "operator-written" in banner and "NOT model output" in banner, banner[:100])
        claims = page.locator(".claim").count()
        check(tag + " cached answer shows claims", claims >= 3, str(claims))
        page.locator(".claim > button").first.click()
        check(tag + " claim expands to highlighted source quote", page.locator(".claim .passage:visible mark").count() == 1)
        page.screenshot(path=SHOTS + "/" + tag + "_research_cached.png", full_page=True)
        check(tag + " no overflow (research)", no_overflow(page))

        page.click("#mode-seg button[data-mode='live']")
        check(tag + " run button says live", "live" in page.inner_text("#run-btn").lower())
        page.click("#run-btn")
        page.wait_for_selector(".banner.live")
        check(tag + " live answer labelled LIVE", "LIVE analysis" in page.inner_text(".banner.live"))
        page.locator(".claim > button").first.click()
        check(tag + " live claim highlights quote", page.locator(".claim .passage:visible mark").count() == 1)
        check(tag + " auto-check tick shown", "✓" in page.inner_text("#result"))
        page.screenshot(path=SHOTS + "/" + tag + "_research_live.png", full_page=True)

        # ---- generic research: any ticker
        page.fill("#ticker-input", "A B")
        page.click("#analyze-btn")
        page.wait_for_selector("#analyze-progress .error")
        check(tag + " invalid ticker explained", "not a valid NSE ticker" in page.inner_text("#analyze-progress"))
        page.fill("#ticker-input", "zzzz")
        page.click("#analyze-btn")
        page.wait_for_selector("#analyze-progress .error", timeout=20000)
        page.wait_for_function("document.querySelector('#analyze-progress .error') && document.querySelector('#analyze-progress .error').innerText.indexOf('No company page') >= 0", timeout=20000)
        check(tag + " unknown ticker explained", "No company page" in page.inner_text("#analyze-progress"))
        page.fill("#ticker-input", "kpittech")
        page.click("#analyze-btn")
        page.wait_for_selector("#analyze-progress .progress li", timeout=10000)
        check(tag + " progress list shown while running", page.locator("#analyze-progress .progress li").count() >= 1)
        check(tag + " button disabled while running", page.locator("#analyze-btn").is_disabled())
        page.wait_for_selector("#brief .brief-section", timeout=60000)
        brief_text = page.inner_text("#brief")
        first_view = (width == 390)  # the server cache persists, so the second viewport legitimately gets a cached brief
        check(tag + " brief labelled " + ("LIVE" if first_view else "CACHED"), ("LIVE brief" if first_view else "CACHED brief") in brief_text)
        check(tag + " company resolved", "KPIT Technologies Ltd" in brief_text and "KPITTECH" in brief_text)
        marker = "fetched now" if first_view else "cached with brief"
        check(tag + " both documents shown with fetch state", brief_text.count(marker) == 2, str(brief_text.count(marker)))
        check(tag + " three sections", page.locator("#brief .brief-section").count() == 3)
        check(tag + " number shows value, unit, scope and period", "64,549.31" in brief_text and "consolidated" in brief_text and "Year ended 31 March 2026" in brief_text, brief_text[-900:] if "64,549.31" not in brief_text else "")
        page.locator("#brief .claim > button").first.click()
        check(tag + " citation opens with page, date and link", page.locator("#brief .cite:visible").count() == 1 and "PDF page" in page.inner_text("#brief .cite:visible") and page.locator("#brief .cite:visible a").count() == 1)
        check(tag + " citation link is https and noopener", page.locator("#brief .cite:visible a").first.get_attribute("href").startswith("https://") and "noopener" in page.locator("#brief .cite:visible a").first.get_attribute("rel"))
        check(tag + " no overflow (brief)", no_overflow(page))
        page.screenshot(path=SHOTS + "/" + tag + "_brief.png", full_page=True)
        page.fill("#ticker-input", "kpittech")
        page.click("#analyze-btn")
        page.wait_for_function("document.querySelector('#brief .banner') && document.querySelector('#brief .banner').innerText.indexOf('CACHED') >= 0", timeout=30000)
        check(tag + " second run labelled CACHED brief", "CACHED brief" in page.inner_text("#brief"))
        check(tag + " cached documents not called fresh", "fetched now" not in page.inner_text("#brief"))
        check(tag + " cached documents say when they were really fetched", "fetched 20" in page.inner_text("#brief"))

        page.click("#btn-digest")
        page.click("#refresh-btn")
        page.wait_for_selector("#status.live")
        check(tag + " live refresh switches status to LIVE", "LIVE prices" in page.inner_text("#status"))
        check(tag + " live refresh note", "16 of 16" in page.inner_text("#refresh-note"), page.inner_text("#refresh-note"))
        page.screenshot(path=SHOTS + "/" + tag + "_digest_live.png")

        bad = small_targets(page)
        check(tag + " tap targets >= 40px", len(bad) == 0, ", ".join(bad[:6]))
        check(tag + " no console errors", len(errors) == 0, "; ".join(errors[:3]))
        context.close()
    browser.close()

failed = [r for r in results if not r[1]]
print("\n%d checks, %d failed" % (len(results), len(failed)))
sys.exit(1 if failed else 0)
