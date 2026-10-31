"""Tests for the generic research flow. No network, no real model: fakes only.
The real-PDF test at the bottom is skipped when the PDFs are not on this machine."""

import json
import os
import threading
import time
import warnings

import pytest
import requests

warnings.filterwarnings("ignore")

from finn import snapshot
from finn.research import cache, discover, extract, jobs, pacer, pipeline, safe_fetch, sections, validate
from tests import research_fakes as fk

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "screener_kpit_reconstructed.html")
AR_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/4d2467e3-e28a-472b-8492-7fd9e46e63c4.pdf"
AR_OLD_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/0e6d2634-be8b-41d7-93c4-9b3fd8b78107.pdf"
CALL_URL = "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d07f9c52-5c5a-443b-b239-6acd5e94b97a.pdf"
SCREENER = "https://www.screener.in/company/KPITTECH/"


def fixture_html():
    return open(FIXTURE).read()


# ------------------------------------------------------------------ ticker validation

@pytest.mark.parametrize("raw,expected", [
    ("kpittech", "KPITTECH"), (" TCS ", "TCS"), ("M&M", "M&M"), ("BAJAJ-AUTO", "BAJAJ-AUTO"),
    ("NSE:INFY", "INFY"), ("20MICRONS", "20MICRONS"),
])
def test_valid_tickers(raw, expected):
    symbol, code, _ = validate.validate_ticker(raw)
    assert symbol == expected and code is None


@pytest.mark.parametrize("raw", [
    "", "   ", "A B", "../etc/passwd", "TCS;DROP", "X" * 21, "TCS/", "http://evil.com", "evil.com",
    "TCS\nHDFC", None, 123, ["TCS"], "ß", "ТСS", "-TCS", "&TCS", "TCS?x=1", "TCS#a", "TCS%2F",
])
def test_invalid_tickers(raw):
    symbol, code, _ = validate.validate_ticker(raw)
    assert symbol is None and code == "invalid_ticker"


# ------------------------------------------------------------------ URL and fetch guards

@pytest.mark.parametrize("url,code", [
    ("http://www.bseindia.com/x.pdf", "bad_url"),
    ("ftp://www.bseindia.com/x.pdf", "bad_url"),
    ("https://evil.com/x.pdf", "host_not_trusted"),
    ("https://www.bseindia.com.evil.com/x.pdf", "host_not_trusted"),
    ("https://evilbseindia.com/x.pdf", "host_not_trusted"),
    ("https://bseindia.com/x.pdf", "host_not_trusted"),
    ("https://www.kpit.com/wp-content/x.mp3", "host_not_trusted"),
    ("https://user:pw@www.bseindia.com/x.pdf", "bad_url"),
    ("https://www.bseindia.com:8443/x.pdf", "bad_url"),
    ("https://127.0.0.1/x.pdf", "host_not_trusted"),
    ("https://localhost/x.pdf", "host_not_trusted"),
    ("https://169.254.169.254/latest/meta-data", "host_not_trusted"),
    ("https://[::1]/x", "host_not_trusted"),
    ("https://www.bseindia.com/a b.pdf", "bad_url"),
    ("https://www.bseindia.com/x.pdf\r\nHost: evil", "bad_url"),
    ("", "bad_url"), (None, "bad_url"),
])
def test_check_url_rejects(url, code):
    ok, reason = safe_fetch.check_url(url)
    assert not ok and reason == code


def test_check_url_accepts_trusted():
    for url in [AR_URL, CALL_URL, SCREENER, "https://nsearchives.nseindia.com/corporate/x.pdf"]:
        assert safe_fetch.check_url(url) == (True, None)


def pdf_bytes(pages=3, text="hello world this is a test page with some words"):
    import pymupdf
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), text + " page " + str(number + 1))
    data = doc.tobytes()
    doc.close()
    return data


def do_fetch(routes, url=AR_URL, **kwargs):
    session = fk.FakeSession(routes)
    kwargs.setdefault("resolver", fk.public_resolver)
    kwargs.setdefault("expect_pdf", True)
    return safe_fetch.fetch(url, session=session, **kwargs), session


def test_fetch_ok_pdf():
    result, _ = do_fetch({AR_URL: fk.FakeResponse(200, pdf_bytes())})
    assert result["ok"] and result["data"][:5] == b"%PDF-"


def test_fetch_html_error_page_is_not_pdf():
    result, _ = do_fetch({AR_URL: fk.FakeResponse(200, b"<html>Access denied</html>")})
    assert result["state"] == "not_pdf"


