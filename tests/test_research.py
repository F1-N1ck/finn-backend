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
    assert [p["page"] for p in passages] == [149]  # consolidated page only; it already shows the profit line
    assert "revenue from operations" in passages[0]["text"].lower()


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
                           chat_fn=fk.make_fake_chat(mode, calls), pacer=pacer.TokenPacer(10 ** 9, 0))
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
    assert claim_cite["url"] == CALL_URL and "July 29, 2026" in claim_cite["doc_date"]  # call date, not the covering letter's August date
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
                                  "https://files.unrelated-host.example/transcript.pdf").replace(
        "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=70c85b6c-58e6-423f-9b32-50b17a948674.pdf",
        "https://files.unrelated-host.example/transcript2.pdf")
    routes = base_routes(kpit_pdfs)
    routes[SCREENER] = fk.FakeResponse(200, html.encode())
    out, session = run_flow(routes)
    brief = out["brief"]
    assert brief["documents"]["concall"]["status"] == "not_available"
    assert "host_not_trusted" in " ".join(brief["notes"])
    assert not any("unrelated-host" in url for url in session.requests)


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


# ------------------------------------------------------------------ reliability fixes (this round)

def test_page_of_quote_uses_page_marks_not_passage_start():
    from finn.research import pipeline
    passage = {"page": 11, "text": "First page text here. " + "x " * 5 + "Second page text with the quoted words.",
               "page_marks": [(0, 11), (30, 12)]}
    page, basis = pipeline.page_of_quote(passage, "the quoted words")
    assert page == 12 and basis == "located_in_passage"
    page, basis = pipeline.page_of_quote(passage, "First page text")
    assert page == 11
    page, basis = pipeline.page_of_quote(passage, "words that are not there")
    assert page == 11 and basis == "passage_page"
    page, basis = pipeline.page_of_quote({"page": 5, "text": "abc"}, "abc")
    assert page == 5 and basis == "passage_page"


def test_watchdog_marks_stuck_job_failed_and_frees_the_slot():
    gate = threading.Event()

    def runner(symbol, config, callback=None):
        gate.wait(5)
        return {"ok": True, "brief": brief_stub(), "stages": []}
    service = jobs.ResearchService(lambda: Cfg(), runner=runner, limiter=jobs.SlidingLimit(100, 1000),
                                   hard_limit_s=0.2)
    job = service.submit("TCS")
    time.sleep(0.6)
    stopped = service.get(job["job_id"])
    assert stopped["status"] == "failed" and stopped["error"]["code"] == "job_timeout"
    assert stopped["outcome"] == "failed"
    service.submit("INFY")  # slot is free again
    gate.set()
    time.sleep(0.2)
    assert service.get(job["job_id"])["status"] == "failed"  # the late result is dropped


def test_public_job_carries_instance_and_outcome():
    def runner(symbol, config, callback=None):
        brief = brief_stub()
        brief["outcome"] = "partial"
        return {"ok": True, "brief": brief, "stages": []}
    service = make_service(runner)
    done = wait_done(service, service.submit("TCS")["job_id"])
    assert done["instance"] == jobs.INSTANCE and done["outcome"] == "partial"


def test_plain_h_is_not_rewritten_when_font_is_not_a_rupee_font():
    import pymupdf
    from finn.research import extract
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((20, 40), "H", fontsize=10)
    page.insert_text((60, 40), "crores", fontsize=10)
    # the built-in font is not a rupee font, so nothing may be replaced here
    assert extract.page_text(doc[0]).split()[0] == "H"


def test_year_alias_accepts_short_and_long_year():
    from finn import grounded_qa
    assert grounded_qa.year_alias_in("2027", {"27"})
    assert grounded_qa.year_alias_in("27", {"2027"})
    assert not grounded_qa.year_alias_in("2028", {"27"})


