# Generic research flow: status

**Claim status: NOT yet shown to generalise.** Everything below was tested offline (fake network, fake model,
real KPIT PDFs). Nothing has run against the live Screener/BSE/Groq path for a ticker other than KPITTECH's earlier
fixed-question tests. Do not describe this as generalising until `scripts/run_research_tests.py` passes on Render
for an unfamiliar ticker and the claims are checked by hand.

## Flow
ticker check -> Screener company page (name, NSE/BSE code) -> latest annual report + latest earnings-call transcript
with a link -> guarded download -> PDF text with PDF page numbers -> three model calls (overview, outlook, numbers)
-> mechanical checks -> brief with citations. Job based: `POST /api/analyze {ticker}` then `GET /api/analyze/{job_id}`.

## Protections
- Ticker: ASCII `[A-Z0-9&-]`, max 20 chars; unknown body fields (e.g. `url`) rejected with 422.
- Hosts: exact allowlist (www.screener.in, www.bseindia.com, nsearchives/archives.nseindia.com); https, port 443 only;
  redirects re-checked; host must resolve to public IPs only. Links on other hosts (e.g. company sites) are reported
  as `host_not_trusted`, never fetched.
- Caps: 35 MB download, 75 s per download, 80 s extraction, 270 s per job, 600 pages; PDFs must start with `%PDF-`.
- Limits: one analysis at a time; 8 live analyses/hour and 20/day (global); cached briefs cost no budget;
  model calls paced to ~6,500 tokens/min (Groq key limit is 8,000).

## States reported
ticker_not_found, source_unreachable, company_not_resolved, ticker_mismatch, documents_not_listed; per document:
host_not_trusted, not_found, inaccessible_http, timeout, too_large, not_pdf, malformed_pdf, encrypted,
scanned_no_text (no OCR), too_slow, partial (page/time cap). Sections: found / not found / unverified / unavailable.

## Known limits (unverified or by design)
- Screener's raw HTML was never seen; the parser was built from a text summary plus a defensive fallback. Tested only
  on a reconstructed fixture. First real run may need a parser fix (the job error includes diagnostics).
- Many transcripts/ARs are on company sites or listed without a link: those give "not found", not a guess.
- Speaker roles are detected from a "MANAGEMENT ... MR. X - title" block (KPIT format). Other formats fall back to
  plain windows with a stricter prompt and a visible warning; outlook quality there is unmeasured.
- Number extraction relies on a page that looks like an income statement; companies with unusual labels may get
  "not found". Scope/unit/period must appear in the cited passage; PDF fonts can turn the rupee sign into a backtick.
- Mechanical checks do not verify meaning. Page numbers are PDF indexes (KPIT AR spreads differ from printed pages).
- Disk cache lives in /tmp and is lost on restart; free instance spins down after ~15 min idle.
- NSE announcement/attachment endpoints were slow or timing out from Render, so exchange discovery is not used.

## Reliability changes (2026-11-01, second round)
- Job hard limit (FINN_JOB_HARD_LIMIT_S, default 420 s): a stuck job becomes `failed / job_timeout`; the slot is freed.
- Pipeline deadline (FINN_RESEARCH_DEADLINE_S, default 270 s): fetch/extract/model steps get what is left; steps skipped for time are reported, not hidden.
- Model-wait cap: the token pacer waits at most 70 s per call, then the section is marked unavailable.
- `outcome` on every job/brief: complete | partial | no_findings | snapshot_fallback | failed. The page shows a PARTIAL banner.
- Every poll response carries `instance`; an unknown job returns 404 with a note that the free server may have restarted.
- A rupee sign drawn with a font whose code is "H" or backtick is read as the rupee sign (HUL, KPIT). Pidilite's glyph is absent from the PDF text layer, so per-share items there fail the unit check (correct).
- Citation pages come from the quote's position inside the passage (`page_basis: located_in_passage`).
- scripts/run_research_tests.py saves after every poll and company; unexpected polls are saved with HTTP status and a redacted body.
- (round 3) Transcripts older than 15 months are not used as outlook (COFORGE's only accessible call was Feb 2022). One patient retry on model rate limits (waits the provider's retry-after if it fits the time budget); sections now carry `provider_detail` and `retry_after_s` when the model call fails.
- (round 4) A transient download failure (timeout, 5xx, connection error) on the newest document is retried once with a longer first-byte wait before an older document is used; if an older call is used, the outlook section says so. The date printed in a transcript is compared with Screener's month label (`date_matches_label`). Per-share items display as `64.01 ₹ per share` (no doubled sign). The test script flags stalls of its own loop (`client_pause_s`, `active_wall_s`).
- (round 5) Retrieval rules, all general: PDF ligatures (ﬁ, ﬂ) are spelled out at extraction so 'Proﬁt' matches 'profit'; an income statement is recognised by its line items (revenue from operations, total income, tax expense, profit lines, earnings per share), not only by a heading, and the passage starts at the statement when it sits below a balance sheet on a long page; the business overview prefers 'About ...' / 'Company overview' / 'Who we are' pages and the 'Corporate information' accounting note, and discounts CSR/ESG pages and director/address directories. Known gap: for some reports (HUL) the real description page is not ranked first.
