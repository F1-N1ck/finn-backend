"""Test doubles for the research flow: a fake HTTP session and a fake model that quotes the passages."""

import json
import re


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def close(self):
        pass


class FakeSession:
    """routes: {url: FakeResponse or callable}. Unknown URLs -> 404. Records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        self.header_log = []
        self.headers = {}

    def get(self, url, stream=True, allow_redirects=False, timeout=None, headers=None):
        self.requests.append(url)
        self.header_log.append((url, dict(headers or {})))
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(404)
        if callable(route):
            return route()
        return route


def public_resolver(host):
    return ["104.18.0.1"]


PASSAGE_RE = re.compile(r"\[(S\d+)\] \(PDF page (\d+)[^\n]*\)\n")


def parse_passages(user_text):
    marks = list(PASSAGE_RE.finditer(user_text))
    passages = {}
    for index, mark in enumerate(marks):
        end = len(user_text)
        if index + 1 < len(marks):
            end = marks[index + 1].start()
        body = user_text[mark.end():end]
        body = re.split(r"\n\n(?:Question|Task):", body)[0]
        passages[mark.group(1)] = body
    return passages


def make_fake_chat(mode="good", calls=None):
    """mode: good | hallucinate | not_stated | bad_json | fail"""
    from finn import llm_adapter

    def chat(config, messages):
        if calls is not None:
            calls.append(messages[0]["content"][:60])
        if mode == "fail":
            raise llm_adapter.LLMUnavailable("provider down")
        if mode == "bad_json":
            return {"text": "sorry, no json", "usage": {}, "latency_s": 0.1}
        system = messages[0]["content"]
        passages = parse_passages(messages[1]["content"])
        first_id = sorted(passages)[0]
        text = passages[first_id]
        if mode == "not_stated":
            out = {"not_stated": True, "claims": [], "items": []}
        elif "selected reported numbers" in system:
            match = re.search(r"Revenue from operations\s*\n\s*\d+\s*\n\s*([\d,\.]+)", text)
            if match is None:
                out = {"not_stated": True, "items": []}
            else:
                quote = match.group(0)
                value = match.group(1)
                if mode == "hallucinate":
                    value = "99,999.99"
                out = {"not_stated": False, "items": [{
                    "metric": "Revenue from operations", "value": value, "unit": "₹ million",
                    "period": "Year ended 31 March 2026",
                    "scope": "consolidated" if "consolidated" in text.lower() else "not stated",
                    "source": first_id, "quote": quote}]}
        else:
            flat = " ".join(text.split())
            sentences = [x for x in re.split(r"(?<=[.?!])\s+(?=[A-Z0-9])", flat) if 40 <= len(x) <= 260]
            if sentences:
                snippet = sentences[0]  # a whole sentence, so nothing its own sentence says is left out of the claim
            else:
                snippet = flat[40:200]
                snippet = snippet[snippet.find(" ") + 1:snippet.rfind(" ")]
            if mode == "hallucinate":
                snippet = "management guided for 40% revenue growth next year"
            out = {"not_stated": False,
                   "claims": [{"text": "claim: " + snippet, "source": first_id, "quote": snippet}]}
        return {"text": json.dumps(out), "usage": {"total_tokens": 900}, "latency_s": 0.2}

    return chat


def make_pdf_from_pages(pages):
    """Build a text PDF from [{"page", "text"}] (used to rebuild KPIT-like documents from the saved text)."""
    import pymupdf
    doc = pymupdf.open()
    for entry in pages:
        page = doc.new_page(width=600, height=4000)
        y = 20
        for line in entry["text"].split("\n"):
            # the built-in PDF font cannot draw these characters; swap for plain ones
            line = (line.replace("–", "-").replace("’", "'").replace("‘", "'").replace("“", '"')
                    .replace("”", '"').replace("₹", "`"))
            page.insert_text((20, y), line, fontsize=6)
            y = y + 7
    data = doc.tobytes()
    doc.close()
    return data