def test_script_survives_poll_without_status_and_saves_after_each_company(tmp_path, monkeypatch):
    import importlib.util
    import http.server
    import json as _json
    spec = importlib.util.spec_from_file_location("run_tests", os.path.join(os.path.dirname(__file__), "..", "scripts", "run_research_tests.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = {"polls": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, payload, raw=None):
            data = raw if raw is not None else _json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._send(202, {"job_id": "abcdef012345", "status": "running", "instance": "i1"})

        def do_GET(self):
            if self.path.startswith("/healthz"):  # the script's background ping is not a poll
                self._send(200, {"ok": True})
                return
            state["polls"] += 1
            if state["polls"] == 1:
                self._send(502, None, raw=b"<html>Bad gateway gsk_SECRET123</html>")
            elif state["polls"] == 2:
                self._send(200, {"detail": {"code": "weird"}})
            else:
                self._send(200, {"job_id": "abcdef012345", "status": "failed", "outcome": "failed", "instance": "i1",
                                 "stages": [], "elapsed_s": 1.0, "error": {"code": "job_timeout", "message": "x"}})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_address[1])
    saved = []
    record, brief = module.run_one(base, "COFORGE", 30, 0.05, lambda: saved.append(1))
    server.shutdown()
    assert record["outcome"] == "failed" and brief is None
    unexpected = [p for p in record["polls"] if p.get("unexpected")]
    assert len(unexpected) == 2
    assert unexpected[0]["http"] == 502 and "gsk_SECRET123" not in unexpected[0]["body_redacted"]
    assert unexpected[1]["json_keys"] == ["detail"]
    assert len(saved) >= 3


def test_old_transcript_is_not_used_as_outlook():
    parsed = discover.parse_screener(fixture_html(), SCREENER)
    # pretend today is Nov 2028: the Aug 2026 call is 27 months old
    selected = discover.select_documents(parsed, today=time.struct_time((2028, 11, 2, 0, 0, 0, 0, 0, 0)))
    assert selected["concall_candidates"] == []
    assert any(n.startswith("Skipped as too old") for n in selected["notes"])
    reason = pipeline._missing_reason("concall", selected, [])
    assert "no recent transcript" in reason
    fresh = discover.select_documents(parsed, today=time.struct_time((2026, 11, 2, 0, 0, 0, 0, 0, 0)))
    assert fresh["concall_candidates"][0]["label"] == "Aug 2026"


def test_model_rate_limit_waits_once_then_succeeds(monkeypatch):
    from finn import llm_adapter
    slept = []
    monkeypatch.setattr(pipeline, "SLEEP", lambda s: slept.append(s))
    state = {"n": 0}

    def chat(config, messages):
        state["n"] += 1
        if state["n"] == 1:
            raise llm_adapter.LLMRateLimited("Rate limited: tpm", status=429, retry_after=12.0)
        return {"text": '{"claims": []}', "usage": {"total_tokens": 100}, "latency_s": 0.1}
    parsed, meta = pipeline.call_model(Cfg(), [{"role": "user", "content": "x"}], 100, pacer.TokenPacer(10 ** 9, 0), chat,
                                       max_wait_s=60)
    assert state["n"] == 2 and slept == [13.0] and meta["paced_wait_s"] == 13.0


def test_model_rate_limit_too_long_is_reported_with_provider_detail():
    from finn import llm_adapter

    def chat(config, messages):
        raise llm_adapter.LLMRateLimited("Rate limited: daily tokens used", status=429, retry_after=3000.0)
    tracker = pipeline.Tracker(None)
    section = pipeline.run_section("model:x", Cfg(), [{"role": "user", "content": "x"}], 100,
                                   pacer.TokenPacer(10 ** 9, 0), chat, lambda p: {"status": "ok"}, tracker)
    assert section["status"] == "unavailable" and section["reason"] == "model_rate_limited"
    assert "daily tokens" in section["provider_detail"] and section["retry_after_s"] == 3000.0


# ------------------------------------------------------------------ round 4 fixes

def test_eps_value_has_no_duplicate_rupee_sign():
    passage = {"source_id": "S1", "doc": "annual_report", "page": 186,
               "text": "Basic (in ₹) ₹64.01 ₹45.32 For Continuing and Discontinued operations consolidated 2026"}
    parsed = {"items": [{"metric": "Basic earnings per share", "value": "₹64.01", "unit": "₹",
                         "period": "2026", "scope": "consolidated", "source": "S1", "quote": "Basic (in ₹) ₹64.01"}]}
    rows = sections.check_number_items(parsed, [passage])
    assert rows[0]["auto_pass"]
    assert rows[0]["value"] == "64.01" and rows[0]["unit"] == "₹ per share" and rows[0]["value_raw"] == "₹64.01"


def test_plain_value_and_crore_unit_are_left_alone():
    row = {"value": "64,468", "unit": "₹ crores"}
    sections.tidy_value_and_unit(row, per_share=False)
    assert row["value"] == "64,468" and row["unit"] == "₹ crores" and "value_raw" not in row


def test_label_matches_text():
    assert pipeline.label_matches_text("May 2026", "7th May, 2026") is True
    assert pipeline.label_matches_text("Aug 2026", "7th May, 2026") is False
    assert pipeline.label_matches_text("Aug 2026", None) is None
    assert pipeline.label_matches_text(None, "7th May, 2026") is None


def realistic_transcript_pages(call_date="7th May, 2026"):
    """A transcript-shaped fake: title page with the call date, then moderator/management turns, > 8000 characters."""
    turns = []
    for i in range(40):
        who = "Moderator" if i % 2 == 0 else "Management"
        turns.append(who + ": " + ("Thank you, the next question is from the line of an analyst. " if who == "Moderator"
                                    else "We expect demand to remain healthy and margins to hold in the coming quarters. ") * 3)
    return [{"page": 1, "text": "X Ltd Q4 FY26 Earnings Conference Call " + call_date + " Moderator: Management:"},
            {"page": 2, "text": "\n".join(turns[:20])}, {"page": 3, "text": "\n".join(turns[20:])}]


def test_slow_newest_transcript_is_retried_before_falling_back(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "SLEEP", lambda s: None)
    monkeypatch.setattr(pipeline, "warm_up_bse", lambda *a, **k: None)
    pdf = fk.make_pdf_from_pages(realistic_transcript_pages())
    calls = {"n": 0}

    def fake_fetch(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "state": "timeout", "seconds": 20.6}
        return {"ok": True, "data": pdf, "bytes": len(pdf), "seconds": 3.0}
    monkeypatch.setattr(safe_fetch, "fetch", fake_fetch)
    monkeypatch.setenv("FINN_CACHE_DIR", str(tmp_path))
    tracker = pipeline.Tracker(None)
    entry = {"label": "Aug 2026", "url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=retrytest.pdf"}
    doc, skipped = pipeline.get_document("concall", [entry], "X Ltd", None, None, tracker, pipeline.limits())
    assert doc is not None and skipped == [] and calls["n"] == 2
    assert doc["record"]["label"] == "Aug 2026"


def test_persistent_timeout_falls_back_to_older_call_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "SLEEP", lambda s: None)
    monkeypatch.setattr(pipeline, "warm_up_bse", lambda *a, **k: None)
    pdf = fk.make_pdf_from_pages(realistic_transcript_pages())

    def fake_fetch(url, **kwargs):
        if "newest" in url:
            return {"ok": False, "state": "timeout", "seconds": 20.0}
        return {"ok": True, "data": pdf, "bytes": len(pdf), "seconds": 1.0}
    monkeypatch.setattr(safe_fetch, "fetch", fake_fetch)
    monkeypatch.setenv("FINN_CACHE_DIR", str(tmp_path))
    entries = [{"label": "Aug 2026", "url": "https://www.bseindia.com/x?Pname=newest.pdf"},
               {"label": "May 2026", "url": "https://www.bseindia.com/x?Pname=older.pdf"}]
    doc, skipped = pipeline.get_document("concall", entries, "X Ltd", None, None, pipeline.Tracker(None), pipeline.limits())
    assert doc["record"]["label"] == "May 2026"
    assert skipped[0]["label"] == "Aug 2026" and skipped[0]["tries"] == 2 and skipped[0]["state"] == "timeout"


def test_flow_outlook_says_when_it_uses_an_older_call(kpit_pdfs):
    # newest listed transcript is unreachable, the next one works: the outlook section must say so
    routes = base_routes(kpit_pdfs)
    older = "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=70c85b6c-58e6-423f-9b32-50b17a948674.pdf"
    routes[older] = fk.FakeResponse(200, kpit_pdfs["call"])
    routes[CALL_URL] = fk.FakeResponse(404)
    out, _ = run_flow(routes)
    section = out["brief"]["sections"]["outlook"]
    doc = out["brief"]["documents"]["concall"]
    assert doc["status"] == "ok" and doc["skipped"][0]["state"] == "not_found"
    assert "could not be used" in (section.get("note") or "")