def test_fetch_declared_too_large_never_reads_body():
    result, _ = do_fetch({AR_URL: fk.FakeResponse(200, b"%PDF-1.4", {"Content-Length": "999999999"})})
    assert result["state"] == "too_large"


def test_fetch_streaming_cap():
    body = b"%PDF-" + b"x" * 200000
    result, _ = do_fetch({AR_URL: fk.FakeResponse(200, body)}, max_bytes=100000)
    assert result["state"] == "too_large"


@pytest.mark.parametrize("status,state", [(404, "not_found"), (403, "inaccessible_http"), (500, "inaccessible_http")])
def test_fetch_http_errors(status, state):
    result, _ = do_fetch({AR_URL: fk.FakeResponse(status)})
    assert result["state"] == state


def test_fetch_never_sends_request_to_bad_url():
    for url in ["http://www.bseindia.com/x.pdf", "https://evil.com/x.pdf"]:
        result, session = do_fetch({}, url=url)
        assert not result["ok"] and session.requests == []


def test_fetch_redirect_to_untrusted_host_is_blocked():
    routes = {AR_URL: fk.FakeResponse(302, b"", {"Location": "https://evil.com/steal.pdf"})}
    result, session = do_fetch(routes)
    assert result["state"] == "host_not_trusted" and session.requests == [AR_URL]


def test_fetch_redirect_to_http_is_blocked():
    routes = {AR_URL: fk.FakeResponse(301, b"", {"Location": "http://www.bseindia.com/x.pdf"})}
    result, _ = do_fetch(routes)
    assert result["state"] == "bad_url"


def test_fetch_follows_trusted_redirect():
    routes = {AR_URL: fk.FakeResponse(302, b"", {"Location": "/moved.pdf"}),
              "https://www.bseindia.com/moved.pdf": fk.FakeResponse(200, pdf_bytes())}
    result, _ = do_fetch(routes)
    assert result["ok"]


def test_fetch_redirect_loop():
    routes = {AR_URL: fk.FakeResponse(302, b"", {"Location": AR_URL})}
    result, _ = do_fetch(routes)
    assert result["state"] == "too_many_redirects"


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "not-an-ip"])
def test_fetch_rejects_private_dns(address):
    result, session = do_fetch({AR_URL: fk.FakeResponse(200, pdf_bytes())}, resolver=lambda host: [address])
    assert result["state"] == "dns_not_public" and session.requests == []


def test_fetch_rejects_if_any_address_private():
    result, _ = do_fetch({AR_URL: fk.FakeResponse(200, pdf_bytes())}, resolver=lambda host: ["104.18.0.1", "10.1.1.1"])
    assert result["state"] == "dns_not_public"


def test_fetch_timeout_and_connection_error():
    def boom():
        raise requests.exceptions.ReadTimeout("slow")
    result, _ = do_fetch({AR_URL: boom})
    assert result["state"] == "timeout"

    def refused():
        raise requests.exceptions.ConnectionError("refused")
    result, _ = do_fetch({AR_URL: refused})
    assert result["state"] == "inaccessible"


# ------------------------------------------------------------------ discovery parsing

def test_parse_fixture_lists_documents():
    parsed = discover.parse_screener(fixture_html(), SCREENER)
    assert parsed["company_name"] == "KPIT Technologies Ltd"
    assert parsed["nse_symbol"] == "KPITTECH" and parsed["bse_code"] == "542651"
    assert [a["year"] for a in parsed["annual_reports"]] == [2026, 2025]
    labels = [c["label"] for c in parsed["concalls"]]
    assert labels == ["Oct 2026", "Aug 2026", "May 2026"]
    assert parsed["concalls"][0]["transcript_url"] is None
    assert parsed["diagnostics"]["parse_mode"] == "headings"


def test_selection_skips_newer_call_without_transcript_and_says_so():
    parsed = discover.parse_screener(fixture_html(), SCREENER)
    selected = discover.select_documents(parsed)
    assert selected["concall_candidates"][0]["label"] == "Aug 2026"
    assert selected["annual_report_candidates"][0]["year"] == 2026
    assert any("Oct 2026" in note for note in selected["notes"])


VARIANT = """
<html><body><h1>Pidilite Industries Ltd</h1>
<span>BSE: 500331</span> <span>NSE: PIDILITIND</span>
<h2>Documents</h2>
<h3>Annual reports</h3>
<ul><li><a href="/static/ar2025.pdf">Financial Year 2025</a></li>
<li><a href="https://www.pidilite.com/ar2024.pdf">Financial Year 2024</a></li></ul>
<h3>Concalls</h3>
<ul>
<li class="flex"><div>Nov 2026</div><a class="concall-link" href="https://www.pidilite.com/t.pdf">Transcript</a></li>
<li class="flex"><div>Aug 2026</div><a class="concall-link" href="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=zz.pdf">Transcript</a></li>
</ul></body></html>
"""


