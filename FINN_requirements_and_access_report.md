# FINN: requirements, access tests, trade-offs

Scope note: Section 2 is BROWSER-PANE results only. Results from running on the Mac itself are kept separately in MAC_RUN_RESULTS.md (pending until the run script is executed). Cloud-host access is NOT verified anywhere.

Date: 31 Oct 2026. Brief: ENAM AI Engineer Build Challenge. Deadline: Sun 1 Nov 2026.

## 1. Requirements

### Mandatory (stated as required or as a submission item)

| # | Requirement | Source in brief |
|---|---|---|
| M1 | Mobile-first working prototype, easy to try on a phone (link, Expo, or responsive web) | "What to submit" |
| M2 | GitHub repository | "What to submit" |
| M3 | AI build history (coding-agent / LLM conversations or share links) | "What to submit" |
| M4 | Covering-email writeup: what was built, key decisions and trade-offs, what was left out, next steps | "What to submit" |
| M5 | Email to assessment@enam.com, subject "ENAM - IIT screening - 2027" | "What to submit" |
| M6 | Mock portfolio of 15-20 NSE/BSE stocks | "Your companies" |
| M7 | 3-5 ad-hoc research companies, possibly outside the portfolio | "Your companies" |
| M8 | Research engine must work on any NSE ticker with data (not hard-wired to demo set) | "Build the engine to generalise" |
| M9 | All three sources working: EOD price/volume, NSE/BSE announcements, fundamentals (AR + concalls) | "The backend" |
| M10 | Pull-side analysis grounded only in annual reports and transcripts: no news, broker notes, or model outside knowledge | "One discipline for the pull side" |
| M11 | The product has two modes, push and pull | "What you'll build" |
| M12 | The demo must run. "A demo that doesn't run is the worst outcome" | "Scope, and how we grade" |

Reading of M11: the brief defines FINN as push plus pull, but also says one or two workflows done well beats breadth. My interpretation: ship both thinly (one digest, one research view) rather than skipping a mode. This is my judgment, not a stated rule.

### Optional (the brief calls these examples, a menu, or guidance)

- Windowed digest with user-chosen period, default 24h, prioritised and collapsed.
- Triggered alerts: material announcement types; volume above about 2x average; daily price move beyond ±X%; configurable thresholds.
- Rich push cards: management change with bio of the incoming person; results with Revenue, EBITDA, PBT, PAT QoQ and YoY.
- Any of the eight pull outputs (business, financial snapshot, trajectory, balance sheet, cash quality, narrative vs numbers, guidance, bull vs bear). The "promised vs delivered over four quarters" example is flagged as most valued.
- Interaction model (chat, voice, copilot, dashboard, other). Free choice; the brief rewards a point of view.
- The 17-category announcement taxonomy in the reference doc: "use it, adapt it, or ignore it".
- Efficiency bonus: model routing, caching, chunking.
- Push-side enrichment such as LinkedIn is allowed (the primary-source rule applies only to pull).

## 2. Access tests (run 31 Oct 2026, about 20:30-21:30 IST)

Three routes were tried. Only the third gave real data, so results are labelled by route.

| Route | Result |
|---|---|
| A. Cloud sandbox shell (yfinance 1.7.0, 7 NSE tickers) | **Blocked.** Every call failed with `CONNECT tunnel failed, response 403`; 0 rows. The sandbox allowlist blocks Yahoo, so this says nothing about Yahoo itself. |
| B. Sandbox web-fetch tool | Direct Yahoo, NSE API and most BSE PDF URLs were refused as not allowlisted. Some pages (screener.in, an NSE results PDF, NSE RSS) came back, but through a summarising model, so treat those as spot checks only. |
| C. Browser pane on your computer (real fetch calls, parsed in-page) | Results below. |

### 2.1 EOD price and volume: works (Yahoo chart JSON, route C)

Endpoint: `https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>?range=3mo&interval=1d`

| Symbol | HTTP | Rows (3mo) | Last bar | Close | Volume vs prior 20-day avg |
|---|---|---|---|---|---|
| RELIANCE.NS | 200 | 21 rows for 1mo | 2026-10-30 | 1164.90 | n/a (1mo query) |
| TCS.NS | 200 | 64 | 2026-10-30 | 2502.60 | 0.50x |
| HDFCBANK.NS | 200 | 64 | 2026-10-30 | 692.80 | 0.67x |
| INFY.NS | 200 | 64 | 2026-10-30 | 1185.00 | 0.77x |
| ITC.NS | 200 | 64 | 2026-10-30 | 265.60 | 0.70x |
| PIDILITIND.NS | 200 | 64 | 2026-10-30 | 1461.60 | 1.02x |
| **KPITTECH.NS** (outside set) | 200 | 64 | 2026-10-30 | 530.25 | 0.46x |
| RELIANCE.BO | 200 | **1 only** | 2026-10-30 | 1163.55 | cannot compute |
| NOSUCHTICKERXYZ.NS | **404** | none | none | none | clean "No data found" error |

- Volume spike and ±X% rules can be computed from this alone.
- `.BO` returned a single row for a 3-month range. Prefer `.NS`; do not rely on `.BO` history.
- Unofficial endpoint, no SLA. Not tested from a server IP (see 2.5).

### 2.2 Announcements: both exchanges work (route C)