# ------------------------------------------------------------------ retrieval fixes (statement under balance sheet, ligatures, overview)

def test_ligatures_are_spelled_out():
    assert extract.fix_ligatures("Net Proﬁt for the year, ﬂow, oﬀer") == "Net Profit for the year, flow, offer"


def test_income_statement_is_found_without_a_heading_and_below_a_balance_sheet():
    balance = "Balance Sheet\n₹ lakhs\n" + "Property, plant and equipment 12,707.51\n" * 60
    statement = ("INCOME\nRevenue from operations 21 3,75,742.37\nOther income 22 18,397.69\nTotal income 3,94,140.06\n"
                 "Total expenses 3,02,060.27\nProfit before tax 82,510.91\nTax expense 27 19,667.88\n"
                 "Net Profit for the year 62,843.03\nOther comprehensive income 649.32\nEarnings per equity share (in ₹) Basic 100.89\n")
    page = {"page": 119, "text": balance + statement}
    assert sections._statement_score(page["text"]) >= 5
    passages = sections.numbers_passages([{"page": 1, "text": "Cover page"}, page], max_chars=1200)
    assert len(passages) == 1 and passages[0]["page"] == 119
    assert "Net Profit for the year" in passages[0]["text"] and passages[0]["char_start"] > 0


def test_notes_and_esg_pages_do_not_look_like_an_income_statement():
    notes = "Notes forming part of the financial statements\nRevenue from operations is recognised when control passes."
    assert sections._statement_score(notes) < 5


def test_overview_prefers_about_page_and_corporate_information_note_over_csr_and_directory_pages():
    filler = "Quarterly results were discussed by the board. " * 30
    csr = ("Our CSR programmes and sustainability targets. Material topics: biodiversity, SDGs, waste management, ESG. "
           "Lives impacted through CSR. our business segments. our business is engaged in many programmes. " * 6)
    directory = ("Corporate Information Registered Office Bengaluru Auditors Bankers Registrar Company Secretary "
                 "Key Managerial Personnel Audit Committee " * 4)
    about = ("About Acme\nAcme Limited is a global engineering services company established in 1989. "
             "It provides design and software services to automotive customers. " * 3)
    note = ("Notes forming part of the financial statements\n1. CORPORATE INFORMATION\nAcme Limited is a public limited "
            "company incorporated in India. The Company provides product design and engineering services. " * 2)
    pages = [{"page": 1, "text": filler}, {"page": 4, "text": directory}, {"page": 8, "text": about},
             {"page": 33, "text": csr}, {"page": 122, "text": note}]
    picked = [p["page"] for p in sections.overview_passages(pages)]
    assert picked[:2] == [8, 122] or picked[:2] == [122, 8]
    assert 33 not in picked[:2] and 4 not in picked[:2]
    assert all("  " not in p["text"] and "\n" not in p["text"] for p in sections.overview_passages(pages))


# ------------------------------------------------------------------ round 6: call date, real transcript check, rupee pre-check

def test_find_call_date_uses_the_call_page_not_the_covering_letter():
    pages = [{"page": 1, "text": "Dear Sir/Madam, Sub: Transcript of earnings call. Date: October 19, 2026 BSE Limited"},
             {"page": 2, "text": "Page 1 of 19 Earnings Conference Call October 13, 2026 Moderator: Management:"}]
    assert pipeline.find_call_date(pages) == "October 13, 2026"
    hul = [{"page": 1, "text": "Hindustan Unilever Q4 Earnings Call April 30th, 2026 Moderator: Management:"}]
    assert pipeline.find_call_date(hul) == "April 30th, 2026"
    assert pipeline.find_call_date([{"page": 1, "text": "Dear Sir, Sub: audio recording August 4, 2026"}]) is None


def test_label_matches_text_allows_one_month_difference():
    assert pipeline.label_matches_text("Aug 2026", "July 29, 2026") is True
    assert pipeline.label_matches_text("Oct 2026", "May 7, 2026") is False


def test_one_page_audio_notice_is_not_a_transcript():
    notice = [{"page": 1, "text": "Dear Sir, Sub: Intimation of audio recording of earnings call. Link: https://example.com/audio"}]
    ok, info = pipeline.looks_like_transcript(notice)
    assert ok is False and info["pages"] == 1
    assert pipeline.looks_like_transcript(realistic_transcript_pages())[0] is True


def test_not_a_transcript_reason_is_plain():
    reason = pipeline._missing_reason("concall", {"concall_candidates": [1], "annual_report_candidates": [], "notes": []},
                                      [{"state": "not_a_transcript"}])
    assert "not a transcript" in reason


def test_get_document_skips_an_audio_notice_and_records_state(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "warm_up_bse", lambda *a, **k: None)
    notice = fk.make_pdf_from_pages([{"page": 1, "text": "Dear Sir, Sub: audio recording of earnings call"}])
    monkeypatch.setattr(safe_fetch, "fetch", lambda url, **kw: {"ok": True, "data": notice, "bytes": len(notice), "seconds": 1.0})
    monkeypatch.setenv("FINN_CACHE_DIR", str(tmp_path))
    entry = {"label": "Feb 2026", "url": "https://www.bseindia.com/x?Pname=audio.pdf"}
    doc, skipped = pipeline.get_document("concall", [entry], "X Ltd", None, None, pipeline.Tracker(None), pipeline.limits())
    assert doc is None and skipped[0]["state"] == "not_a_transcript"


def test_rupee_candidate_precheck():
    from finn.research import extract
    assert extract.RUPEE_CANDIDATE_RE.search("Revenue H 1,234 crores")
    assert extract.RUPEE_CANDIDATE_RE.search("EPS `64.01")
    assert not extract.RUPEE_CANDIDATE_RE.search("Hindustan Unilever has a heritage in India")


# ------------------------------------------------------------------ round 7: company-hosted transcripts, host rules, no summaries

from finn.research import hosts  # noqa: E402

KPIT_COMPANY_CALL = "https://www.kpit.com/wp-content/uploads/2026/08/Q1-FY27-transcript.pdf"


def company_html(call_url=KPIT_COMPANY_CALL, website_text="kpit.com", website_href="http://www.kpit.com"):
    html = fixture_html().replace(CALL_URL, call_url)
    return html.replace('<a href="http://www.kpit.com" target="_blank">kpit.com</a>',
                        '<a href="' + website_href + '" target="_blank">' + website_text + '</a>')