def test_variant_markup_relative_links_and_untrusted_hosts():
    parsed = discover.parse_screener(VARIANT, SCREENER)
    selected = discover.select_documents(parsed)
    # relative AR link resolves against screener (a trusted host); the company-site link is skipped
    assert selected["annual_report_candidates"][0]["url"] == "https://www.screener.in/static/ar2025.pdf"
    # newest concall is on the company's own site -> skipped with an explicit reason
    assert selected["concall_candidates"][0]["label"] == "Aug 2026"
    joined = " ".join(selected["notes"])
    assert "host_not_trusted" in joined and "www.pidilite.com" in joined


def test_fallback_parser_when_headings_missing():
    html = ("<html><body><h1>Some Co</h1><p>NSE: SOMECO</p>"
            "<a href='https://www.bseindia.com/a.pdf'>Annual Report 2025</a>"
            "<a href='https://www.bseindia.com/t.pdf'>Transcript</a></body></html>")
    parsed = discover.parse_screener(html, SCREENER)
    assert parsed["diagnostics"]["parse_mode"] == "fallback_by_link_text"
    assert len(parsed["annual_reports"]) == 1 and len(parsed["concalls"]) == 1


def test_screener_url_is_encoded_and_fixed_host():
    assert discover.screener_url("M&M") == "https://www.screener.in/company/M%26M/"
    assert discover.screener_url("BAJAJ-AUTO").startswith("https://www.screener.in/company/")


def run_discover(html=None, status=200, symbol="KPITTECH", routes=None):
    if routes is None:
        routes = {"https://www.screener.in/company/" + symbol + "/": fk.FakeResponse(status, (html or "").encode())}
    return discover.discover(symbol, session=fk.FakeSession(routes), resolver=fk.public_resolver)


def test_discover_states():
    assert run_discover(fixture_html())["state"] == "ok"
    assert run_discover(status=404)["state"] == "ticker_not_found"
    assert run_discover(status=503)["state"] == "source_unreachable"
    assert run_discover("<html><body><p>nothing</p></body></html>")["state"] == "company_not_resolved"
    other = fixture_html().replace("NSE: KPITTECH", "NSE: OTHERCO")
    assert run_discover(other)["state"] == "ticker_mismatch"
    nodocs = "<html><body><h1>X Ltd</h1><span>NSE: KPITTECH</span><h3>Concalls</h3></body></html>"
    result = run_discover(nodocs)
    assert result["state"] == "documents_not_listed"
    assert "diagnostics" in result["parsed"]


# ------------------------------------------------------------------ extraction states

def test_extract_ok_with_page_numbers():
    result = extract.extract_pdf(pdf_bytes(pages=3, text="alpha beta gamma delta epsilon zeta eta theta iota"))
    assert result["state"] == "ok" and result["page_count"] == 3
    assert [p["page"] for p in result["pages"]] == [1, 2, 3]
    assert "page 2" in result["pages"][1]["text"]


def test_extract_malformed_and_empty():
    assert extract.extract_pdf(b"%PDF-1.4 this is not a pdf")["state"] == "malformed_pdf"
    assert extract.extract_pdf(b"")["state"] == "malformed_pdf"
    assert extract.extract_pdf(b"<html>hello</html>")["state"] == "malformed_pdf"


def test_extract_truncated_pdf_does_not_crash():
    data = pdf_bytes(pages=6)
    result = extract.extract_pdf(data[: len(data) // 2])
    assert result["state"] in ("ok", "partial", "malformed_pdf")


def test_extract_encrypted():
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "secret annual report text that is long enough to count")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="user", owner_pw="owner")
    assert extract.extract_pdf(data)["state"] == "encrypted"


def test_extract_scanned_pages_have_no_text():
    import pymupdf
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200))
    pix.clear_with(200)
    for _ in range(3):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix)
    result = extract.extract_pdf(doc.tobytes())
    assert result["state"] == "scanned_no_text" and result["page_count"] == 3


def test_extract_page_cap_is_partial():
    result = extract.extract_pdf(pdf_bytes(pages=5), max_pages=2)
    assert result["state"] == "partial" and result["partial_reason"] == "page_cap" and result["pages_extracted"] == 2


def test_extract_time_cap():
    result = extract.extract_pdf(pdf_bytes(pages=5), budget_s=-1)
    assert result["state"] == "too_slow" and result["pages_extracted"] == 0


# ------------------------------------------------------------------ section checks

