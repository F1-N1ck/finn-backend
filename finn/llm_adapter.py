"""Server-side LLM adapter for FINN (OpenAI-compatible chat endpoint).

Rules this module follows:
- The API key is read ONLY from the environment (or a local .env file). It is never
  logged, never returned by public_dict(), and never put in an error message.
- Every call has a connect timeout and a read timeout.
- Failures raise a specific error class with a short code the UI can show.
- answer_with_fallback() returns a LIVE result, or a CACHED result clearly labelled
  as cached. If neither exists it raises NoAnswerAvailable. It never invents an answer.

Environment variables:
  FINN_LLM_API_KEY          required for live calls (secret)
  FINN_LLM_BASE_URL         required, e.g. https://api.groq.com/openai/v1
  FINN_LLM_MODEL            required, e.g. qwen/qwen3.8-27b
  FINN_LLM_TIMEOUT_S        read timeout seconds (default 30)
  FINN_LLM_CONNECT_TIMEOUT_S connect timeout seconds (default 5)
  FINN_LLM_MAX_RETRIES      retries on 429/5xx/timeout (default 1)
  FINN_LLM_MAX_TOKENS       max output tokens (default 700)
  FINN_LLM_JSON_MODE        1 = send response_format json_object (default 1)
  FINN_LLM_EXTRA_BODY       JSON object merged into the request body (provider options)
  FINN_LLM_PRICE_IN_PER_M   optional USD per 1M input tokens, for cost reporting
  FINN_LLM_PRICE_OUT_PER_M  optional USD per 1M output tokens
  FINN_CACHE_PATH           cached demo answers file (default data/cache/demo_answers.json)
"""

import datetime
import json
import os
import re
import time

import requests

RETRY_AFTER_CAP_SECONDS = 5.0


# ---------------------------------------------------------------- errors

