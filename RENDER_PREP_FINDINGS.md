# Render/Groq prep: findings (31 Oct 2026)

Nothing has been deployed to Render. No Groq request has been made. No claim here is about Render.

## Verified (by me, with evidence)
- **Code**: 46 tests pass in the sandbox (adapter 16, app 17, NSE verdicts 13). The model is mocked; no network.
- **App behaviour (mock model)**: cached mode never calls the model; missing key, provider error and exhausted budget fall back to a labelled cached answer with a reason code; an explicit `live` request returns an error instead of silently serving cache; a fabricated answer is returned with failed checks shown in its label; admin routes are 404 unless enabled, 401 on a bad token, 429 after 5 failures, and reject tokens in the URL.
- **Snapshot**: committed text (20-page concall, 210-page annual report) matches MANIFEST hashes. Cached answers are written to the repo, so a Render restart cannot remove them.
- **TCS zero results (browser pane on your Mac, 31 Oct)**: HTTP 200 with `[]` for 24-31 Oct is a valid empty window. TCS had 19 filings in the prior 30 days (newest 19 Oct 2026). Caveat: the same API also returns HTTP 200 `[]` for a lowercase symbol and for a nonexistent symbol, so `[]` alone proves nothing. `finn/nse.py` upper-cases the symbol and returns one of: ok_nonempty, valid_empty_window, ambiguous_empty, fetch_error, parse_error.

## Retrieval (KPIT outlook miss): improved on ONE transcript, not proven
Ground-truth pages (chosen by me by reading): 4, 6, 7, 9, 11, 16.

| Method | k=3 pages | Ground-truth pages hit | Key outlook sentence present |
|---|---|---|---|
| Old page windows | 8, 16, 7 | 7, 16 | no |
| Speaker turns + roles, old vocabulary | 3, 9, 19 | 9 | no |
| Speaker turns + roles, new vocabulary | 4, 9, 7 | 4, 7, 9 | yes |

- The gain comes mostly from the **new guidance vocabulary**, which I wrote after seeing where the answer was. That is partly circular. Turns + roles alone did not help.
- Still missed: pages 6, 11, 16. Not tested on any other transcript. Do not claim the research flow works in general.

## Not verified / untested
- Anything on Render: outbound access to Yahoo, NSE, Screener, BSE PDF from Render's IPs; cold start; 0.1 CPU speed; Python 3.13 and pinned wheels building there.
- Groq: that `qwen/qwen3.8-27b` is available to your account, whether `reasoning_effort: none` is accepted, free-plan limits and card requirement.
- Any real model answer. The two cached answers are **operator-written** and labelled "NOT model output".
- Manual claim-by-claim check of model answers (impossible until a live run exists).
- BSE announcements API: returned 403 from plain `requests` on your Mac; cause unknown. It works in a browser page. The host check records its result from Render.
- Host check re-extracts the BSE concall PDF on Render and compares page text to the snapshot, but the model excerpts come from the committed snapshot, not from that fresh download.

## Next step (yours)
Follow DEPLOY_RENDER.md, save the polled JSON as results/render_host_check.json, and tell me. I will then read it and check each answer and citation against the PDF text.