PASSAGE = [{"source_id": "S1", "page": 7, "doc": "annual_report", "char_start": 0,
            "text": "Consolidated Statement of Profit and Loss\n(Amount in ` million)\nFor the year ended 31 March 2026\n"
                    "Revenue from operations\n35\n 64,549.31 \n 58,000.10"}]


def item(**changes):
    base = {"metric": "Revenue from operations", "value": "64,549.31", "unit": "₹ million",
            "period": "Year ended 31 March 2026", "scope": "consolidated", "source": "S1",
            "quote": "Revenue from operations\n35\n 64,549.31"}
    base.update(changes)
    return {"items": [base]}


def problems_for(**changes):
    return sections.check_number_items(item(**changes), PASSAGE)[0]


def test_number_item_passes_when_everything_is_in_the_passage():
    assert problems_for()["auto_pass"]


@pytest.mark.parametrize("changes,problem", [
    ({"value": "99,999.99"}, "value_not_in_quote"),
    ({"quote": "Revenue from operations was huge"}, "quote_not_verbatim"),
    ({"source": "S9"}, "source_missing"),
    ({"unit": "₹ crore"}, "unit_not_in_passage"),
    ({"unit": "units"}, "unit_unrecognised"),
    ({"period": "Year ended 31 March 2024"}, "period_year_not_in_passage"),
    ({"scope": "standalone"}, "scope_not_in_passage"),
    ({"scope": "group"}, "scope_unrecognised"),
])
def test_number_item_failures(changes, problem):
    row = problems_for(**changes)
    assert not row["auto_pass"] and problem in row["problems"]


def test_number_item_scope_not_stated_is_reported_not_invented():
    row = problems_for(scope="not stated")
    assert row["auto_pass"] and row["scope_stated"] is False


def test_text_claim_check_rejects_invented_quote_and_number():
    parsed = {"claims": [{"text": "Growth of 40%", "source": "S1", "quote": "management guided 40% growth"}]}
    row = sections.check_text_claims(parsed, PASSAGE)[0]
    assert not row["auto_pass"]


# ------------------------------------------------------------------ passage selection on the saved KPIT text

def kpit_pages():
    return snapshot.load_pages("KPIT_AR_2026"), snapshot.load_pages("KPIT_CONCALL_AUG2026")


def test_numbers_passages_pick_income_statement_pages():
    ar, _ = kpit_pages()
    passages = sections.numbers_passages(ar)
    assert [p["page"] for p in passages] == [149, 107]
    assert all("revenue from operations" in p["text"].lower() for p in passages)


def test_outlook_passages_use_management_turns():
    _, call = kpit_pages()
    passages, method = sections.outlook_passages(call)
    assert method == "management_turns"
    assert all(p["role"] == "management" for p in passages)


def test_outlook_falls_back_to_windows_without_speaker_labels():
    pages = [{"page": i + 1, "text": "We expect growth guidance for the full year to improve margin " * 20} for i in range(8)]
    passages, method = sections.outlook_passages(pages)
    assert method == "windows_speakers_not_detected" and len(passages) > 0


def test_outlook_without_any_document():
    assert sections.outlook_passages(None) == ([], "no_document")


# ------------------------------------------------------------------ whole flow with fakes

@pytest.fixture(scope="module")
def kpit_pdfs():
    ar, call = kpit_pages()
    wanted = {4, 56, 107, 110, 149, 152}
    small_ar = [p for p in ar if p["page"] in wanted]
    # keep page numbers meaningful: pad so that statement pages stay where they are
    padded = []
    by_page = {p["page"]: p for p in small_ar}
    for number in range(1, 153):
        padded.append(by_page.get(number, {"page": number, "text": "Filler page " + str(number) + " about nothing in particular."}))
    return {"ar": fk.make_pdf_from_pages(padded), "call": fk.make_pdf_from_pages(call)}


class Cfg:
    model = "fake-model"
    api_key = "x"
    base_url = "https://example.invalid/v1"
    max_tokens = 700

    def missing(self):
        return []


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("FINN_CACHE_DIR", str(tmp_path / "cache"))


def base_routes(kpit_pdfs, **overrides):
    routes = {SCREENER: fk.FakeResponse(200, fixture_html().encode()),
              "https://www.bseindia.com/": fk.FakeResponse(200, b"<html></html>"),
              AR_URL: fk.FakeResponse(200, kpit_pdfs["ar"]),
              CALL_URL: fk.FakeResponse(200, kpit_pdfs["call"])}
    routes.update(overrides)
    return routes


def run_flow(routes, mode="good", calls=None, symbol="KPITTECH"):
    session = fk.FakeSession(routes)
    out = pipeline.analyze(symbol, Cfg(), session=session, resolver=fk.public_resolver,
                           chat_fn=fk.make_fake_chat(mode, calls), pacer=pacer.TokenPacer(10 ** 9))
    return out, session


