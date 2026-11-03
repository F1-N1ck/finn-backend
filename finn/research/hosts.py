"""Which web hosts may a transcript be downloaded from, besides the stock exchanges?

Many companies publish their call transcripts on their own site and only link to them from Screener. A link on
Screener is not proof that the file belongs to the company, so a company host is accepted only when it is
independently tied to the company the user asked about:

  1. listed_website - the host is the company's website as listed on its Screener page (or a subdomain of it).
  2. listed_website_links_to_it - the host is NOT on the listed website's domain (some groups publish investor
     documents on a second domain, for example Astral lists astralpipes.com but files transcripts on astralltd.com),
     but BOTH hold: the company's own listed website links to that domain, and the company name and the domain's
     name part agree (one starts with the other). Either alone is not enough (every homepage links to social networks; a group site can link to a
     sister company).

Everything else is refused with a reason that is shown to the reader. The user never supplies a URL or a host.
Shared platforms (CDNs, cloud storage, link shorteners, social networks, exchanges, free hosting) are never a
company's own domain, so a link to one of them is refused even when the name matches.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

SHARED_DOMAINS = {
    "screener.in", "bseindia.com", "nseindia.com", "sebi.gov.in", "google.com", "googleapis.com", "gstatic.com",
    "googleusercontent.com", "youtube.com", "youtu.be", "facebook.com", "fb.com", "twitter.com", "x.com",
    "linkedin.com", "instagram.com", "wikipedia.org", "github.com", "github.io", "cloudfront.net", "amazonaws.com",
    "azurewebsites.net", "azureedge.net", "windows.net", "herokuapp.com", "vercel.app", "netlify.app", "web.app",
    "firebaseapp.com", "pages.dev", "workers.dev", "blogspot.com", "wordpress.com", "wixsite.com", "weebly.com",
    "sharepoint.com", "onedrive.com", "live.com", "dropbox.com", "box.com", "bit.ly", "tinyurl.com", "t.co",
    "goo.gl", "ow.ly", "apple.com", "play.google.com", "medium.com", "substack.com", "whatsapp.com", "telegram.org",
}
GENERIC_SUFFIX_LABELS = {"co", "com", "org", "net", "gov", "edu", "ac", "nic", "res", "ltd", "plc"}
GENERIC_NAME_WORDS = {"ltd", "limited", "pvt", "private", "public", "india", "the", "and", "company", "co", "corporation",
                      "corp", "inc", "plc", "of"}
LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def host_under(host, domain):
    return bool(domain) and (host == domain or host.endswith("." + domain))


def site_domain(host):
    """The host without a leading 'www.', or None if it cannot be a company's own domain (an IP address, a bare
    public suffix such as co.in, or a shared platform)."""
    host = (host or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) < 2 or not all(LABEL_RE.match(label) for label in labels):
        return None
    if labels[-1].isdigit() or len(labels[-1]) < 2:
        return None
    if len(labels) == 2 and labels[0] in GENERIC_SUFFIX_LABELS:
        return None
    for index in range(len(labels) - 1):
        if ".".join(labels[index:]) in SHARED_DOMAINS:
            return None
    return host


def name_key(company_name):
    """'Astral Ltd' -> 'astral'; 'Tata Elxsi Ltd' -> 'tataelxsi'. Empty if too short to mean anything."""
    words = re.sub(r"[^a-z0-9 ]", " ", (company_name or "").lower()).split()
    kept = [word for word in words if word not in GENERIC_NAME_WORDS]
    key = "".join(kept)
    return key if len(key) >= 4 else ""


def domain_label(domain):
    """The name part of a domain: astralltd.com -> astralltd, investors.coforge.com -> coforge, abc.co.in -> abc."""
    labels = domain.split(".")
    if len(labels) >= 3 and labels[-2] in GENERIC_SUFFIX_LABELS and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2]


def name_in_domain(domain, company_name):
    """The company name and the domain's name part agree: one starts with the other, ignoring a trailing 'ltd'/
    'group'/'india'. 'Astral Ltd' ~ astralltd.com and 'KPIT Technologies Ltd' ~ kpit.com agree; 'Tata Elxsi Ltd' and
    tatamotors.com do not."""
    key = name_key(company_name)
    core = re.sub(r"[^a-z0-9]", "", domain_label(domain))
    core = re.sub(r"(limited|ltd|india|group|inc|corp)$", "", core)
    return len(key) >= 4 and len(core) >= 4 and (key.startswith(core) or core.startswith(key))


class _LinkParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def linked_domains(html, base_url):
    parser = _LinkParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    found = []
    for href in parser.hrefs[:3000]:
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        if urlsplit(absolute).scheme not in ("http", "https"):
            continue
        domain = site_domain(host_of(absolute))
        if domain is not None and domain not in found:
            found.append(domain)
    return found


class CompanyHostPolicy:
    """Decides, per host, whether a company-hosted transcript may be fetched. `fetch_homepage` is a function that
    returns {"state": ..., "html": ..., "seconds": ...} for the listed website's home page; it is called at most once,
    and only when the cheaper checks cannot decide."""

    def __init__(self, company_name, website_url, fetch_homepage=None):
        self.company_name = company_name
        self.website_url = website_url
        self.listed_domain = site_domain(host_of(website_url)) if website_url else None
        self.fetch_homepage = fetch_homepage
        self.homepage = None  # what happened when the home page was read (shown to the reader)
        self._linked = None
        self.decisions = {}

    def check(self, host):
        host = (host or "").lower()
        if host not in self.decisions:
            self.decisions[host] = self._decide(host)
        return self.decisions[host]

    def _deny(self, host, reason):
        return {"allowed": False, "host": host, "reason": reason, "listed_website": self.listed_domain}

    def _allow(self, host, domain, basis, detail):
        return {"allowed": True, "host": host, "company_domain": domain, "basis": basis, "detail": detail,
                "listed_website": self.listed_domain}

    def _decide(self, host):
        if self.listed_domain is None:
            return self._deny(host, "no usable company website is listed on the Screener page, so a company-hosted "
                                    "file cannot be tied to the company")
        if host_under(host, self.listed_domain):
            return self._allow(host, self.listed_domain, "listed_website",
                               "host is on the company website listed on Screener (" + self.listed_domain + ")")
        domain = site_domain(host)
        if domain is None:
            return self._deny(host, "the host is a shared platform, an IP address or not a usable domain name")
        if not name_in_domain(domain, self.company_name):
            return self._deny(host, "the host is not on the listed website (" + self.listed_domain
                              + ") and the company name does not appear in the domain")
        if domain not in self._linked_domains():
            state = (self.homepage or {}).get("state")
            return self._deny(host, "the listed website (" + self.listed_domain + ") does not link to " + domain
                              + (" (its home page could not be read: " + str(state) + ")" if state != "ok" else ""))
        return self._allow(host, domain, "listed_website_links_to_it",
                           "the company's listed website (" + self.listed_domain + ") links to " + domain
                           + " and the company name matches that domain")

    def _linked_domains(self):
        if self._linked is not None:
            return self._linked
        self._linked = []
        if self.fetch_homepage is None:
            self.homepage = {"state": "not_attempted"}
            return self._linked
        result = self.fetch_homepage(self.listed_domain)
        self.homepage = {"state": result.get("state"), "seconds": result.get("seconds"),
                         "url": "https://" + self.listed_domain + "/"}
        if result.get("state") == "ok":
            self._linked = linked_domains(result.get("html") or "", "https://" + self.listed_domain + "/")
            self.homepage["links_found"] = len(self._linked)
        return self._linked