class LLMError(Exception):
    code = "llm_error"

    def __init__(self, message, status=None, retry_after=None):
        Exception.__init__(self, message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


class LLMConfigError(LLMError):
    code = "config_missing"


class LLMAuthError(LLMError):
    code = "auth_failed"


class LLMBillingError(LLMError):
    code = "billing_required"


class LLMRateLimited(LLMError):
    code = "rate_limited"


class LLMTimeout(LLMError):
    code = "timeout"


class LLMUnavailable(LLMError):
    code = "provider_unavailable"


class LLMBadResponse(LLMError):
    code = "bad_response"


class LiveNotAttempted(LLMError):
    code = "live_not_attempted"


class NoAnswerAvailable(Exception):
    def __init__(self, reason_code, reason_message):
        Exception.__init__(self, reason_code + ": " + reason_message)
        self.reason_code = reason_code
        self.reason_message = reason_message


# ---------------------------------------------------------------- config

def load_dotenv_if_present(path=".env"):
    """Tiny .env reader. Only sets variables that are not already set. Prints nothing."""
    if not os.path.exists(path):
        return
    file_handle = open(path)
    for line in file_handle:
        line = line.strip()
        if line == "" or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name not in os.environ:
            os.environ[name] = value
    file_handle.close()


def _to_float(text, default):
    try:
        return float(text)
    except Exception:
        return default


def _to_int(text, default):
    try:
        return int(text)
    except Exception:
        return default


class LLMConfig:
    def __init__(self, api_key, base_url, model, timeout_s=30.0, connect_timeout_s=5.0,
                 max_retries=1, max_tokens=700, json_mode=True, extra_body=None,
                 price_in_per_m=None, price_out_per_m=None):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.json_mode = json_mode
        self.extra_body = extra_body or {}
        self.price_in_per_m = price_in_per_m
        self.price_out_per_m = price_out_per_m

    @classmethod
    def from_env(cls, env=None):
        if env is None:
            env = os.environ
        extra_text = env.get("FINN_LLM_EXTRA_BODY", "").strip()
        extra_body = {}
        if extra_text != "":
            try:
                extra_body = json.loads(extra_text)
            except Exception:
                raise LLMConfigError("FINN_LLM_EXTRA_BODY is not valid JSON")
            if not isinstance(extra_body, dict):
                raise LLMConfigError("FINN_LLM_EXTRA_BODY must be a JSON object")
        price_in = env.get("FINN_LLM_PRICE_IN_PER_M")
        price_out = env.get("FINN_LLM_PRICE_OUT_PER_M")
        return cls(
            api_key=env.get("FINN_LLM_API_KEY", ""),
            base_url=env.get("FINN_LLM_BASE_URL", ""),
            model=env.get("FINN_LLM_MODEL", ""),
            timeout_s=_to_float(env.get("FINN_LLM_TIMEOUT_S"), 30.0),
            connect_timeout_s=_to_float(env.get("FINN_LLM_CONNECT_TIMEOUT_S"), 5.0),
            max_retries=_to_int(env.get("FINN_LLM_MAX_RETRIES"), 1),
            max_tokens=_to_int(env.get("FINN_LLM_MAX_TOKENS"), 700),
            json_mode=env.get("FINN_LLM_JSON_MODE", "1") != "0",
            extra_body=extra_body,
            price_in_per_m=_to_float(price_in, None) if price_in else None,
            price_out_per_m=_to_float(price_out, None) if price_out else None,
        )

    def missing(self):
        names = []
        if self.api_key == "":
            names.append("FINN_LLM_API_KEY")
        if self.base_url == "":
            names.append("FINN_LLM_BASE_URL")
        if self.model == "":
            names.append("FINN_LLM_MODEL")
        return names

    def validate(self):
        names = self.missing()
        if len(names) > 0:
            raise LLMConfigError("Missing environment variables: " + ", ".join(names))
        is_local = self.base_url.startswith("http://localhost") or self.base_url.startswith("http://127.0.0.1")
        if not self.base_url.startswith("https://") and not is_local:
            raise LLMConfigError("FINN_LLM_BASE_URL must start with https:// (http allowed for localhost only)")

    def public_dict(self):
        key_state = "set" if self.api_key != "" else "missing"
        return {
            "api_key": key_state,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_s": self.timeout_s,
            "connect_timeout_s": self.connect_timeout_s,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "json_mode": self.json_mode,
            "extra_body_keys": sorted(self.extra_body.keys()),
        }

    def __repr__(self):
        return "LLMConfig(" + json.dumps(self.public_dict()) + ")"


# ---------------------------------------------------------------- helpers

def _scrub(text, api_key):
    if api_key:
        text = text.replace(api_key, "***")
    return text[:300]


def _provider_message(response, api_key):
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return _scrub(str(error["message"]), api_key)
            if isinstance(error, str):
                return _scrub(error, api_key)
    except Exception:
        pass
    return _scrub(response.text, api_key)


def _retry_after_seconds(response):
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- one call

def chat(config, messages):
    """Call the chat endpoint. Returns a dict. Raises an LLMError subclass on failure."""
    config.validate()
    url = config.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + config.api_key,
        "Content-Type": "application/json",
    }
    body = {
        "model": config.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": config.max_tokens,
    }
    if config.json_mode:
        body["response_format"] = {"type": "json_object"}
    for key in config.extra_body:
        body[key] = config.extra_body[key]

    attempts_allowed = config.max_retries + 1
    last_error = None
    for attempt in range(1, attempts_allowed + 1):
        started = time.time()
        error = None
        response = None
        try:
            response = requests.post(url, headers=headers, json=body,
                                     timeout=(config.connect_timeout_s, config.timeout_s))
        except requests.exceptions.Timeout:
            error = LLMTimeout("No response within " + str(config.timeout_s) + " s")
        except requests.exceptions.ConnectionError:
            error = LLMUnavailable("Could not connect to the model provider")
        except Exception as unexpected:
            error = LLMUnavailable("Request failed: " + type(unexpected).__name__)

        if error is None:
            status = response.status_code
            if status == 200:
                return _parse_success(config, response, time.time() - started, attempt)
            message = _provider_message(response, config.api_key)
            if status in (401, 403):
                raise LLMAuthError("Provider rejected the key or access: " + message, status=status)
            if status == 402:
                raise LLMBillingError("Provider says payment or credits are required: " + message, status=status)
            if status == 429:
                error = LLMRateLimited("Rate limited: " + message, status=status,
                                       retry_after=_retry_after_seconds(response))
            elif status == 408 or status >= 500:
                error = LLMUnavailable("Provider error " + str(status) + ": " + message, status=status)
            else:
                raise LLMBadResponse("Provider returned " + str(status) + ": " + message, status=status)

        last_error = error
        if attempt < attempts_allowed:
            wait = 1.0 * attempt
            if error.retry_after is not None:
                wait = error.retry_after
            if wait > RETRY_AFTER_CAP_SECONDS:
                raise error
            time.sleep(wait)
    raise last_error