def statuses(brief):
    return {name: section["status"] for name, section in brief["sections"].items()}


def test_flow_good_run_has_full_citations(kpit_pdfs):
    out, _ = run_flow(base_routes(kpit_pdfs))
    brief = out["brief"]
    assert statuses(brief) == {"outlook": "ok", "overview": "ok", "numbers": "ok"}
    assert brief["sections"]["outlook"]["note"] == "passages chosen by: management_turns"
    assert brief["answer_source"] == "live_model"
    assert brief["documents"]["annual_report"]["fetch"]["source"] == "fetched_now"
    item_ = brief["sections"]["numbers"]["items"][0]
    cite = item_["citation"]
    assert item_["value"] == "64,549.31" and item_["scope"] == "consolidated"
    assert cite["pdf_page"] == 149 and cite["url"] == AR_URL
    assert cite["doc_title"].startswith("KPIT Technologies Ltd - Annual Report 2026")
    assert cite["excerpt"] and cite["doc_date"]
    claim_cite = brief["sections"]["outlook"]["claims"][0]["citation"]
    assert claim_cite["url"] == CALL_URL and "August" in claim_cite["doc_date"]
    assert brief["documents"]["concall"]["date_label"] == "Aug 2026"
    assert any("Oct 2026" in n for n in brief["notes"])


def test_flow_second_run_is_cached_and_makes_no_requests(kpit_pdfs):
    run_flow(base_routes(kpit_pdfs))
    calls = []
    out, session = run_flow(base_routes(kpit_pdfs), calls=calls)
    assert out["brief"]["answer_source"] == "cached_brief"
    assert session.requests == [] and calls == []
    assert out["brief"]["documents"]["annual_report"]["fetch"]["source"] == "cached_with_brief"
    assert pipeline.peek_cached_brief("KPITTECH", Cfg()) is not None


def test_flow_documents_come_from_disk_cache_when_brief_cache_is_gone(kpit_pdfs, tmp_path):
    run_flow(base_routes(kpit_pdfs))
    for name in os.listdir(str(tmp_path / "cache" / "brief")):
        os.remove(str(tmp_path / "cache" / "brief" / name))
    out, session = run_flow(base_routes(kpit_pdfs))
    brief = out["brief"]
    assert brief["answer_source"] == "live_model"
    assert brief["documents"]["annual_report"]["fetch"]["source"] == "disk_cache"
    assert AR_URL not in session.requests


def test_flow_hallucinated_model_output_is_not_shown_as_findings(kpit_pdfs):
    out, _ = run_flow(base_routes(kpit_pdfs), mode="hallucinate")
    brief = out["brief"]
    assert statuses(brief) == {"outlook": "unverified", "overview": "unverified", "numbers": "unverified"}
    for section in brief["sections"].values():
        assert section["claims"] == [] and section.get("items", []) == [] and len(section["failed_checks"]) == 1


def test_flow_model_says_not_stated(kpit_pdfs):
    out, _ = run_flow(base_routes(kpit_pdfs), mode="not_stated")
    assert set(statuses(out["brief"]).values()) == {"not_found"}


def test_flow_model_failure_is_unavailable_and_not_cached(kpit_pdfs):
    out, _ = run_flow(base_routes(kpit_pdfs), mode="fail")
    assert set(statuses(out["brief"]).values()) == {"unavailable"}
    assert pipeline.peek_cached_brief("KPITTECH", Cfg()) is None


def test_flow_model_bad_json(kpit_pdfs):
    out, _ = run_flow(base_routes(kpit_pdfs), mode="bad_json")
    assert set(statuses(out["brief"]).values()) == {"unavailable"}
    assert out["brief"]["sections"]["numbers"]["reason"] == "model_bad_json"


def test_flow_missing_transcript_says_not_found(kpit_pdfs):
    routes = base_routes(kpit_pdfs)
    routes[CALL_URL] = fk.FakeResponse(404)
    routes["https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=70c85b6c-58e6-423f-9b32-50b17a948674.pdf"] = fk.FakeResponse(404)
    out, _ = run_flow(routes)
    brief = out["brief"]
    assert brief["sections"]["outlook"]["status"] == "not_found"
    assert "not_found" in brief["sections"]["outlook"]["reason"]
    assert brief["documents"]["concall"]["status"] == "not_available"
    assert brief["sections"]["numbers"]["status"] == "ok"


