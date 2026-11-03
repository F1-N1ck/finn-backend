"""Offline end-to-end checks on REAL company-hosted transcripts (downloaded by hand on 2026-11-03).
Skipped unless the PDFs are in FINN_REAL_PDF_DIR (default /tmp/pdfs6): ASTRAL_AUG2026_CALL.pdf, COFORGE_Q2FY27_CALL.pdf.
Network is faked; the model is a fake that quotes the first passage. These tests check discovery, host rules, fetch,
extraction, transcript check, call date and speaker roles on real files - not what a real model would say."""

import os
import warnings

import pytest

warnings.filterwarnings("ignore")

from finn import retrieval
from finn.research import extract, pacer, pipeline, sections
from tests import research_fakes as fk

PDF_DIR = os.environ.get("FINN_REAL_PDF_DIR", "/tmp/pdfs6")
ASTRAL_CALL = os.path.join(PDF_DIR, "ASTRAL_AUG2026_CALL.pdf")
COFORGE_CALL = os.path.join(PDF_DIR, "COFORGE_Q2FY27_CALL.pdf")
ASTRAL_HTML = os.path.join(os.path.dirname(__file__), "fixtures", "screener_astral_listing_reconstructed.html")

needs_pdfs = pytest.mark.skipif(not (os.path.exists(ASTRAL_CALL) and os.path.exists(COFORGE_CALL)),
                                reason="real transcripts not on this machine")


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


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def small_ar():
    return fk.make_pdf_from_pages([{"page": 1, "text": "Notes\nCorporate information\nThe Company is engaged in the business of pipes."},
                                   {"page": 2, "text": "Statement of Profit and Loss\nRevenue from operations 23 65,686 58,324\nTotal income 66,000 59,000\n"
                                    "Tax expense 1,000 900\nProfit for the year 5,347 5,189\nBasic (in `) 19.97 19.50\nconsolidated ` In Million"}])


def run(symbol, html, routes):
    session = fk.FakeSession(routes)
    out = pipeline.analyze(symbol, Cfg(), session=session, resolver=fk.public_resolver,
                           chat_fn=fk.make_fake_chat("good"), pacer=pacer.TokenPacer(10 ** 9, 0))
    return out, session


@needs_pdfs
def test_astral_august_transcript_on_the_second_company_domain():
    html = open(ASTRAL_HTML).read()
    aug = "https://www.astralltd.com/wp-content/uploads/2026/08/Equirus-Astral-Aug12-2026-1.pdf"
    ar = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/5772a6ee-bd33-47d5-bbd1-85b269bd26c3.pdf"
    routes = {"https://www.screener.in/company/ASTRAL/": fk.FakeResponse(200, html.encode()),
              "https://www.bseindia.com/": fk.FakeResponse(200, b"<html></html>"), ar: fk.FakeResponse(200, small_ar()),
              "https://astralpipes.com/": fk.FakeResponse(200, b"<html><a href='https://astralltd.com/'>About Astral Group</a></html>"),
              aug: fk.FakeResponse(200, read(ASTRAL_CALL))}
    out, session = run("ASTRAL", html, routes)
    doc = out["brief"]["documents"]["concall"]
    assert doc["status"] == "ok" and doc["url"] == aug
    assert doc["source"]["basis"] == "listed_website_links_to_it" and doc["source"]["company_domain"] == "astralltd.com"
    assert doc["date_in_text"] == "August 12, 2026" and doc["date_matches_label"] is True
    assert doc["extraction"]["page_count"] == 29 and doc["fetch"]["bytes"] == 238892
    assert out["brief"]["sections"]["outlook"]["status"] == "ok"
    assert [u for u in session.requests if "nseindia" in u] == []  # the audio notice is not even needed


@needs_pdfs
def test_astral_cfo_is_management_even_though_the_title_wraps_lines():
    pages = extract.extract_pdf(read(ASTRAL_CALL), max_pages=100, budget_s=60, max_rss_mb=400)["pages"]
    roles = retrieval.speaker_roles(pages)
    assert roles.get("savlani") == "management" and roles.get("engineer") == "management"
    turns = retrieval.split_turns(pages)
    savlani = [t for t in turns if t["speaker"] == "Hiranand Savlani"]
    assert len(savlani) > 20 and all(t["role"] == "management" for t in savlani)
    passages, method = sections.outlook_passages(pages)
    assert method == "management_turns" and all(p["role"] == "management" for p in passages)


@needs_pdfs
def test_coforge_october_transcript_on_a_listed_website_subdomain():
    url = "https://investors.coforge.com/hubfs/Coforge-Limited-Earnings-Call-Transcript-Q2FY27-October-23-2026.pdf?hsLang=en"
    html = (open(ASTRAL_HTML).read().replace("Astral Ltd", "Coforge Ltd").replace("astralpipes.com", "coforge.com")
            .replace("ASTRAL", "COFORGE").replace("Aug 2026", "Oct 2026")
            .replace("https://www.astralltd.com/wp-content/uploads/2026/08/Equirus-Astral-Aug12-2026-1.pdf", url))
    ar = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/5772a6ee-bd33-47d5-bbd1-85b269bd26c3.pdf"
    routes = {"https://www.screener.in/company/COFORGE/": fk.FakeResponse(200, html.encode()),
              "https://www.bseindia.com/": fk.FakeResponse(200, b"<html></html>"), ar: fk.FakeResponse(200, small_ar()),
              url: fk.FakeResponse(200, read(COFORGE_CALL))}
    out, session = run("COFORGE", html, routes)
    doc = out["brief"]["documents"]["concall"]
    assert doc["status"] == "ok" and doc["url"] == url and doc["host"] == "investors.coforge.com"
    assert doc["source"]["basis"] == "listed_website" and doc["date_in_text"] == "October 23, 2026"
    assert doc["date_label"] == "Oct 2026" and doc["date_matches_label"] is True
    assert "coforge.com" not in [u for u in session.requests if u.endswith("/")]  # no home-page read was needed