@pytest.mark.parametrize("host,domain", [
    ("astralltd.com", "astralltd.com"), ("www.coforge.com", "coforge.com"), ("investors.coforge.com", "investors.coforge.com"),
    ("d1y69b020rytqm.cloudfront.net", None), ("bucket.s3.amazonaws.com", None), ("co.in", None), ("10.0.0.1", None),
    ("localhost", None), ("x.github.io", None), ("bit.ly", None), ("a_b.example.com", None), ("", None),
])
def test_site_domain(host, domain):
    assert hosts.site_domain(host) == domain


@pytest.mark.parametrize("domain,name,expected", [
    ("astralltd.com", "Astral Ltd", True), ("kpit.com", "KPIT Technologies Ltd", True),
    ("tataelxsi.com", "Tata Elxsi Ltd", True), ("tatamotors.com", "Tata Elxsi Ltd", False),
    ("investors.coforge.com", "Coforge Ltd", True), ("twitter.com", "Astral Ltd", False),
    ("pidilite.com", "Pidilite Industries Ltd", True), ("abc.co.in", "ABC India Ltd", False),
])
def test_name_matches_domain(domain, name, expected):
    assert hosts.name_in_domain(domain, name) is expected


def make_policy(name, website, home_html=None, home_state="ok"):
    calls = {"n": 0}

    def fetch_homepage(domain):
        calls["n"] += 1
        return {"state": home_state, "html": home_html, "seconds": 0.4}
    return hosts.CompanyHostPolicy(name, website, fetch_homepage), calls


def test_policy_listed_website_and_subdomain_are_allowed():
    policy, calls = make_policy("Coforge Ltd", "https://www.coforge.com")
    assert policy.check("investors.coforge.com")["allowed"] is True
    assert policy.check("www.coforge.com")["basis"] == "listed_website"
    assert calls["n"] == 0  # no network needed


@pytest.mark.parametrize("host", ["coforge.com.evil.example", "notcoforge.com", "evil.example", "coforge.com@evil.example"])
def test_policy_lookalikes_are_refused(host):
    policy, _ = make_policy("Coforge Ltd", "https://www.coforge.com", home_html="<a href='https://evil.example/'>x</a>")
    assert policy.check(host)["allowed"] is False


def test_policy_second_domain_needs_link_and_name():
    home = "<a href='https://astralltd.com/'>About Astral Group</a><a href='https://twitter.com/astral'>t</a>"
    policy, calls = make_policy("Astral Ltd", "https://www.astralpipes.com", home_html=home)
    decision = policy.check("www.astralltd.com")
    assert decision["allowed"] and decision["basis"] == "listed_website_links_to_it" and decision["company_domain"] == "astralltd.com"
    assert policy.check("astralltd.com")["allowed"] and calls["n"] == 1  # the home page is read once
    assert policy.homepage["state"] == "ok"
    # name matches but the listed website never links there: refused
    policy2, _ = make_policy("Astral Ltd", "https://www.astralpipes.com", home_html="<a href='https://other.example/'>x</a>")
    refused = policy2.check("www.astralltd.com")
    assert refused["allowed"] is False and "does not link" in refused["reason"]
    # linked but the name does not match: refused without reading the home page
    policy3, calls3 = make_policy("Astral Ltd", "https://www.astralpipes.com", home_html="<a href='https://sisterco.com/'>x</a>")
    assert policy3.check("sisterco.com")["allowed"] is False and calls3["n"] == 0


def test_policy_shared_platform_is_refused_even_if_linked_and_named():
    home = "<a href='https://astral.cloudfront.net/x'>x</a>"
    policy, _ = make_policy("Astral Ltd", "https://www.astralpipes.com", home_html=home)
    assert policy.check("astral.cloudfront.net")["allowed"] is False


def test_policy_homepage_failure_is_reported():
    policy, _ = make_policy("Astral Ltd", "https://www.astralpipes.com", home_html=None, home_state="timeout")
    refused = policy.check("www.astralltd.com")
    assert refused["allowed"] is False and "timeout" in refused["reason"]


def test_policy_without_a_listed_website_refuses_everything():
    policy, _ = make_policy("Astral Ltd", None)
    assert policy.check("www.astralltd.com")["allowed"] is False
    assert "no usable company website" in policy.check("www.astralltd.com")["reason"]


def test_screener_website_needs_text_and_link_to_agree():
    parsed = discover.parse_screener(fixture_html(), SCREENER)
    assert parsed["website"] == "http://www.kpit.com"
    swapped = fixture_html().replace('<a href="http://www.kpit.com" target="_blank">kpit.com</a>',
                                     '<a href="http://www.evil.example" target="_blank">kpit.com</a>')
    assert discover.parse_screener(swapped, SCREENER)["website"] is None


def test_check_url_company_domain_boundaries():
    assert safe_fetch.check_url("https://investors.coforge.com/a.pdf", "coforge.com") == (True, None)
    assert safe_fetch.check_url("https://coforge.com/a.pdf", "coforge.com") == (True, None)
    assert safe_fetch.check_url("https://evilcoforge.com/a.pdf", "coforge.com") == (False, "host_not_trusted")
    assert safe_fetch.check_url("http://coforge.com/a.pdf", "coforge.com") == (False, "bad_url")
    assert safe_fetch.check_url("https://coforge.com:8443/a.pdf", "coforge.com") == (False, "bad_url")
    assert safe_fetch.check_url("https://coforge.com/a.pdf") == (False, "host_not_trusted")


def test_company_fetch_redirect_inside_domain_ok_outside_blocked():
    url = "https://www.coforge.com/a.pdf"
    apex = "https://coforge.com/a.pdf"
    routes = {url: fk.FakeResponse(302, b"", {"Location": apex}), apex: fk.FakeResponse(200, pdf_bytes())}
    result, _ = do_fetch(routes, url=url, company_domain="coforge.com")
    assert result["ok"]
    routes = {url: fk.FakeResponse(302, b"", {"Location": "https://evil.example/a.pdf"})}
    result, session = do_fetch(routes, url=url, company_domain="coforge.com")
    assert result["state"] == "host_not_trusted" and result["host"] == "evil.example"
    assert session.requests == [url]  # the redirect target was never requested