def _parse_success(config, response, elapsed, attempt):
    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"]
    except Exception:
        raise LLMBadResponse("Provider reply was not in the expected chat format", status=200)
    if text is None:
        raise LLMBadResponse("Provider reply had empty content", status=200)
    usage = data.get("usage") or {}
    rate_headers = {}
    for name in response.headers:
        lowered = name.lower()
        if lowered.startswith("x-ratelimit") or lowered == "retry-after":
            rate_headers[lowered] = response.headers[name]
    cost = None
    if (config.price_in_per_m is not None and config.price_out_per_m is not None
            and usage.get("prompt_tokens") is not None and usage.get("completion_tokens") is not None):
        cost = (usage["prompt_tokens"] * config.price_in_per_m
                + usage["completion_tokens"] * config.price_out_per_m) / 1000000.0
    return {
        "text": strip_think(text),
        "model_reported": data.get("model"),
        "usage": usage,
        "latency_s": round(elapsed, 2),
        "attempts": attempt,
        "rate_limit_headers": rate_headers,
        "cost_usd_estimate": cost,
    }


# ---------------------------------------------------------------- cache + labels

def load_cache(path):
    if not os.path.exists(path):
        return {}
    try:
        file_handle = open(path)
        data = json.load(file_handle)
        file_handle.close()
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_cache_entry(path, key, entry):
    """Operator action: store a result as a cached demo answer. Written atomically."""
    folder = os.path.dirname(path)
    if folder != "":
        os.makedirs(folder, exist_ok=True)
    cache = load_cache(path)
    cache[key] = entry
    temp_path = path + ".tmp"
    file_handle = open(temp_path, "w")
    json.dump(cache, file_handle, indent=2)
    file_handle.close()
    os.replace(temp_path, path)


def make_label(mode, model, generated_at, verification, source_type="model"):
    if mode == "live":
        return "LIVE analysis | model " + str(model) + " | " + generated_at + " | " + verification
    if source_type != "model":
        who = "operator-written extract, NOT model output"
    else:
        who = "model " + str(model)
    return "CACHED demo result | " + who + " | generated " + str(generated_at) + " | " + verification


def answer_with_fallback(config, cache_key, messages, parse_fn, cache_path, allow_live=True,
                         verification_fn=None):
    """Try live. On any LLMError, fall back to a cached entry. Else raise NoAnswerAvailable.

    parse_fn(text) must return a dict (the structured answer) or raise ValueError.
    allow_live=False skips the provider call (e.g. budget used up) and goes straight to cache.
    verification_fn(parsed) -> short text describing automatic checks, shown in the live label.
    """
    try:
        if not allow_live:
            raise LiveNotAttempted("Live call skipped (budget or configuration)")
        raw = chat(config, messages)
        try:
            parsed = parse_fn(raw["text"])
        except ValueError as parse_error:
            raise LLMBadResponse("Model reply could not be parsed: " + str(parse_error))
        generated_at = now_iso()
        verification = "automatic checks only"
        if verification_fn is not None:
            verification = verification_fn(parsed)
        return {
            "mode": "live",
            "label": make_label("live", config.model, generated_at, verification),
            "model": config.model,
            "generated_at": generated_at,
            "result": parsed,
            "meta": {k: raw[k] for k in ("usage", "latency_s", "attempts",
                                         "rate_limit_headers", "cost_usd_estimate")},
        }
    except LLMError as error:
        entry = load_cache(cache_path).get(cache_key)
        if entry is None:
            raise NoAnswerAvailable(error.code, error.message)
        verification = entry.get("verification", "automatic checks only")
        return {
            "mode": "cached",
            "label": make_label("cached", entry.get("model"), entry.get("generated_at"), verification,
                                entry.get("source_type", "model")),
            "model": entry.get("model"),
            "source_type": entry.get("source_type", "model"),
            "generated_at": entry.get("generated_at"),
            "result": entry.get("result"),
            "passages": entry.get("passages"),
            "fallback_reason": {"code": error.code, "message": error.message},
            "meta": entry.get("meta"),
        }