def test_flow_newest_annual_report_missing_uses_older_and_says_so(kpit_pdfs):
    routes = base_routes(kpit_pdfs)
    routes[AR_URL] = fk.FakeResponse(404)
    routes[AR_OLD_URL] = fk.FakeResponse(200, kpit_pdfs["ar"])
    out, _ = run_flow(routes)
    doc = out["brief"]["documents"]["annual_report"]
    assert doc["label"] == "Annual Report 2025" and doc["skipped"][0]["state"] == "not_found"
    assert any("older one was used" in text for text in out["brief"]["limitations"])


def test_flow_scanned_annual_report(kpit_pdfs):
    import pymupdf
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100))
    pix.clear_with(255)
    for _ in range(3):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix)
    scanned = doc.tobytes()
    routes = base_routes(kpit_pdfs)
    routes[AR_URL] = fk.FakeResponse(200, scanned)
    routes[AR_OLD_URL] = fk.FakeResponse(200, scanned)
    out, _ = run_flow(routes)
    brief = out["brief"]
    assert brief["documents"]["annual_report"]["status"] == "not_available"
    assert "scanned_no_text" in brief["documents"]["annual_report"]["reason"]
    assert brief["sections"]["numbers"]["status"] == "not_found"


def test_flow_malformed_and_html_pdfs(kpit_pdfs):
    routes = base_routes(kpit_pdfs)
    routes[AR_URL] = fk.FakeResponse(200, b"%PDF-1.4 broken")
    routes[AR_OLD_URL] = fk.FakeResponse(200, b"<html>blocked</html>")
    out, _ = run_flow(routes)
    reason = out["brief"]["documents"]["annual_report"]["reason"]
    assert "malformed_pdf" in reason and "not_pdf" in reason


def test_flow_oversized_pdf(kpit_pdfs, monkeypatch):
    monkeypatch.setenv("FINN_RESEARCH_MAX_BYTES", "1000")
    out, _ = run_flow(base_routes(kpit_pdfs))
    assert "too_large" in out["brief"]["documents"]["annual_report"]["reason"]


def test_flow_untrusted_transcript_host_is_reported(kpit_pdfs):
    html = fixture_html().replace("https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d07f9c52-5c5a-443b-b239-6acd5e94b97a.pdf",
                                  "https://www.kpit.com/transcript.pdf").replace(
        "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=70c85b6c-58e6-423f-9b32-50b17a948674.pdf",
        "https://www.kpit.com/transcript2.pdf")
    routes = base_routes(kpit_pdfs)
    routes[SCREENER] = fk.FakeResponse(200, html.encode())
    out, session = run_flow(routes)
    brief = out["brief"]
    assert brief["documents"]["concall"]["status"] == "not_available"
    assert "host_not_trusted" in " ".join(brief["notes"])
    assert not any("kpit.com" in url for url in session.requests)


def test_flow_discovery_failures_abort_with_clear_codes(kpit_pdfs):
    out, _ = run_flow({SCREENER: fk.FakeResponse(404)})
    assert out["ok"] is False and out["error"]["code"] == "ticker_not_found"
    out, _ = run_flow({})
    assert out["error"]["code"] == "ticker_not_found"  # the fake returns 404 for unknown routes


def test_flow_only_trusted_hosts_are_contacted(kpit_pdfs):
    _, session = run_flow(base_routes(kpit_pdfs))
    for url in session.requests:
        assert safe_fetch.check_url(url)[0], url


@pytest.mark.skipif(not os.path.exists("/mnt/user-data/uploads/nikhil/enam assg/data/pdfs/KPIT_AR_2026.pdf"),
                    reason="real KPIT PDFs not on this machine")
def test_flow_with_real_kpit_pdfs():
    base = "/mnt/user-data/uploads/nikhil/enam assg/data/pdfs/"
    routes = {SCREENER: fk.FakeResponse(200, fixture_html().encode()),
              "https://www.bseindia.com/": fk.FakeResponse(200, b"<html></html>"),
              AR_URL: fk.FakeResponse(200, open(base + "KPIT_AR_2026.pdf", "rb").read()),
              CALL_URL: fk.FakeResponse(200, open(base + "KPIT_CONCALL_AUG2026.pdf", "rb").read())}
    out, _ = run_flow(routes)
    brief = out["brief"]
    assert statuses(brief) == {"outlook": "ok", "overview": "ok", "numbers": "ok"}
    assert brief["sections"]["numbers"]["items"][0]["citation"]["pdf_page"] == 149


# ------------------------------------------------------------------ pacing and limits