def test_company_fetch_refuses_private_addresses_on_every_hop():
    url = "https://www.coforge.com/a.pdf"
    routes = {url: fk.FakeResponse(302, b"", {"Location": "https://internal.coforge.com/a.pdf"})}

    def resolver(host):
        return ["10.1.2.3"] if host == "internal.coforge.com" else ["104.18.0.1"]
    result, session = do_fetch(routes, url=url, company_domain="coforge.com", resolver=resolver)
    assert result["state"] == "dns_not_public" and session.requests == [url]
    for bad in (["127.0.0.1"], ["169.254.169.254"], ["10.0.0.1"], ["192.168.1.1"], ["::1"], ["fd00::1"], ["100.64.0.1"],
                ["104.18.0.1", "10.0.0.2"], [], ["not-an-ip"]):
        result, session = do_fetch({url: fk.FakeResponse(200, pdf_bytes())}, url=url, company_domain="coforge.com",
                                   resolver=lambda host, bad=bad: bad)
        assert result["state"] == "dns_not_public" and session.requests == []


def test_company_fetch_keeps_size_and_type_limits():
    url = "https://www.coforge.com/a.pdf"
    result, _ = do_fetch({url: fk.FakeResponse(200, pdf_bytes(pages=30))}, url=url, company_domain="coforge.com", max_bytes=500)
    assert result["state"] == "too_large"
    result, _ = do_fetch({url: fk.FakeResponse(200, b"<html>login</html>", {"Content-Type": "text/html"})}, url=url,
                         company_domain="coforge.com")
    assert result["state"] == "not_pdf"


def test_connection_is_pinned_to_the_checked_public_address(monkeypatch):
    seen = {}

    def fake_create(address, timeout, source_address=None, socket_options=None):
        seen["address"] = address
        return "socket"
    monkeypatch.setattr(safe_fetch.urllib3_connection, "create_connection", fake_create)
    connection_class = type("C", (safe_fetch.PinnedHTTPSConnection,), {"resolver": staticmethod(lambda host: ["93.184.216.34"])})
    assert connection_class("www.coforge.com", 443)._new_conn() == "socket"
    assert seen["address"] == ("93.184.216.34", 443)
    private_class = type("P", (safe_fetch.PinnedHTTPSConnection,), {"resolver": staticmethod(lambda host: ["10.0.0.7"])})
    with pytest.raises(safe_fetch.NotPublicAddress):
        private_class("www.coforge.com", 443)._new_conn()
    session = safe_fetch.make_session(lambda host: ["10.0.0.7"])
    pool_class = session.get_adapter("https://www.coforge.com/").poolmanager.pool_classes_by_scheme["https"]
    assert issubclass(pool_class.ConnectionCls, safe_fetch.PinnedHTTPSConnection)


def test_select_documents_company_host_decisions():
    parsed = discover.parse_screener(company_html("https://files.unrelated-host.example/t.pdf"), SCREENER)
    policy = hosts.CompanyHostPolicy(parsed["company_name"], parsed["website"], None)
    chosen = discover.select_documents(parsed, today=time.strptime("2026-11-03", "%Y-%m-%d"), policy=policy)
    assert [c["label"] for c in chosen["concall_candidates"]] == ["May 2026"]
    assert chosen["concall_candidates"][0]["source"]["kind"] == "exchange"
    assert chosen["host_checks"][0]["allowed"] is False and chosen["host_checks"][0]["host"] == "files.unrelated-host.example"
    assert "host_not_trusted (files.unrelated-host.example)" in " ".join(chosen["notes"])
    ok = discover.select_documents(discover.parse_screener(company_html(), SCREENER),
                                   today=time.strptime("2026-11-03", "%Y-%m-%d"),
                                   policy=hosts.CompanyHostPolicy("KPIT Technologies Ltd", "http://www.kpit.com", None))
    first = ok["concall_candidates"][0]
    assert first["label"] == "Aug 2026" and first["source"]["kind"] == "company_website"
    assert first["source"]["company_domain"] == "kpit.com" and first["source"]["basis"] == "listed_website"
    # an annual report on a company host is never accepted
    parsed_ar = discover.parse_screener(fixture_html().replace(AR_URL, "https://www.kpit.com/ar.pdf"), SCREENER)
    chosen = discover.select_documents(parsed_ar, today=time.strptime("2026-11-03", "%Y-%m-%d"),
                                       policy=hosts.CompanyHostPolicy("KPIT Technologies Ltd", "http://www.kpit.com", None))
    assert all("kpit.com/ar.pdf" not in (c.get("url") or "") for c in chosen["annual_report_candidates"])


def test_flow_company_hosted_transcript_is_used_and_labelled(kpit_pdfs):
    routes = base_routes(kpit_pdfs)
    routes[SCREENER] = fk.FakeResponse(200, company_html().encode())
    routes[KPIT_COMPANY_CALL] = fk.FakeResponse(200, kpit_pdfs["call"])
    out, session = run_flow(routes)
    brief = out["brief"]
    doc = brief["documents"]["concall"]
    assert doc["status"] == "ok" and doc["host"] == "www.kpit.com" and doc["url"] == KPIT_COMPANY_CALL
    assert doc["source"]["kind"] == "company_website" and doc["source"]["basis"] == "listed_website"
    assert doc["fetch"]["source"] == "fetched_now" and "July 29, 2026" in doc["date_in_text"]
    assert doc["date_matches_label"] is True
    assert brief["sections"]["outlook"]["status"] == "ok"
    assert "company's own website (www.kpit.com)" in brief["sections"]["outlook"]["note"]
    assert any("own website" in line for line in brief["limitations"])
    assert brief["host_checks"][0]["allowed"] is True
    referers = [h.get("Referer") for url, h in session.header_log if url == KPIT_COMPANY_CALL]
    assert referers == ["https://kpit.com/"]
    citation = brief["sections"]["outlook"]["claims"][0]["citation"]
    assert citation["url"] == KPIT_COMPANY_CALL and citation["pdf_page"] >= 1


