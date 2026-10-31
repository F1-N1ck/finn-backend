# Hosted open-weight Qwen: three options (docs read 31 Oct 2026)

Model: **Qwen3.8-27B**. Open weights: the Hugging Face repo's LICENSE file is Apache License 2.0 (fetched); release reported by eWeek on 17 Aug 2026 (secondary source). Not tested by me on any FINN task. Prices and limits change; re-check before relying on them. "Unverified" = the docs I could read did not say.

| | A. Groq | B. Cerebras | C. OpenRouter (aggregator) |
|---|---|---|---|
| Model id | `qwen/qwen3.8-27b`, labelled **Preview** | `qwen-3.8-27b` | `qwen/qwen3.8-27b` (many upstream providers) |
| Context | 131,072; max output 16,384 | 64k free / 128k paid | page shows 1.0M model-wide; per-provider not given |
| Paid price / 1M tokens (in / out) | $0.80 / $4.00 | Not found on pricing page (table did not render). OpenRouter lists a Cerebras route at $0.99 / $1.49 | Listed providers: from $0.035 / $1.250 (cheapest rows) to $0.99 / $1.49 and $0.45 / $3.20 |
| Free access | Free plan exists. Limits row for this model: 30 RPM, 1K requests/day, 8K tokens/min, 200K tokens/day. **The page also says its limits are the "base limits for the Developer plan", so which plan this row belongs to is unclear**; your account's Limits page is authoritative | "Free Trial": 5 RPM, 30K uncached tokens/min, 90K total tokens/min, 1M tokens/hour, 1M tokens/day | No free variant listed for this model. Free (`:free`) models elsewhere: 20 RPM and 50 requests/day, or 1000/day after buying 10 credits |
| Signup / payment | Upgrade to Developer needs a payment method. Whether the free plan needs a card: **unverified** | **Payment method required**: without it API access stays inactive. Adding one gives a one-time $5 credit, expires 30 days after activation; no automatic paid enrolment per the pricing page | Credits bought by card, AliPay or USDC. Whether sign-up needs a card: **unverified**. A fee applies when buying credits; exact rate **unverified** |
| Useful features | JSON object + JSON schema mode, tool use. `reasoning_effort: "none"` turns thinking off | Not verified (base URL, JSON mode, reasoning control not found in pages I read) | Depends on the upstream provider |
| Base URL (OpenAI-compatible) | `https://api.groq.com/openai/v1` (from docs) | Not confirmed from docs | Not fetched; commonly `https://openrouter.ai/api/v1` (unverified here) |
| Main risk for FINN | Preview status; small free token budget | Needs a payment method up front; trial credit expires in 30 days | Quality/latency/privacy differ by upstream; per-request cost is lowest but unpredictable routing |

Cost per request, **my arithmetic, assuming 3,000 input + 500 output tokens** (assumption, not measured): Groq $0.0044; Cerebras via OpenRouter listing $0.0037; cheapest OpenRouter row $0.0007; priciest listed row $0.0030. So 1,000 requests is roughly $0.70-$4.40 at these list prices.

## Recommendation (judgement, not a test result)
1. **Develop and demo against Groq `qwen/qwen3.8-27b`** through the provider-neutral adapter, because it has a documented free plan, JSON mode and a switch to turn thinking off. Do not treat the free plan as reliable: it is a Preview model and the daily token budget (if the 200K figure applies to you) is only about 60-70 requests of this size.
2. **Always ship labelled cached answers as the fallback** (the adapter does this). Live mode is a bonus, not a dependency.
3. **If you want live answers to be dependable for reviewers**, add a small prepaid balance on OpenRouter and point `FINN_LLM_BASE_URL`/`FINN_LLM_MODEL` at it. Check the minimum top-up and fee at checkout; I could not read them.
4. Cerebras is attractive on speed claims (page says ~1,850 tokens/s, unmeasured by me) but requires a payment method before any use.

Sources: [Groq model page](https://console.groq.com/docs/model/qwen/qwen3.8-27b), [Groq rate limits](https://console.groq.com/docs/rate-limits), [Groq billing FAQs](https://console.groq.com/docs/billing-faqs), [Groq OpenAI compatibility](https://console.groq.com/docs/openai), [Cerebras rate limits](https://inference-docs.cerebras.ai/support/rate-limits), [Cerebras models](https://inference-docs.cerebras.ai/models/overview), [Cerebras pricing](https://www.cerebras.ai/pricing), [OpenRouter limits](https://openrouter.ai/docs/api_reference/limits), [OpenRouter FAQ](https://openrouter.ai/docs/faq), [OpenRouter Qwen3.8 27B listing](https://openrouter.ai/qwen/qwen3.8-27b), [Qwen3.8-27B LICENSE](https://huggingface.co/Qwen/Qwen3.8-27B/blob/main/LICENSE), [eWeek](https://www.eweek.com/news/alibaba-qwen3-8-27b-license-apac-china/).
