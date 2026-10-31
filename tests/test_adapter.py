"""Adapter tests against a LOCAL MOCK provider. They test the adapter's error handling,
labels and fallback. They say nothing about any real provider or model."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from finn import llm_adapter as a
from finn import grounded_qa as g

SECRET = "test-secret-key-123"
QUEUE = []
SEEN = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length))
        SEEN.append({"auth": self.headers.get("Authorization"), "body": body})
        status, headers, payload, delay = QUEUE.pop(0)
        if delay:
            time.sleep(delay)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        for name in headers:
            self.send_header(name, headers[name])
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def ok_reply(text, prompt_tokens=100, completion_tokens=20):
    return (200, {"x-ratelimit-remaining-requests": "999"},
            {"model": "mock-model", "choices": [{"message": {"content": text}}],
             "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}, 0)


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        del QUEUE[:]
        del SEEN[:]
        self.config = a.LLMConfig(api_key=SECRET, base_url="http://127.0.0.1:%d/v1" % self.port,
                                  model="mock-model", timeout_s=1.0, connect_timeout_s=1.0,
                                  max_retries=1, price_in_per_m=1.0, price_out_per_m=2.0,
                                  extra_body={"reasoning_effort": "none"})
        self.messages = [{"role": "user", "content": "hi"}]
        self.cache_path = os.path.join(tempfile.mkdtemp(), "cache.json")

    def test_missing_env_names_only(self):
        config = a.LLMConfig.from_env({})
        with self.assertRaises(a.LLMConfigError) as ctx:
            a.chat(config, self.messages)
        self.assertIn("FINN_LLM_API_KEY", ctx.exception.message)

    def test_key_never_in_repr_or_public_dict(self):
        self.assertNotIn(SECRET, repr(self.config))
        self.assertNotIn(SECRET, json.dumps(self.config.public_dict()))

    def test_https_required_for_remote(self):
        config = a.LLMConfig(SECRET, "http://example.com/v1", "m")
        with self.assertRaises(a.LLMConfigError):
            config.validate()

    def test_success_cost_headers_extra_body(self):
        QUEUE.append(ok_reply('{"x":1}', 1000000, 500000))
        result = a.chat(self.config, self.messages)
        self.assertEqual(result["text"], '{"x":1}')
        self.assertAlmostEqual(result["cost_usd_estimate"], 2.0)
        self.assertIn("x-ratelimit-remaining-requests", result["rate_limit_headers"])
        self.assertEqual(SEEN[0]["body"]["reasoning_effort"], "none")
        self.assertEqual(SEEN[0]["auth"], "Bearer " + SECRET)

    def test_401_maps_and_scrubs_key(self):
        QUEUE.append((401, {}, {"error": {"message": "bad key " + SECRET}}, 0))
        with self.assertRaises(a.LLMAuthError) as ctx:
            a.chat(self.config, self.messages)
        self.assertNotIn(SECRET, ctx.exception.message)

    def test_402_billing(self):
        QUEUE.append((402, {}, {"error": {"message": "insufficient credits"}}, 0))
        with self.assertRaises(a.LLMBillingError):
            a.chat(self.config, self.messages)

    def test_429_then_success_retries(self):
        QUEUE.append((429, {"retry-after": "0"}, {"error": {"message": "slow down"}}, 0))
        QUEUE.append(ok_reply("{}"))
        result = a.chat(self.config, self.messages)
        self.assertEqual(result["attempts"], 2)

    def test_429_long_retry_after_fails_fast(self):
        QUEUE.append((429, {"retry-after": "99"}, {"error": {"message": "daily limit"}}, 0))
        started = time.time()
        with self.assertRaises(a.LLMRateLimited):
            a.chat(self.config, self.messages)
        self.assertLess(time.time() - started, 3)

    def test_500_twice_unavailable(self):
        QUEUE.append((500, {}, {"error": {"message": "boom"}}, 0))
        QUEUE.append((500, {}, {"error": {"message": "boom"}}, 0))
        with self.assertRaises(a.LLMUnavailable):
            a.chat(self.config, self.messages)

    def test_timeout(self):
        self.config.max_retries = 0
        self.config.timeout_s = 0.3
        QUEUE.append(ok_reply("{}")[:3] + (1.0,))
        with self.assertRaises(a.LLMTimeout):
            a.chat(self.config, self.messages)

    def test_malformed_body(self):
        QUEUE.append((200, {}, b"not json at all", 0))
        with self.assertRaises(a.LLMBadResponse):
            a.chat(self.config, self.messages)

    def test_fallback_to_cache_is_labelled(self):
        a.save_cache_entry(self.cache_path, "k", {"model": "cached-model", "generated_at": "2026-10-31T10:00:00Z",
                                                  "result": {"answer": "old"}, "verification": "manually checked"})
        QUEUE.append((500, {}, {"error": {"message": "x"}}, 0))
        QUEUE.append((500, {}, {"error": {"message": "x"}}, 0))
        outcome = a.answer_with_fallback(self.config, "k", self.messages, g.parse_answer_json, self.cache_path)
        self.assertEqual(outcome["mode"], "cached")
        self.assertTrue(outcome["label"].startswith("CACHED demo result"))
        self.assertIn("manually checked", outcome["label"])
        self.assertEqual(outcome["fallback_reason"]["code"], "provider_unavailable")

    def test_no_cache_no_answer(self):
        QUEUE.append((401, {}, {"error": {"message": "x"}}, 0))
        with self.assertRaises(a.NoAnswerAvailable) as ctx:
            a.answer_with_fallback(self.config, "missing", self.messages, g.parse_answer_json, self.cache_path)
        self.assertEqual(ctx.exception.reason_code, "auth_failed")

    def test_live_label(self):
        QUEUE.append(ok_reply('{"answer":"a","claims":[]}'))
        outcome = a.answer_with_fallback(self.config, "k", self.messages, g.parse_answer_json, self.cache_path)
        self.assertEqual(outcome["mode"], "live")
        self.assertTrue(outcome["label"].startswith("LIVE analysis"))

    def test_unparseable_model_text_falls_back(self):
        a.save_cache_entry(self.cache_path, "k", {"model": "m", "generated_at": "t", "result": {}})
        QUEUE.append(ok_reply("sorry, no json here"))
        outcome = a.answer_with_fallback(self.config, "k", self.messages, g.parse_answer_json, self.cache_path)
        self.assertEqual(outcome["mode"], "cached")
        self.assertEqual(outcome["fallback_reason"]["code"], "bad_response")

    def test_claim_checker_catches_fabrication(self):
        passages = [{"source_id": "S1", "page": 1, "text": "Revenue from operations 64,549.31 for the year."}]
        good = {"answer": "64,549.31", "claims": [{"text": "Revenue 64,549.31", "source": "S1",
                                                    "quote": "Revenue from operations 64,549.31"}]}
        bad = {"answer": "65,000", "claims": [{"text": "Revenue 65,000", "source": "S1",
                                                "quote": "Revenue from operations 65,000"}]}
        self.assertEqual(g.check_claims(good, passages)["auto_pass_count"], 1)
        report = g.check_claims(bad, passages)
        self.assertEqual(report["auto_pass_count"], 0)
        self.assertEqual(report["answer_numbers_not_in_passages"], ["65000"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