def test_flow_company_hosted_second_domain_reads_the_home_page_once(kpit_pdfs, tmp_path, monkeypatch):
    call = "https://www.kpit-investor-files.example/wp-content/q1.pdf"
    html = company_html(call, website_text="kpitgroup.example", website_href="https://www.kpitgroup.example")
    routes = base_routes(kpit_pdfs)
    routes[SCREENER] = fk.FakeResponse(200, html.encode())
    routes["https://kpitgroup.example/"] = fk.FakeResponse(
        200, b"<html><a href='https://www.kpit-investor-files.example/'>Investors</a></html>")
    routes[call] = fk.FakeResponse(200, kpit_pdfs["call"])
    # name 'KPIT Technologies Ltd' vs domain label 'kpitinvestorfiles' : one starts with the other? no -> refused
    older = "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=70c85b6c-58e6-423f-9b32-50b17a948674.pdf"
    routes[older] = fk.FakeResponse(200, kpit_pdfs["call"])
    out, session = run_flow(routes)
    assert out["brief"]["documents"]["concall"]["url"] == older
    assert call not in session.requests
    # the same layout with a domain whose name does match is accepted (fresh cache: discovery is cached per ticker)
    monkeypatch.setenv("FINN_CACHE_DIR", str(tmp_path / "second"))
    call2 = "https://www.kpit.com/wp-content/q1.pdf"
    html2 = company_html(call2, website_text="kpitgroup.example", website_href="https://www.kpitgroup.example")
    routes[SCREENER] = fk.FakeResponse(200, html2.encode())
    routes["https://kpitgroup.example/"] = fk.FakeResponse(200, b"<html><a href='https://www.kpit.com/'>Investors</a></html>")
    routes[call2] = fk.FakeResponse(200, kpit_pdfs["call"])
    out, session = run_flow(routes)
    doc = out["brief"]["documents"]["concall"]
    assert doc["url"] == call2 and doc["source"]["basis"] == "listed_website_links_to_it"
    assert session.requests.count("https://kpitgroup.example/") == 1
    assert out["brief"]["company_website_page"]["state"] == "ok"


@pytest.mark.parametrize("response,state", [
    (lambda pdf: fk.FakeResponse(403), "inaccessible_http"),
    (lambda pdf: fk.FakeResponse(404), "not_found"),
    (lambda pdf: fk.FakeResponse(200, b"<html>blocked</html>", {"Content-Type": "text/html"}), "not_pdf"),
    (lambda pdf: fk.FakeResponse(302, b"", {"Location": "https://evil.example/x.pdf"}), "host_not_trusted"),
])
def test_flow_company_hosted_failure_is_shown_and_falls_back(kpit_pdfs, response, state):
    routes = base_routes(kpit_pdfs)
    routes[SCREENER] = fk.FakeResponse(200, company_html().encode())
    routes[KPIT_COMPANY_CALL] = response(None)
    older = "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=70c85b6c-58e6-423f-9b32-50b17a948674.pdf"
    routes[older] = fk.FakeResponse(200, kpit_pdfs["call"])
    out, session = run_flow(routes)
    doc = out["brief"]["documents"]["concall"]
    assert doc["status"] == "ok" and doc["url"] == older and doc["source"]["kind"] == "exchange"
    skipped = doc["skipped"][0]
    assert skipped["state"] == state and skipped["host"] == "www.kpit.com" and skipped["source_kind"] == "company_website"
    assert "evil.example" not in " ".join(session.requests)
    assert "could not be used" in out["brief"]["sections"]["outlook"]["note"]


def test_flow_company_hosted_audio_notice_is_rejected_with_facts(kpit_pdfs):
    notice = fk.make_pdf_from_pages([{"page": 1, "text": "Dear Sir, Sub: Audio Recording of Earnings Call. Link below."}])
    routes = base_routes(kpit_pdfs)
    html = company_html().replace('<a href="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=70c85b6c-58e6-423f-9b32-50b17a948674.pdf"', '<a href="https://www.kpit.com/old.pdf"')
    routes[SCREENER] = fk.FakeResponse(200, html.encode())
    routes[KPIT_COMPANY_CALL] = fk.FakeResponse(200, notice)
    routes["https://www.kpit.com/old.pdf"] = fk.FakeResponse(200, notice)
    out, _ = run_flow(routes)
    doc = out["brief"]["documents"]["concall"]
    assert doc["status"] == "not_available"
    assert [s["state"] for s in doc["skipped"]] == ["not_a_transcript", "not_a_transcript"]
    assert doc["skipped"][0]["detail"]["pages"] == 1 and doc["skipped"][0]["host"] == "www.kpit.com"
    assert out["brief"]["sections"]["outlook"]["status"] == "not_found"


def test_flow_unlisted_website_blocks_company_hosted_files(kpit_pdfs):
    html = company_html().replace('<a href="http://www.kpit.com" target="_blank">kpit.com</a>', "")
    routes = base_routes(kpit_pdfs)
    routes[SCREENER] = fk.FakeResponse(200, html.encode())
    routes[KPIT_COMPANY_CALL] = fk.FakeResponse(200, kpit_pdfs["call"])
    out, session = run_flow(routes)
    assert KPIT_COMPANY_CALL not in session.requests
    check = out["brief"]["host_checks"][0]
    assert check["allowed"] is False and "no usable company website" in check["reason"]


def test_company_file_size_cap_is_tighter_than_exchange_cap(kpit_pdfs, monkeypatch):
    monkeypatch.setenv("FINN_RESEARCH_MAX_BYTES_COMPANY", "2000")
    routes = base_routes(kpit_pdfs)
    routes[SCREENER] = fk.FakeResponse(200, company_html().encode())
    routes[KPIT_COMPANY_CALL] = fk.FakeResponse(200, kpit_pdfs["call"])
    out, _ = run_flow(routes)
    skipped = out["brief"]["documents"]["concall"]["skipped"]
    assert skipped[0]["state"] == "too_large" and skipped[0]["source_kind"] == "company_website"


# summaries and fiscal periods

def test_period_tokens():
    assert sections.period_tokens("Management expects H2 FY27 to beat 1H") == {"h2", "fy27", "h1"}
    assert sections.period_tokens("2Q and Q2 and FY2026-27 and FY'26") == {"q2", "fy27", "fy26"}
    assert sections.period_tokens("the second half of the year, 2027, Hall") == set()


