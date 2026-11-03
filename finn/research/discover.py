"""Find a company's latest annual report and earnings-call transcript from its Screener page.

Screener lists documents under headings (Annual reports, Concalls ...). Its exact HTML was only
seen through a text summary, so the parser is defensive: it reads headings, list items and links,
has a fallback that classifies links by their text, and reports diagnostics when it finds nothing.
"""

import re
import time
from html.parser import HTMLParser
from urllib.parse import quote, urljoin

from finn.research import hosts, safe_fetch

SCREENER_BASE = "https://www.screener.in"
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
MONTH_YEAR_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(20\d\d)(?!\d)")
YEAR_RE = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")


def screener_url(symbol):
    return SCREENER_BASE + "/company/" + quote(symbol, safe="") + "/"


def section_for_heading(text):
    lowered = text.lower()
    if "annual report" in lowered:
        return "annual_reports"
    if "concall" in lowered or "earnings call" in lowered:
        return "concalls"
    if "announcement" in lowered:
        return "announcements"
    if "credit rating" in lowered:
        return "credit_ratings"
    return None


class ScreenerParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.h1_text = ""
        self.heading_stack = None
        self.heading_text = ""
        self.section = None
        self.in_li = False
        self.item = None
        self.anchor = None
        self.items = []
        self.all_anchors = []
        self.headings_seen = []
        self.text_chunks = []
        self.seen_h1 = False

    def _gap(self):
        if self.anchor is not None:
            self.anchor["text"] = self.anchor["text"] + " "
        if self.item is not None:
            self.item["texts"].append(" ")

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in ("div", "span", "br", "p", "small"):
            self._gap()
        if tag in ("h1", "h2", "h3", "h4"):
            self.heading_stack = tag
            self.heading_text = ""
        elif tag == "li":
            self.in_li = True
            self.item = {"section": self.section, "texts": [], "anchors": []}
        elif tag == "a":
            self.anchor = {"href": attributes.get("href", ""), "title": attributes.get("title", ""),
                           "text": "", "section": self.section}

    def handle_endtag(self, tag):
        if tag in ("div", "span", "p", "small"):
            self._gap()
        if tag == self.heading_stack:
            text = " ".join(self.heading_text.split())
            if tag == "h1" and not self.seen_h1:
                self.h1_text = text
                self.seen_h1 = True
            if tag != "h1":
                self.headings_seen.append(text)
                self.section = section_for_heading(text)
            self.heading_stack = None
        elif tag == "li":
            if self.item is not None and self.item["section"] is not None:
                self.items.append(self.item)
            self.item = None
            self.in_li = False
        elif tag == "a":
            if self.anchor is not None:
                self.anchor["text"] = " ".join(self.anchor["text"].split())
                self.all_anchors.append(self.anchor)
                if self.item is not None:
                    self.item["anchors"].append(self.anchor)
                elif self.anchor["section"] is not None:
                    self.items.append({"section": self.anchor["section"],
                                       "texts": [self.anchor["text"]], "anchors": [self.anchor]})
                self.anchor = None

    def handle_data(self, data):
        if self.heading_stack is not None:
            self.heading_text = self.heading_text + data
        if self.anchor is not None:
            self.anchor["text"] = self.anchor["text"] + data
        if self.item is not None:
            self.item["texts"].append(data)
        self.text_chunks.append(data)


def _clean(text):
    return " ".join(text.split())


def parse_screener(html, base_url):
    parser = ScreenerParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # keep whatever was parsed so far
    page_text = _clean(" ".join(parser.text_chunks))
    result = {"company_name": parser.h1_text or None, "nse_symbol": None, "bse_code": None,
              "website": None, "annual_reports": [], "concalls": []}
    match = re.search(r"NSE:\s*([A-Z0-9&\-]+)", page_text)
    if match:
        result["nse_symbol"] = match.group(1)
    match = re.search(r"BSE:\s*(\d{5,6})", page_text)
    if match:
        result["bse_code"] = match.group(1)
    for anchor in parser.all_anchors[:60]:  # the company's own website is linked near the top, before the documents
        href = anchor["href"]
        if not href.startswith(("http://", "https://")):
            continue
        shown = anchor["text"].strip().lower()
        shown = shown[4:] if shown.startswith("www.") else shown
        link_host = hosts.host_of(href)
        link_host = link_host[4:] if link_host.startswith("www.") else link_host
        # Screener prints the site as its own link text ("astralpipes.com"); require text and link to agree
        if shown != "" and shown == link_host and hosts.site_domain(link_host) is not None:
            result["website"] = href
            break

    for item in parser.items:
        if item["section"] == "annual_reports":
            for anchor in item["anchors"]:
                _add_annual_report(result, item, anchor, base_url)
        elif item["section"] == "concalls":
            _add_concall(result, item, base_url)

    mode = "headings"
    if len(result["annual_reports"]) == 0 and len(result["concalls"]) == 0:
        mode = "fallback_by_link_text"
        _fallback(result, parser, base_url)

    result["diagnostics"] = {
        "parse_mode": mode,
        "anchors": len(parser.all_anchors),
        "headings_seen": parser.headings_seen[:20],
        "annual_reports_found": len(result["annual_reports"]),
        "concalls_found": len(result["concalls"]),
        "first_anchor_texts": [a["text"] for a in parser.all_anchors[:8]],
    }
    return result


