"""Outbound fetching with guards.

Rules:
  * https only, port 443 only, no user:password in the URL
  * host must be in TRUSTED_HOSTS (exact match, no wildcards)
  * every redirect hop is re-checked against the same rules
  * the host name must resolve only to public IP addresses
  * download size and total time are capped
  * a PDF must start with the bytes %PDF-
The URL always comes from our own code or from a trusted page we fetched; never from the user.
"""

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import requests

TRUSTED_HOSTS = {
    "www.screener.in": "screener",
    "www.bseindia.com": "bse",
    "nsearchives.nseindia.com": "nse_archive",
    "archives.nseindia.com": "nse_archive",
}
MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 4
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def check_url(url):
    """Return (ok, code). code is a short reason when not ok."""
    if not isinstance(url, str) or url == "" or len(url) > MAX_URL_LENGTH:
        return False, "bad_url"
    for char in url:
        if ord(char) < 33 or ord(char) == 127:
            return False, "bad_url"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False, "bad_url"
    if parts.scheme != "https":
        return False, "bad_url"
    if parts.username is not None or parts.password is not None:
        return False, "bad_url"
    if port is not None and port != 443:
        return False, "bad_url"
    host = (parts.hostname or "").lower()
    if host not in TRUSTED_HOSTS:
        return False, "host_not_trusted"
    return True, None


def default_resolver(host):
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    addresses = []
    for info in infos:
        addresses.append(info[4][0])
    return addresses


def resolves_public(host, resolver=None):
    resolver = resolver or default_resolver
    try:
        addresses = resolver(host)
    except Exception:
        return False
    if len(addresses) == 0:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    return session


def _result(state, started, **extra):
    info = {"ok": state == "ok", "state": state, "seconds": round(time.monotonic() - started, 2)}
    info.update(extra)
    return info


def fetch(url, session=None, max_bytes=35000000, total_timeout_s=60.0, expect_pdf=False,
          resolver=None, referer=None):
    """Download one URL under all the guards. Never raises. Returns a dict with state.

    states: ok, bad_url, host_not_trusted, dns_not_public, not_found, inaccessible_http,
            timeout, too_large, not_pdf, inaccessible, too_many_redirects
    """
    started = time.monotonic()
    session = session or make_session()
    current = url
    redirects = 0
    while True:
        ok, code = check_url(current)
        if not ok:
            return _result(code, started, url=current, host=host_of(current))
        if not resolves_public(host_of(current), resolver):
            return _result("dns_not_public", started, url=current, host=host_of(current))
        remaining = total_timeout_s - (time.monotonic() - started)
        if remaining <= 1:
            return _result("timeout", started, url=current)
        headers = {}
        if referer:
            headers["Referer"] = referer
        response = None
        try:
            response = session.get(current, stream=True, allow_redirects=False,
                                   timeout=(5, min(20, remaining)), headers=headers)
            status = response.status_code
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                response.close()
                redirects = redirects + 1
                if redirects > MAX_REDIRECTS or location == "":
                    return _result("too_many_redirects", started, url=current)
                current = urljoin(current, location)
                continue
            if status == 404:
                response.close()
                return _result("not_found", started, url=current, http_status=status)
            if status != 200:
                response.close()
                return _result("inaccessible_http", started, url=current, http_status=status)
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                response.close()
                return _result("too_large", started, url=current, declared_bytes=int(declared))
            chunks = []
            total = 0
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                total = total + len(chunk)
                if total > max_bytes:
                    response.close()
                    return _result("too_large", started, url=current, bytes=total)
                if time.monotonic() - started > total_timeout_s:
                    response.close()
                    return _result("timeout", started, url=current, bytes=total)
                chunks.append(chunk)
            data = b"".join(chunks)
            content_type = response.headers.get("Content-Type", "")
            response.close()
            if expect_pdf and data[:5] != b"%PDF-":
                return _result("not_pdf", started, url=current, bytes=len(data), content_type=content_type)
            return _result("ok", started, url=current, data=data, bytes=len(data), content_type=content_type)
        except requests.exceptions.Timeout:
            return _result("timeout", started, url=current)
        except requests.exceptions.RequestException as error:
            return _result("inaccessible", started, url=current, detail=type(error).__name__)
        except Exception as error:
            return _result("inaccessible", started, url=current, detail=type(error).__name__)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