def test_claim_naming_a_period_the_quote_does_not_name_is_rejected():
    passage = {"source_id": "S1", "doc": "concall", "page": 15, "speaker": "CFO", "role": "management",
               "text": "Margins, as you know, the H2FY26 margins were at a certain level. We expect 2H to be better than 1H, "
                       "it would imply that our revenues would be higher even on a Y-o-Y basis in H2."}
    quote = "We expect 2H to be better than 1H"
    good = {"claims": [{"text": "Management expects H2 to be better than H1.", "source": "S1", "quote": quote}]}
    bad = {"claims": [{"text": "Management expects H2 FY26 to be better than H1.", "source": "S1", "quote": quote}]}
    assert sections.check_text_claims(good, [passage])[0]["auto_pass"] is True
    rows = sections.check_text_claims(bad, [passage])
    assert rows[0]["auto_pass"] is False and rows[0]["periods_not_in_quote"] == ["fy26"]


def test_text_section_has_no_summary_and_names_the_period_problem():
    passage = {"source_id": "S1", "doc": "concall", "page": 3, "speaker": "CFO", "role": "management",
               "text": "We expect margins to hold in Q3 and Q4 and 2H to be better."}
    docs = {"concall": {"record": {"title": "T", "label": "Oct 2026", "url": "https://www.bseindia.com/x",
                                   "date_label": "Oct 2026", "date_in_text": None}}}
    parsed = {"summary": "H2 FY26 will outperform", "claims": [
        {"text": "Margins hold in Q3 and Q4.", "source": "S1", "quote": "We expect margins to hold in Q3 and Q4"},
        {"text": "Margins hold in Q1 FY27.", "source": "S1", "quote": "We expect margins to hold in Q3"}]}
    section = pipeline.text_section(parsed, [passage], docs)
    assert "summary" not in section and section["status"] == "ok"
    assert [c["text"] for c in section["claims"]] == ["Margins hold in Q3 and Q4."]
    assert any("period_not_in_quote" in p for p in section["failed_checks"][0]["problems"])


def test_prompts_do_not_ask_for_a_summary():
    for prompt in (sections.OVERVIEW_PROMPT, sections.OUTLOOK_PROMPT, sections.NUMBERS_PROMPT):
        assert "summary" not in prompt
    assert "never work out a period yourself" in sections.OUTLOOK_PROMPT


def test_good_run_brief_has_no_summary_anywhere(kpit_pdfs):
    out, _ = run_flow(base_routes(kpit_pdfs))
    assert all("summary" not in section for section in out["brief"]["sections"].values())


def test_speaker_list_with_titles_that_wrap_lines_and_continue_on_page_two():
    page1 = {"page": 1, "text": "Page 1 of 9\n\"X Limited Q1 FY27 Earnings Conference Call\"\nMANAGEMENT: MR. SANDEEP ENGINEER -\n"
             "CHAIRMAN AND MANAGING DIRECTOR -\nX LIMITED\n\nMR. HIRANAND SAVLANI - EXECUTIVE\nDIRECTOR & CHIEF FINANCIAL\nOFFICER - X LIMITED\n"}
    page2 = {"page": 2, "text": "Page 2 of 9\nMR. KAIRAV PATEL - EXECUTIVE\nDIRECTOR - X LIMITED\n\nMODERATOR:\nMR. PRANAV MEHTA - EQUIRUS\nSECURITIES PRIVATE LIMITED\n"}
    from finn import retrieval
    roles = retrieval.speaker_roles([page1, page2])
    assert roles == {"engineer": "management", "savlani": "management", "patel": "management"}
    assert "mehta" not in roles


def test_pinned_adapter_over_a_real_tls_connection(tmp_path, monkeypatch):
    """A local HTTPS server with a throw-away certificate: the checked address is the one connected to, the
    certificate is still verified against the host name, and a private resolution is refused at connect time."""
    import http.server
    import shutil
    import ssl
    import subprocess
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    key, cert = str(tmp_path / "k.pem"), str(tmp_path / "c.pem")
    made = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                           "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost", "-days", "1"],
                          capture_output=True)
    if made.returncode != 0:
        pytest.skip("openssl could not make a certificate")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"%PDF-")

        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "https://localhost:%d/" % server.server_address[1]
    checked = []

    def pretend_public(host, resolver=None):
        checked.append(host)
        return ["127.0.0.1"]
    monkeypatch.setattr(safe_fetch, "public_addresses", pretend_public)
    response = safe_fetch.make_session().get(url, verify=cert, timeout=5)
    assert response.status_code == 200 and response.content == b"%PDF-" and checked == ["localhost"]
    monkeypatch.undo()
    with pytest.raises(requests.exceptions.ConnectionError) as error:
        safe_fetch.make_session(lambda host: ["127.0.0.1"]).get(url, verify=cert, timeout=5)
    assert "dns_not_public" in str(error.value)
    server.shutdown()


# ------------------------------------------------------------------ statement title kept in the passage (round 8)

COFORGE_LIKE_PAGE = (
    "Coforge Limited\n(CIN: L72100HR1992PLC128382)\nConsolidated Balance Sheet\n(All amounts in INR Mn unless otherwise stated)\n"
    + "Property, plant and equipment 3 9,425 7,682\n" * 40
    + "As per our report of even date\nPlace : Hyderabad\nDate : May 5, 2026\n"
    + "Coforge Limited\n(CIN: L72100HR1992PLC128382)\nConsolidated Statement of Profit and Loss\n"
      "(All amounts in INR Mn unless otherwise stated)\nThe accompanying notes are an integral part of the consolidated financial statements.\n"
      "As per our report of even date\nFor and on behalf of Board of Directors\nChartered Accountants\n"
      "Firm Registration No. 101049W/E300004\nDIN : 07080613\nPlace : Hyderabad\nDate : May 5, 2026\n"
    + "Particulars\nNote\nYear ended \nMarch 31, 2026\nYear ended \nMarch 31, 2025\nContinuing Operations\nIncome\n"
      "Revenue from operations\n17\n1,64,027\n1,20,733\nOther income\n18\n732\n1,647\nTotal income\n1,64,759\n1,22,380\n"
      "Total expenses\n1,43,171\n1,09,419\nProfit before tax\n19,328\n12,961\nTotal tax expense\n2,583\n3,326\n"
      "Profit for the year\n17,447\n9,361\nBasic earnings per share\n46.44\n24.60\n"
)


def test_statement_passage_starts_at_the_statement_title_not_in_the_middle_of_the_signature_block():
    passages = sections.numbers_passages([{"page": 136, "text": COFORGE_LIKE_PAGE}])
    text = passages[0]["text"]
    assert text.startswith("Consolidated Statement of Profit and Loss")
    assert "All amounts in INR Mn" in text and "Profit for the year\n17,447" in text
    # the balance sheet title above is not pulled in
    assert "Consolidated Balance Sheet" not in text