def _add_annual_report(result, item, anchor, base_url):
    href = anchor["href"]
    if href == "" or href.startswith("#") or href.startswith("javascript"):
        return
    label = anchor["text"]
    years = YEAR_RE.findall(label + " " + anchor["title"])
    if len(years) == 0:
        return
    result["annual_reports"].append({"label": label.split(" from ")[0].strip(),
                                     "year": max(int(y) for y in years),
                                     "url": urljoin(base_url, href)})


def _add_concall(result, item, base_url):
    joined = _clean(" ".join(item["texts"]))
    month_match = MONTH_YEAR_RE.search(joined)
    entry = {"label": None, "year": None, "month": None, "transcript_url": None,
             "other_links": []}
    if month_match:
        entry["label"] = month_match.group(1) + " " + month_match.group(2)
        entry["month"] = MONTHS[month_match.group(1).lower()]
        entry["year"] = int(month_match.group(2))
    for anchor in item["anchors"]:
        href = anchor["href"]
        if href == "" or href.startswith("#") or href.startswith("javascript"):
            continue
        words = (anchor["text"] + " " + anchor["title"]).lower()
        if "transcript" in words and entry["transcript_url"] is None:
            entry["transcript_url"] = urljoin(base_url, href)
        else:
            entry["other_links"].append(anchor["text"] or "link")
    if entry["label"] is None and entry["transcript_url"] is None:
        return
    result["concalls"].append(entry)


def _fallback(result, parser, base_url):
    for anchor in parser.all_anchors:
        href = anchor["href"]
        text = anchor["text"]
        lowered = text.lower()
        if href == "" or href.startswith("#"):
            continue
        if re.search(r"(annual report|financial year)\s*(19|20)\d\d", lowered):
            years = YEAR_RE.findall(text)
            result["annual_reports"].append({"label": text, "year": max(int(y) for y in years),
                                             "url": urljoin(base_url, href)})
        elif "transcript" in lowered:
            result["concalls"].append({"label": None, "year": None, "month": None,
                                       "transcript_url": urljoin(base_url, href), "other_links": []})


MAX_CALL_AGE_MONTHS = 15


def _months_old(entry, today):
    if not entry.get("year"):
        return None
    return (today.tm_year - entry["year"]) * 12 + (today.tm_mon - (entry.get("month") or 6))


def select_documents(parsed, today=None, max_call_age_months=MAX_CALL_AGE_MONTHS, policy=None):
    """Choose candidate documents, newest first, and note what was skipped and why.
    A transcript older than max_call_age_months is not used as 'management outlook': stale guidance is worse than none.
    `policy` (hosts.CompanyHostPolicy) lets a transcript on the company's own website through; without it only
    the exchange hosts are accepted."""
    if today is None:
        today = time.localtime()
    notes = []
    host_checks = []
    annual = sorted(parsed["annual_reports"], key=lambda e: e["year"], reverse=True)
    calls = []
    without_transcript = 0
    newest_without = None
    ordered = sorted(parsed["concalls"], key=lambda e: ((e["year"] or 0), (e["month"] or 0)), reverse=True)
    for entry in ordered:
        if entry["transcript_url"] is None:
            if len(calls) == 0:
                without_transcript = without_transcript + 1
                if newest_without is None:
                    newest_without = entry["label"]
            continue
        age = _months_old(entry, today)
        if age is not None and age > max_call_age_months:
            if not any(n.startswith("Skipped as too old") for n in notes):
                notes.append("Skipped as too old (over " + str(max_call_age_months) + " months): newest transcript with a link is '"
                             + str(entry["label"]) + "'.")
            continue
        calls.append(entry)
    if without_transcript > 0:
        notes.append(str(without_transcript) + " newer concall(s) are listed without a transcript link"
                     + (" (latest: " + newest_without + ")" if newest_without else "")
                     + "; using the newest call that has one.")
    return {"annual_report_candidates": _trusted_first(annual, notes, "annual report", None, host_checks),
            "concall_candidates": _trusted_first(calls, notes, "concall transcript", policy, host_checks),
            "notes": notes, "host_checks": host_checks,
            "company_website": (policy.listed_domain if policy is not None else None)}


