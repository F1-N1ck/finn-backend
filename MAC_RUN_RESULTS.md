# Results from running on the Mac (31 Oct 2026, 20:54 IST)

Kept separate from the browser-pane tests in FINN_requirements_and_access_report.md.
Host: macOS 26.4, arm64, Python 3.14.5, 16.0 GB RAM (5.1 GB available at start).
**This is one machine on a home/campus network. It says nothing about any cloud host.**

## 1. Access probe (probe_access.py, plain `requests`, no browser)
| Source | Result |
|---|---|
| Yahoo chart (RELIANCE, TCS, KPITTECH `.NS`) | HTTP 200, 64 daily rows each |
| NSE page + announcements API (24-31 Oct window hard-coded in the script) | HTTP 200. RELIANCE 9, KPITTECH 11, **TCS 0** |
| Screener (3 symbols) | HTTP 200, documents section present for all 3 |
| BSE announcements API (500325, 542651) | **HTTP 403 both** |
Unverified: why TCS returned 0 (empty list, HTTP 200; could be no filings in the window, not checked). Why BSE API returns 403 here while it returned data in the browser pane and while BSE PDF downloads worked from the same Mac (see 2). host_check.py adds an Origin header variant; untested.

## 2. PDF text extraction (PyMuPDF 2.0)
| Document | Result |
|---|---|
| KPIT Q1 FY27 earnings call transcript (BSE, 483,932 bytes) | Download OK. 20 PDF pages, 49,756 chars, 0.13 s, **no flagged pages**. Page 1 is a cover letter dated 4 Aug 2026; call date 29 Jul 2026. Printed "Page N of 19" = PDF page - 1. |
| KPIT Annual Report 2025-26 (BSE, 10,301,220 bytes) | Download OK. 210 PDF pages, 1,202,590 chars, 2.79 s. 201 pages unflagged; 1 `low_text` (p2, 48 chars); 9 `full_page_image_with_text_layer` (p2,3,5,14,15,17,18,20,210). |
No page was flagged as scanned-without-text. I read the first ~140 characters of each flagged page: text layer is present and readable on those samples. Quality of the full text on those 9 pages is NOT verified; they look like design spreads (front section and back cover).

Findings that affect FINN design (all verified by reading the extracted text):
- **Annual report PDF pages are two-page spreads.** PDF page n carries printed pages 2n-4 and 2n-3 (checked on PDF pages 145-149: 286/287 ... 294/295; and p105 -> 206). Cite by PDF page index and show the printed numbers separately. Verified only for the body pages I checked, not all 210.
- **Statement titles can sit at the end of the page text.** PDF p149 starts with the table and ends with "CONSOLIDATED STATEMENT OF PROFIT AND LOSS". Chunking by 1,800-char windows can cut the title off; use whole-page passages for statements.
- Real figures on PDF p149 (read directly, ₹ million, FY26 vs FY25): Revenue from operations 64,549.31 vs 58,423.45; Profit for the year 6,373.70 vs 8,396.00 (owners 6,373.39).
- **Keyword-window retrieval missed the key page in the concall.** The explicit outlook statement is on PDF p4 ("H2 will be better than H1 ... return to growth in H2 by Q4"). With weighted guidance keywords, p4 ranked 8th of 20, so top-3 passages do not include it. This is a retrieval problem, not an extraction problem.

## 3. Local model (step 3/4 of the earlier kit)
**Did not run.** The run used MODEL=qwen2.5:3b (not Qwen3-4B). Ollama server: connection refused. No `ollama` binary found on PATH or in /Applications/Ollama.app; no llama.cpp binaries. ~/.ollama still holds a qwen2.5:3b manifest and a 1.93 GB blob, but I cannot tell whether that model is usable without the runtime. No latency, memory or accuracy numbers exist for any local model.

## 4. Other observations
- results/pip_freeze.txt lists pymupdf4llm, pymupdf_layout, onnxruntime, networkx, PyYAML etc. that my script did not install, so `.venv` was not pristine (something else installed into it, or it pre-existed).