def test_statement_start_falls_back_to_500_chars_above_the_table_when_no_title_is_near():
    page = "Some filler line about the business.\n" * 60 + "Revenue from operations 100\nProfit for the year 10\n"
    start = sections._statement_start(page)
    assert start == page.lower().find("revenue from operations") - 500


def test_statement_start_ignores_a_title_that_is_far_above_the_table():
    far = "Consolidated Statement of Profit and Loss\n" + "x filler line of text here\n" * 300 + "Revenue from operations 100\n"
    assert sections._statement_start(far) == far.lower().find("revenue from operations") - 500


def test_checks_stay_strict_for_the_coforge_items_and_now_pass_because_the_words_are_in_the_passage():
    passages = sections.numbers_passages([{"page": 136, "text": COFORGE_LIKE_PAGE}])
    good = {"items": [{"metric": "Revenue from operations", "value": "1,64,027", "unit": "INR Mn",
                       "period": "Year ended March 31, 2026", "scope": "consolidated", "source": "S1",
                       "quote": "Revenue from operations\n17\n1,64,027\n1,20,733"}]}
    rows = sections.check_number_items(good, passages)
    assert rows[0]["auto_pass"] is True, rows[0]["problems"]
    # same page with the title removed: the scope check must still reject it
    stripped = COFORGE_LIKE_PAGE.replace("Consolidated Statement of Profit and Loss", "Statement").replace(
        "(All amounts in INR Mn unless otherwise stated)", "").replace("consolidated", "group")
    bare = [{"source_id": "S1", "page": 136, "char_start": 0, "text": stripped[stripped.find("Revenue from operations") - 100:],
             "doc": "annual_report"}]
    bad = sections.check_number_items(good, bare)
    assert bad[0]["auto_pass"] is False and "scope_not_in_passage" in bad[0]["problems"]
    assert "unit_not_in_passage" in bad[0]["problems"]


def test_real_coforge_annual_report_page_keeps_title_and_unit():
    path = "/tmp/pdfs3/finn_verify_COFORGE_AR.pdf"
    if not os.path.exists(path):
        pytest.skip("original Coforge annual report not present")
    import pymupdf
    doc = pymupdf.open(path)
    page = {"page": 136, "text": doc[135].get_text()}
    passages = sections.numbers_passages([page])
    assert passages and passages[0]["page"] == 136
    text = passages[0]["text"]
    assert "Consolidated Statement of Profit and Loss" in text and "INR Mn" in text
    assert "Profit for the year\n17,447" in text


# ------------------------------------------------------------------ output-token budget (round 8)

GROQ_OTPM_MESSAGE = ("Rate limited: Rate limit reached for model `qwen/qwen3.8-27b` in organization `org_01m0wc4vhhe5y9nmny86skeb1k` "
                     "service tier `on_demand` on output tokens per minute (OTPM): Limit 1000, Used 625, Requested 600. "
                     "Please try again in 13.5s.")


def test_parse_rate_limit_reads_only_what_the_provider_said():
    from finn import llm_adapter
    got = llm_adapter.parse_rate_limit(GROQ_OTPM_MESSAGE)
    assert got == {"code": "OTPM", "name": "output tokens per minute", "limit": 1000, "used": 625, "requested": 600}
    assert llm_adapter.parse_rate_limit("Rate limited: slow down") is None
    tpm = GROQ_OTPM_MESSAGE.replace("output tokens per minute (OTPM)", "tokens per minute (TPM)")
    assert llm_adapter.parse_rate_limit(tpm)["code"] == "TPM"


def test_run_section_reports_which_limit_and_hides_the_organisation_id():
    from finn import llm_adapter

    def chat(config, messages):
        raise llm_adapter.LLMRateLimited(GROQ_OTPM_MESSAGE, status=429, retry_after=3000.0)
    section = pipeline.run_section("model:x", Cfg(), [{"role": "user", "content": "x"}], 100,
                                   pacer.TokenPacer(10 ** 9, 0), chat, lambda p: {"status": "ok"}, pipeline.Tracker(None))
    assert section["rate_limit"]["code"] == "OTPM" and section["rate_limit"]["limit"] == 1000
    assert "org_01m0" not in section["provider_detail"] and "org_…" in section["provider_detail"]


def test_pacer_holds_back_a_call_that_would_exceed_the_output_budget_even_when_total_is_low():
    clock = {"t": 1000.0}
    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] = clock["t"] + seconds

    p = pacer.TokenPacer(limit_per_minute=7000, output_limit_per_minute=1000)
    p.record(1700, now=lambda: clock["t"], output_tokens=600)       # first call: 600 output tokens
    assert p.wait_for(1500, sleep=fake_sleep, now=lambda: clock["t"], estimated_output=300) == 0   # 900 <= 1000
    p.record(1500, now=lambda: clock["t"], output_tokens=455)       # second call: 455 -> 1055 used
    waited = p.wait_for(1500, sleep=fake_sleep, now=lambda: clock["t"], estimated_output=600)
    assert waited > 55 and len(slept) >= 1                              # total 3,200 + 1,500 is fine; output is not


def test_pacer_output_budget_can_be_switched_off_and_unknown_output_counts_in_full():
    p = pacer.TokenPacer(limit_per_minute=10 ** 9, output_limit_per_minute=0)
    p.record(5000, output_tokens=5000)
    assert p.wait_for(10, estimated_output=5000) == 0
    q = pacer.TokenPacer(limit_per_minute=10 ** 9, output_limit_per_minute=1000)
    q.record(900)                                                   # output unknown -> counted as 900
    assert q.events[0][2] == 900


def test_call_model_asks_the_pacer_for_output_budget_and_records_completion_tokens():
    seen = {}

    class Spy(pacer.TokenPacer):
        def wait_for(self, estimated_tokens, **kwargs):
            seen["wait"] = kwargs
            return 0.0

    spy = Spy(10 ** 9, 1000)

    def chat(config, messages):
        return {"text": '{"claims": []}', "usage": {"total_tokens": 900, "completion_tokens": 450}, "latency_s": 0.1}
    pipeline.call_model(Cfg(), [{"role": "user", "content": "x"}], 600, spy, chat)
    assert seen["wait"]["estimated_output"] == 600 and spy.events[0][1:] == (900, 450)