def test_pacer_waits_when_minute_budget_is_used():
    clock = {"t": 1000.0}
    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] = clock["t"] + seconds

    p = pacer.TokenPacer(limit_per_minute=5000)
    p.record(4000, now=lambda: clock["t"])
    waited = p.wait_for(2000, sleep=fake_sleep, now=lambda: clock["t"])
    assert waited > 55 and len(slept) == 1


def test_pacer_does_not_wait_when_budget_is_free():
    p = pacer.TokenPacer(limit_per_minute=5000)
    assert p.wait_for(2000) == 0


def test_sliding_limit_hour_and_day():
    limiter = jobs.SlidingLimit(per_hour=2, per_day=3)
    assert limiter.acquire(now=0)[0] and limiter.acquire(now=10)[0]
    assert limiter.acquire(now=20) == (False, "rate_limited_hourly")
    assert limiter.acquire(now=4000)[0]
    assert limiter.acquire(now=8000) == (False, "rate_limited_daily")
    assert limiter.acquire(now=90000)[0]


# ------------------------------------------------------------------ service (jobs) and HTTP API

def brief_stub(status="ok"):
    section = {"status": status, "claims": [], "items": [], "failed_checks": [], "passages": []}
    return {"ticker": "X", "answer_source": "live_model", "sections": {"overview": section, "outlook": section, "numbers": section},
            "documents": {}, "label": "stub"}