**NSE** (`/api/corporate-announcements?index=equities&symbol=<SYM>&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`), called after loading NSE's announcements page in the same browser session:
- RELIANCE returned 3,379 records and KPITTECH 721 for the unwindowed call (2.9 MB for RELIANCE). Always pass a date window.
- Windowed KPITTECH, 24-31 Oct: 11 records.
- Market-wide, 30-31 Oct: 1,387 records. Top categories were investor-meet updates (261), newspaper copies (207), board-meeting outcomes (197), general updates (129), press releases (85). This is the noise the digest has to bury.
- Fields include `desc` (NSE's own category), `attchmntText` (one-line description), `attchmntFile` (PDF link on nsearchives.nseindia.com), `an_dt`, `hasXbrl`.
- Newest records were from 31 Oct 2026 (same-day).
- NSE's `desc` values do not match the reference doc's 17 categories, so a mapping layer is needed.

**BSE** (`api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?...strScrip=<scrip code>&strPrevDate=YYYYMMDD&strToDate=YYYYMMDD...`), called from a bseindia.com page:
- KPIT (542651): 11 rows; RELIANCE (500325): 10 rows for 24-31 Oct.
- Fields include `CATEGORYNAME`, `SUBCATNAME`, `HEADLINE`, `ATTACHMENTNAME` (UUID.pdf, openable as `bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=<UUID>.pdf`).
- Needs the numeric BSE scrip code, not the NSE symbol. I took the two codes from screener.in URLs; there is no tested symbol-to-code mapping.
- Cross-check: KPIT's director cessation filing appears on both (BSE 18:19, NSE 18:25 IST, 31 Oct). Reliance's results-day PDFs on BSE match the UUIDs listed on screener.in.

### 2.3 Annual reports and transcripts: works (route C)

screener.in company page, logged out, `consolidated` view:
- KPITTECH: Documents section parsed into Announcements (6), Annual reports (15), Credit ratings (6), Concalls (65 links, mixing transcripts, PPTs and audio recordings).
- Statement tables parse by id: quarters, profit-loss, balance-sheet, cash-flow, ratios. Row labels include Sales, Operating Profit, PBT, Net Profit, Borrowings, CFO, Free Cash Flow, ROCE.
- Document files fetched in-page, all HTTP 200, `application/pdf`, `%PDF-1.7` magic bytes:

| File | Size |
|---|---|
| RELIANCE annual report 2025 | 8.9 MB |
| RELIANCE concall (Jul 2026) | 0.6 MB |
| KPITTECH annual report 2026 | 10.3 MB |
| KPITTECH concall (Aug 2026) | 0.5 MB |

- Via the summarising fetch tool (spot check only): some older concall entries have no transcript link, and the newest call (Oct 2026) listed audio only, so transcripts lag results by days. Some transcripts are hosted on company sites rather than BSE.
- Numeric cross-check: KPIT Sep-2025 quarter, from the NSE results PDF (via the summarising tool): revenue ₹15,877.11 mn, PBT ₹2,305.65 mn, profit ₹1,690.87 mn. screener shows Sales 1,588, PBT 231, Net Profit 169 (₹ Cr). These agree.
- Screener's "Operating Profit" is its own derived line. A results PDF has no EBITDA line, so EBITDA needs a stated definition.

### 2.4 Tested vs not tested

Tested and working: Yahoo chart JSON; NSE announcements API; BSE announcements API; screener page structure; BSE-hosted PDFs reachable.

**Not tested**:
- Any request from a server or deployed host. All real data came through a browser with normal cookies and headers. NSE and BSE often treat cloud IPs and bare HTTP clients differently; this could break the deployed demo.
- Whether NSE works without first loading its page for cookies (I loaded the page first).
- Rate limits, and how long these unofficial endpoints stay stable.
- Text extraction quality from annual-report and transcript PDFs (I confirmed they are valid PDFs, not that the text parses cleanly). The one results PDF read through the summariser had minor character errors.
- LinkedIn enrichment, any LLM step, and mobile rendering.

## 3. Smallest reliable architecture

1. **Ingest job (not request-time).** Python script on a schedule: pulls Yahoo EOD, NSE announcements by date window, screener page per ticker. Writes to SQLite or JSON files in the repo.
2. **Seed snapshot committed to the repo.** The demo reads cached data, so a blocked live endpoint cannot kill it (M12). Live refresh is an extra, not a dependency.
3. **Rules first, LLM second.** Map NSE `desc` to your categories with simple rules. Use a cheap model only for one-line summaries of results and management-change items; a stronger model only for narrative-vs-numbers. This routing is a design, not something I measured.
4. **Numbers from screener tables, text from PDFs.** Trajectory, balance sheet and cash quality come straight from parsed tables. Guidance and promised-vs-delivered need transcript text. Cache extracted text by URL hash.
5. **Thin API plus responsive web app (PWA-style).** The simplest way to satisfy M1 on any phone.
6. **Generalisation (M8).** Everything keys on the NSE symbol; screener URL is `/company/<SYMBOL>/consolidated/`; BSE scrip code is looked up from the screener page when needed. I tested one ticker outside the set (KPITTECH); that is a sample of one.

## 4. Trade-offs

| Decision | Option A | Option B | Lean |
|---|---|---|---|
| Live vs cached data | Live scrape at request time | Scheduled ingest plus committed snapshot | B (demo must run) |
| Primary announcement source | NSE only | NSE plus BSE | NSE primary; BSE for gaps and dedupe |
| Price client | `yfinance` library | Direct chart endpoint with `requests` | Try both on the real host; endpoint is simpler and was verified here |
| Statement numbers | Parse PDFs with an LLM | Read screener tables | Screener tables (cheaper, checkable) |
| Classification | LLM on every filing | Rules, LLM on a few types | Rules first |
| Interaction | Chat | Digest feed with ask-anywhere | Open; decide after data layer works |
| Delivery | Expo app | Responsive web | Web (faster to ship and share) |

## 5. Next step (one thing)

Run `probe_access.py` (below) from the machine or host that will run the ingest job. If NSE or BSE fail there, switch to a locally scheduled snapshot before building any UI.

## Appendix: probe script (written, not run)

See `probe_access.py` in the same folder.
