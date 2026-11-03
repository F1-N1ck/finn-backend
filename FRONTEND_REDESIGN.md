# Front-end redesign (3 Nov 2026)

Mobile-first investor briefing. Backend behaviour, research checks, data sources and protections are unchanged.
Only additions on the server: `source_url` / `short` / `doc` on `/api/questions`, `/api/research` and the
`snapshot_fallback` answers (read from the snapshot manifest, so saved answers can link to their original PDF).

## Which backend behaviour supports each interaction

| Interaction | Backend it relies on |
|---|---|
| Priority filings, other filings, dedupe, window note | `GET /api/digest` (`filings`, `tier`, `as_of_utc`). Dedupe and the filing window are client-side. |
| Filings freshness (live / partial / refresh failed / snapshot) | `filings_status` in the digest, `POST /api/filings/refresh`, `GET /api/filings/status` |
| Prices freshness, "end-of-day, not a live feed" | `meta.prices` in the digest; `POST /api/prices/refresh` (Yahoo daily bars; `partial` marks an intraday bar) |
| Market signals, filter sheet | Client-side thresholds over `portfolio[].volume_ratio` / `change_pct` (or live refresh rows) |
| "Research company" with the filing kept as context | The filing object from the digest. `/api/analyze` takes only a ticker; the filing PDF is never analysed. |
| Research run, steps, results, claim evidence | `POST /api/analyze`, `GET /api/analyze/{id}`: brief citations (`excerpt`, `context`, `doc_date`, `pdf_page`, `speaker`, `url`) and passages |
| Fresh attempt first, then same-company fallback | 1. fresh `/api/analyze`; 2. brief saved on this device (localStorage, model output, dated); 3. server `snapshot_fallback` or `/api/questions` + `/api/research` (KPITTECH only, operator-written, labelled so); 4. honest error + Retry |
| Guidance vs outcome | `GET /api/guidance` (comparison gate in `finn/guidance.py`; nothing recomputed in the browser) |

## Rules the UI keeps
- Never shows a saved answer for a different company than the one asked for.
- Operator-written answers are labelled "written by the builder, not model output" with their date.
- "Research company" is the only label; the page says the filing itself has not been analysed.
- Quote checks say "word for word, does not check meaning". Nothing is marked fully verified.
- Colour is never the only status signal: every state has a glyph (● ○ ◆ ▲ ▼ ✓ ✕ !) and words.

## Tests
`python3 tests/ui_server.py &` then `python3 tests/ui_redesign_check.py [shots_dir]` (restart the server between runs).
Fake model, fake prices and fake NSE: states exercised are loading, empty, failed digest, partial refresh, stale, price failure,
saved-result fallbacks, error + Retry, claim evidence, guidance, focus ring, labels, tap targets, overflow at 360 / 390 / 1280.

## Round 2 (3 Nov, evening): blue/white restyle, landing page, demo sign-in, Profile

