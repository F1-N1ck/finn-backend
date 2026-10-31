# Deployment recommendation (design reasoning, NOT measured)

Status of evidence: no server/cloud host has been tested. NSE/BSE/Yahoo/screener access is verified from the browser pane only. Qwen3-4B has not been run. Treat everything below as a proposal to validate with the Mac run and the probe on the chosen host.

## Simplest approach
1. **Ingest on your Mac** (the only environment where access has worked): fetch EOD, announcements, PDFs; extract text per page; store as files in the repo (data/text/*.pages.jsonl, announcements.json, prices.json).
2. **Live research = retrieval over already-extracted text + one LLM call.** It needs no NSE/BSE access at request time, only an LLM endpoint. This is the smallest thing that is honestly "live".
3. **Cached demo answers = precomputed outputs saved with provenance.** Each stored answer records: question, model, generated_at, source ids (doc, page), and the check report.
4. **Always label the mode.** Every API response carries `mode: "cached" | "live"`, `model`, `generated_at`, `sources`. The UI shows a visible badge ("Cached, generated <time> by <model>" vs "Live analysis"). A cached answer is never shown as live.
5. **Fallback rule:** if the live model endpoint fails or times out, return the cached answer for that exact question marked "cached (live unavailable)". If none exists, say so; do not guess.

## Configurable model access
One OpenAI-compatible client driven by env vars: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT`.
- Dev: Ollama on the Mac (Ollama documents an OpenAI-compatible /v1 endpoint; verify on your version).
- Live demo: a hosted API on a free tier, because graders cannot reach your laptop. Which provider and its limits: NOT checked.
- Routing idea (the brief rewards efficiency): small model for classification and extraction, stronger model only for narrative-vs-numbers. Unmeasured until the eval shows where Qwen3-4B fails.

## Hosting
One small web service plus static mobile web app. Host choice is open and unverified: before committing, run probe_access.py on that host only if the service needs NSE/BSE/Yahoo at request time (this design avoids that).

## Concrete next step
Run `bash run_all.sh`, read results/, then decide whether the local model is good enough for classification only or also for QA.