def wait_done(service, job_id, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        job = service.get(job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def make_service(runner, fallback=None, per_hour=100):
    return jobs.ResearchService(lambda: Cfg(), fallback_fn=fallback, runner=runner,
                                limiter=jobs.SlidingLimit(per_hour, 1000))


def test_service_runs_job_and_reports_stages():
    def runner(symbol, config, callback=None):
        callback([{"name": "discover", "status": "ok"}])
        return {"ok": True, "brief": brief_stub(), "stages": []}
    service = make_service(runner)
    job = service.submit("TCS")
    done = wait_done(service, job["job_id"])
    assert done["status"] == "done" and done["brief"]["answer_source"] == "live_model"


def test_service_one_at_a_time():
    gate = threading.Event()

    def runner(symbol, config, callback=None):
        gate.wait(5)
        return {"ok": True, "brief": brief_stub(), "stages": []}
    service = make_service(runner)
    first = service.submit("TCS")
    same = service.submit("TCS")
    assert same["job_id"] == first["job_id"]
    with pytest.raises(jobs.ServiceError) as caught:
        service.submit("INFY")
    assert caught.value.status == 429 and caught.value.code == "busy"
    gate.set()
    wait_done(service, first["job_id"])
    service.submit("INFY")  # allowed again


def test_service_rate_limit_without_fallback_is_429():
    def runner(symbol, config, callback=None):
        return {"ok": True, "brief": brief_stub(), "stages": []}
    service = make_service(runner, per_hour=1)
    wait_done(service, service.submit("TCS")["job_id"])
    with pytest.raises(jobs.ServiceError) as caught:
        service.submit("INFY")
    assert caught.value.status == 429 and caught.value.code == "rate_limited_hourly"


def test_service_failed_analysis_reports_error():
    def runner(symbol, config, callback=None):
        return {"ok": False, "error": {"code": "ticker_not_found", "message": "x"}, "stages": []}
    service = make_service(runner)
    done = wait_done(service, service.submit("ZZZZ")["job_id"])
    assert done["status"] == "failed" and done["error"]["code"] == "ticker_not_found"


def test_service_runner_crash_does_not_leak_details():
    def runner(symbol, config, callback=None):
        raise RuntimeError("secret path /etc/shadow")
    service = make_service(runner)
    done = wait_done(service, service.submit("TCS")["job_id"])
    assert done["status"] == "failed" and "shadow" not in json.dumps(done)


def test_service_snapshot_fallback_when_live_sections_empty():
    def runner(symbol, config, callback=None):
        return {"ok": True, "brief": brief_stub("not_found"), "stages": []}

    def fallback(symbol, reason):
        return {"ticker": symbol, "answer_source": "snapshot_fallback", "sections": {}, "fallback_reason": reason}
    service = make_service(runner, fallback)
    done = wait_done(service, service.submit("KPITTECH")["job_id"])
    assert done["brief"]["answer_source"] == "snapshot_fallback"
    assert done["brief"]["live_attempt"]["answer_source"] == "live_model"


def test_service_does_not_replace_partial_live_result_with_snapshot():
    def runner(symbol, config, callback=None):
        brief = brief_stub("not_found")
        brief["sections"]["numbers"] = dict(brief["sections"]["numbers"], status="ok")
        return {"ok": True, "brief": brief, "stages": []}
    service = make_service(runner, lambda s, r: {"answer_source": "snapshot_fallback", "sections": {}})
    done = wait_done(service, service.submit("KPITTECH")["job_id"])
    assert done["brief"]["answer_source"] == "live_model"


from fastapi.testclient import TestClient
from app import main


@pytest.fixture
def client(monkeypatch):
    return TestClient(main.app)


def use_service(monkeypatch, runner, per_hour=100, config=None):
    service = jobs.ResearchService(lambda: config or Cfg(), fallback_fn=main.snapshot_fallback, runner=runner,
                                   limiter=jobs.SlidingLimit(per_hour, 1000))
    monkeypatch.setattr(main, "RESEARCH", service)
    return service


def ok_runner(symbol, config, callback=None):
    return {"ok": True, "brief": brief_stub(), "stages": []}


@pytest.mark.parametrize("body", [
    {"ticker": ""}, {"ticker": "A B"}, {"ticker": "../x"}, {"ticker": "x" * 50}, {"ticker": 5}, {}, [], "TCS",
])
def test_api_rejects_bad_ticker_bodies(client, monkeypatch, body):
    use_service(monkeypatch, ok_runner)
    assert client.post("/api/analyze", json=body).status_code == 422


def test_api_rejects_any_url_field(client, monkeypatch):
    use_service(monkeypatch, ok_runner)
    response = client.post("/api/analyze", json={"ticker": "TCS", "url": "https://evil.com/x.pdf"})
    assert response.status_code == 422
    response = client.post("/api/analyze", json={"ticker": "https://evil.com/x.pdf"})
    assert response.status_code == 422


def test_api_happy_path_and_polling(client, monkeypatch):
    use_service(monkeypatch, ok_runner)
    response = client.post("/api/analyze", json={"ticker": "tcs"})
    assert response.status_code == 202 and response.json()["symbol"] == "TCS"
    job_id = response.json()["job_id"]
    for _ in range(100):
        body = client.get("/api/analyze/" + job_id).json()
        if body["status"] != "running":
            break
        time.sleep(0.02)
    assert body["status"] == "done" and "brief" in body


@pytest.mark.parametrize("job_id", ["nope", "../../etc/passwd", "0" * 12, "G" * 12, "a" * 40])
def test_api_unknown_job_ids(client, monkeypatch, job_id):
    use_service(monkeypatch, ok_runner)
    assert client.get("/api/analyze/" + job_id).status_code == 404


def test_api_rate_limited_other_ticker_gets_429_with_code(client, monkeypatch):
    use_service(monkeypatch, ok_runner, per_hour=0)
    response = client.post("/api/analyze", json={"ticker": "TCS"})
    assert response.status_code == 429 and response.json()["detail"]["code"] == "rate_limited_hourly"


def test_api_rate_limited_kpit_gets_labelled_snapshot(client, monkeypatch):
    use_service(monkeypatch, ok_runner, per_hour=0)
    response = client.post("/api/analyze", json={"ticker": "KPITTECH"})
    assert response.status_code == 202
    brief = response.json()["brief"]
    assert brief["answer_source"] == "snapshot_fallback"
    assert brief["fallback_reason"]["code"] == "rate_limited_hourly"
    assert "SAVED SNAPSHOT" in brief["label"] and len(brief["snapshot_answers"]) == 2


def test_api_model_not_configured(client, monkeypatch):
    class NoModel(Cfg):
        def missing(self):
            return ["FINN_LLM_API_KEY"]
    use_service(monkeypatch, ok_runner, config=NoModel())
    response = client.post("/api/analyze", json={"ticker": "TCS"})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "model_not_configured"
    kpit = client.post("/api/analyze", json={"ticker": "KPITTECH"})
    assert kpit.status_code == 202 and kpit.json()["brief"]["answer_source"] == "snapshot_fallback"


def test_api_cached_brief_does_not_use_budget(client, monkeypatch, kpit_pdfs):
    service = use_service(monkeypatch, ok_runner, per_hour=0)
    cache.save("discovery", cache.key_for("discovery|KPITTECH"),
               dict(discover.discover("KPITTECH", session=fk.FakeSession({SCREENER: fk.FakeResponse(200, fixture_html().encode())}),
                                      resolver=fk.public_resolver), discovery_source="fetched_now"))
    info = cache.load("discovery", cache.key_for("discovery|KPITTECH"))
    cache.save("brief", pipeline.brief_key("KPITTECH", info, Cfg().model), brief_stub())
    response = client.post("/api/analyze", json={"ticker": "KPITTECH"})
    assert response.status_code == 202
    assert response.json()["brief"]["answer_source"] == "cached_brief"


def test_existing_routes_still_work(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/questions").status_code == 200
    assert client.get("/admin/host-check/abc").status_code == 404
