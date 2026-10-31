# Render host check: results and manual review (31 Oct 2026, ~21:52 IST)

Service: Render free instance, Python 3.13.18, Linux (AWS). Source file: results/render_host_check.json (job status "done", 49.3 s).

## Works (from Render)
| Check | Result |
|---|---|
| FINN_LLM_API_KEY configured | `/healthz`: live_model_configured true (value never shown). Groq accepted it (see model rows). |
| Yahoo chart, RELIANCE/TCS/KPITTECH .NS | HTTP 200, 64 rows each, last bar 2026-10-30; closes 1164.90 / 2502.60 / 530.25 match the earlier browser test. |
| Screener KPITTECH | HTTP 200, documents section present, 43 PDF links. |
| BSE concall PDF | HTTP 200, 483,932 bytes (same size as my copy), 20 pages, text of all 20 pages identical to the committed snapshot. |
| BSE annual report | HTTP 200, `%PDF-`, content-length 10,301,220 (matches my copy). Only first bytes fetched; text not re-extracted on Render. |
| Groq, 2 requests | Both `live`, no fallback, 1 attempt, 1.2 s and 1.4 s, 1,753 and 2,016 tokens. Headers show 1,000 requests/day and 8,000 tokens/min limits for this key. |
| Snapshot integrity | ok |

## Fails
- **NSE: ReadTimeout after 30 s** at the first call (page warm-up inside `make_session`). Nothing else in the NSE step ran, so the TCS verdict and NSE attachment download are **untested from Render**. Cause unknown (cloud-IP blocking is plausible, not shown).
- **BSE announcements API: HTTP 403** from Render (same as on your Mac).

## Manual claim check against the source PDFs
Checked by rendering the pages from the PDFs and reading them. Mechanical checks passed 3/3 and 4/4; this is the human part.

**Q2 (annual report, PDF p149, printed pages 294-295): all 4 claims correct.**
- Revenue from operations 64,549.31 (FY26) and 58,423.45 (FY25): on printed p294. Profit for the year 6,373.70 and 8,396.00: on printed p295 ("Cont."). Both halves sit on PDF page 149, so the single citation is valid.
- Gap: the answer omits the unit (₹ million) and says "year ended 31 March" without "consolidated".

**Q1 (concall, July 29 2026): quotes real, speaker labels wrong, one claim over-reaches.**
- Claim 1 (H2 better than H1, return to growth in H2 by Q4): correct, PDF p4 (printed p3).
- Claim 2 (margins improve incrementally until revenue grows): correct, PDF p9 (printed p8).
- Claim 3 ("22% to 24% by FY'29 can still hold"): partly supported. The 22-24%/FY'29 figures are in the **analyst's question**. Kishor Patil's reply says medium-term outlook is good and "we will be in a position to get where we have mentioned". The quote does not contain the figures; the answer states them as management's belief. Treat as an inference, not a statement.
- Answer omits management's other guidance (commercial-vehicle growth next quarter, Qorix loss for "next quarter or two").
- **Speaker labels on the passages were wrong**: S1 said Sunil Phansalkar, S2 said Priya Hardikar, S3 said Nitin Padmanabhan (an analyst). The actual speaker of all three quotes is Kishor Patil (CEO & MD per the annual report signature block). Page citations printed in the PDF footer are one lower than PDF page numbers (cover page); the app cites PDF page.

## Bug found and fixed (not yet redeployed)
`_strip_headers` deleted any line on at least 60% of pages. "Kishor Patil:" is such a line, so his turns were merged into the previous speaker's. Fixed (speaker labels are never stripped); regression test added. Turns 68 to 92, management turns 16 to 40. Re-running the KPIT outlook retrieval on the fixed code: top 3 = pages 4, 7, 11; top 4 = 4, 7, 11, 9; the key outlook sentence is found; all passages correctly Kishor Patil. Still one transcript, ground truth chosen by me.

## Not tested
- NSE from Render (all of it); BSE API cause; Groq with the fixed retrieval; any other company or transcript; long-run stability; free-instance cold start time (host check ran on an already-awake instance).
- Cached answers are still operator-written. The live Q2 answer is a correct model answer and could be promoted (`scripts/promote_to_snapshot.py`); the live Q1 answer should NOT be promoted as is.

---
# Update: clean re-run after fixes (commit f6c084d, 16:46 UTC)

## Works from Render
- Yahoo (3 symbols), Screener, BSE concall PDF (20/20 pages identical to snapshot), BSE annual report headers, snapshot integrity.
- Groq: both requests live, no fallback. Q1 1.07 s / 1,545 tokens; Q2 1.3 s / 2,016 tokens (2 attempts: a retry happened, reason not recorded).
- NSE announcements API: works (HTTP 200 lists) but **about 20 s per call**. RELIANCE 8 filings, KPITTECH 10 filings in 7 days. The page warm-up (cookies) timed out every time and was not needed.
- **TCS: `valid_empty_window`** from Render. 0 filings in 7 days, 45 in 90 days, newest 19-Oct-2026. Earlier run: uppercase TCS 30-day query returned 19 filings; the market-wide window had 4,631 records and no TCS rows.

## Fails / untested
- BSE announcements API: 403 (third time). NSE attachment PDF (nsearchives.nseindia.com): timed out at 45 s in the earlier run; not retried (budget).
- Lowercase `tcs` control from Render: non-list result, reason not recorded in that run (logging added; not re-run, NSE time budget was spent).
- Untested: a second company's transcript; ranking quality beyond one transcript; cold-start time.

## Manual check of the new Q1 answer (all quotes confirmed on the PDF pages)
- "H2 will be better than H1" / "return to growth in H2 by Q4": PDF p4 (printed p3), Kishor Patil, Co-Founder, CEO and MD (introduced on PDF p3). Correct.
- "margins will improve incrementally, not significantly until revenues go back to growing ways": PDF p9, Kishor Patil. Correct.
- "Qorix loss will continue for at least next quarter or two": PDF p9, same turn. Correct.
- Speaker labels on all four passages are now Kishor Patil (management), which is right.
- Incomplete: it omits commercial-vehicle growth next quarter and the medium-term outlook that retrieval did return (S2). No numeric guidance exists in these passages; the answer says none is given as a number, which is consistent.
- Q2 re-check: identical answer to the first run; all 4 claims correct (see above). Missing unit (₹ million) and word "consolidated".

## Host check disabled in repo
`render.yaml` now sets FINN_ENABLE_HOST_CHECK to "0". After push and deploy, /admin routes return 404.