def _trusted_first(entries, notes, what, policy, host_checks):
    """Keep at most 3 candidates. A link on an exchange host is kept. A link on any other host is kept only for a
    transcript, and only if the policy ties that host to the company; otherwise it is skipped and the reason is
    recorded (notes for the page, host_checks for the brief)."""
    kept = []
    for entry in entries:
        url = entry.get("url") or entry.get("transcript_url")
        host = safe_fetch.host_of(url)
        ok, code = safe_fetch.check_url(url)
        if ok:
            kept.append(dict(entry, source={"kind": "exchange", "host": host}))
        elif code == "host_not_trusted" and policy is not None and what == "concall transcript":
            decision = policy.check(host)
            host_checks.append({"label": entry.get("label"), "url": url, "host": host, "allowed": decision["allowed"],
                                "basis": decision.get("basis"), "detail": decision.get("detail"),
                                "reason": decision.get("reason")})
            if not decision["allowed"]:
                notes.append("Skipped " + what + " '" + str(entry.get("label")) + "': host_not_trusted (" + host + "); "
                             + decision["reason"] + ".")
                continue
            kept.append(dict(entry, source={"kind": "company_website", "host": host,
                                            "company_domain": decision["company_domain"], "basis": decision["basis"],
                                            "detail": decision["detail"], "listed_website": decision["listed_website"]}))
        else:
            notes.append("Skipped " + what + " '" + str(entry.get("label")) + "': "
                         + code + " (" + host + ")")
            continue
        if len(kept) == 3:
            break
    return kept


def homepage_fetcher(session, resolver):
    """Reads the home page of the company's listed website, for the 'website links to it' check."""
    def fetch_homepage(domain):
        got = safe_fetch.fetch("https://" + domain + "/", session=session, max_bytes=1500000, total_timeout_s=12.0,
                               read_timeout_s=8.0, resolver=resolver, company_domain=domain)
        if got["ok"]:
            return {"state": "ok", "html": got["data"].decode("utf-8", errors="replace"), "seconds": got["seconds"]}
        return {"state": got["state"], "seconds": got["seconds"]}
    return fetch_homepage


def discover(symbol, session=None, resolver=None, total_timeout_s=40.0):
    """Fetch the Screener page and parse it. Returns a dict with state and parsed info."""
    url = screener_url(symbol)
    response = safe_fetch.fetch(url, session=session, max_bytes=4000000, total_timeout_s=total_timeout_s,
                                resolver=resolver)
    info = {"screener_url": url, "fetch_state": response["state"], "seconds": response["seconds"]}
    if response["state"] == "not_found":
        info["state"] = "ticker_not_found"
        return info
    if response["state"] != "ok":
        info["state"] = "source_unreachable"
        info["detail"] = response.get("detail") or response.get("http_status")
        return info
    html = response["data"].decode("utf-8", errors="replace")
    parsed = parse_screener(html, response["url"])
    info["final_url"] = response["url"]
    info["parsed"] = parsed
    if parsed["nse_symbol"] is None and parsed["company_name"] is None:
        info["state"] = "company_not_resolved"
        return info
    if parsed["nse_symbol"] is not None and parsed["nse_symbol"].upper() != symbol.upper():
        info["state"] = "ticker_mismatch"
        return info
    policy = hosts.CompanyHostPolicy(parsed["company_name"], parsed["website"], homepage_fetcher(session, resolver))
    selected = select_documents(parsed, policy=policy)
    selected["homepage_check"] = policy.homepage
    info["selection"] = selected
    if len(parsed["annual_reports"]) == 0 and len(parsed["concalls"]) == 0:
        info["state"] = "documents_not_listed"
        return info
    info["state"] = "ok"
    return info