- Visual reference: enam.com inspected in the browser pane (white canvas, one strong blue, Helvetica-style sans, hairline greys,
  square corners, black-and-white skyline). FINN uses its own blue (#0B5FA5), its own wordmark (rising-line glyph + "FINN") and its own
  grey skyline artwork (`app/static/hero.svg`, drawn for FINN). No ENAM logo, name or imagery is used as FINN's identity; the landing page
  and footer say FINN is an independent prototype, not an ENAM product.
- Demo sign-in: display name only (`finn.profile` in this browser's localStorage). No password field, no account, never sent to the server
  (the UI test records every request and checks the name is in none of them). Sign out keeps filters/research unless the box is ticked;
  "Remove all demo data" deletes every `finn.*` key. If the browser blocks storage the Profile says the name is not saved.
- This is not access control: the data behind the app is public demo data and every endpoint stays open as before.
- Profile tab: name, demo status, portfolio count (from `/api/digest`, 16), Today-filter preferences, saved research on this device.
- Tests: `tests/ui_redesign_check.py` now 256 checks (adds landing, sign-in, privacy, profile, sign-out, blocked-storage; sizes 360/390/768/1280).

## Round 3 (3 Nov, night): Portfolio view, per-user selections

**What the user sees.** A fourth tab, Portfolio: search by company name or type an exact NSE ticker, add, remove (with Undo), Remove all,
Reset to demo, a per-company coverage readout, and clear empty / failed states. Today, Research suggestions and Profile all follow the selection.

**Where the selection lives.** Only in this browser's localStorage, under `finn.portfolio.<display name, lower-case>`
(`{v:1, symbols:[{symbol,name}], updated}`). Nothing is saved until the user changes the demo list; Reset to demo deletes the key.
Different display names on one browser have separate selections; renaming moves the selection; sign-out keeps it; "also remove my data"
deletes only that name's; "Remove all demo data" deletes every name's. **It does not sync across devices or browsers**, and the
server never stores it (clearing site data, or opening FINN elsewhere, starts again from the 16-company demo list).

**Coverage words (per company, per source).** Available, Loading, Unavailable, Refresh failed.
Filings: live / valid_empty / snapshot = available; ambiguous_empty = unavailable (cannot be read as "no filings");
stale_live / failed_snapshot / failed = refresh failed; unchecked = unavailable ("Not checked yet"), or loading while a check is running.
Prices: saved or live close = available; Yahoo has no history = unavailable; fetch error = refresh failed (the earlier close, if any, stays labelled).
A company that has never been checked is never shown as having "no filings": feeds say the filings are "unknown, not absent".

**Backend changes (no change to claim checks, research, or calculations).**
- `GET /api/portfolio/demo`: the 16 demo tickers and names (read-only starting list).
- `POST /api/digest {symbols}`: digest for that list only (GET /api/digest still returns the demo portfolio). Each entry carries `in_snapshot`, `price_source`, `filings_state`, `refreshing`.
- `POST /api/filings/refresh {symbols}`, `GET /api/filings/status?symbols=`, `POST /api/prices/refresh {symbols}`: per-symbol work, per-symbol cooldown/cache (120 s), a global limit on live calls. Empty list = 422. The old 20 s global price throttle is replaced by the per-symbol cache.
- `GET /api/symbols/check?symbol=` (Yahoo `.NS` chart: valid / not_found / check_failed) and `GET /api/symbols/search?q=` (Yahoo search, NSE equities only; 503 `search_unavailable` when blocked).
- Validation: tickers must match `[A-Z0-9&-]{1,20}`, at most 40 per request; extra fields are rejected (422), so the endpoints cannot be used to fetch arbitrary URLs. Hosts are fixed in code.
- Per-company filings state `unchecked` / `failed` added; the digest never reuses another selection's run summary.

**Tests.** `python3 -m pytest -q` (340). UI: `python3 tests/ui_server.py &` then `python3 tests/ui_redesign_check.py` (256) and
`python3 tests/ui_portfolio_check.py` (130: add, duplicate, invalid, unconfirmable ticker, search source down, remove + undo, reload restore,
reset, empty, unchecked vs "no filings", failed NSE, failed prices, separate users, rename, no sync, 40-company limit, damaged and blocked storage, 360/390/768/1280).

## Round 4 (3 Nov, night): investor-on-a-phone review

Walked sign-in → portfolio edit → Today → company → research brief → evidence → Profile at 390 / 360 / 768 / 1280 and fixed what broke.

| Found | Fix |
|---|---|
| Opening claim evidence made the page wider than a phone screen (a stretched, rotated chevron), shrinking the view and pushing the bottom nav off-screen and untappable. The old overflow test missed it because it compared against the already-widened viewport. | `.claim .chev { align-self: start }`; overflow checks now also fail if the layout viewport widened; new test opens every claim and taps the nav. |
| A "filing you came from" panel stayed under research for a different company. | Dropped when the researched ticker differs. |
| A saved brief was shown if it sat under the ticker's key, without checking it was for that company. | Shown only if the brief's own ticker matches; tampered or moved values are ignored. |
| Saved copies showed a date but not their age; the "Today" window used a 31 Oct date without saying it was 3 days old. | "(3 days old)" on filings, prices, the filing window and per-company coverage. |
| Filing summaries cut by the feed ended mid-word. | Says "(summary cut short; open the filing for the full text)". |
| "Watchlist" / "16-company watchlist" copy after the portfolio became editable; placeholder example was already a demo company. | Reworded to portfolio. |
| The Hindustan Unilever guidance example read as part of the researched company's result. | Titled "Worked example", says it is a different company and not part of any research result. |
| An annual report's "Dated" field showed the report's name. | "Annual Report 2026 (the report's year; no filing date is shown)". |
| Silent skeleton on a cold free-tier server. | After 7 s: "Still loading. The free server may be waking up…". |
| Tab changes did not move focus. | Focus moves to the page title. |

Tests: `tests/ui_review_check.py` (35 checks). Run order: restart `tests/ui_server.py` before each UI suite.
