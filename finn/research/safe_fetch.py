"""Outbound fetching with guards.

Rules:
  * https only, port 443 only, no user:password in the URL
  * host must be in TRUSTED_HOSTS (exact match, no wildcards), or - only when the caller passes company_domain -
    be that domain or a subdomain of it. The caller derives company_domain from the company's own website listing
    (see hosts.py); it never comes from the user
  * every redirect hop is re-checked against the same rules
  * the host name must resolve only to public IP addresses, checked before connecting and again for the address
    actually connected to (so a DNS answer that changes between the check and the connection is caught)
  * download size and total time are capped
  * a PDF must start with the bytes %PDF-
The URL always comes from our own code or from a trusted page we fetched; never from the user.
"""

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util import connection as urllib3_connection

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


def host_under(host, domain):
    return bool(domain) and (host == domain or host.endswith("." + domain))


def check_url(url, company_domain=None):
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
    if host in TRUSTED_HOSTS or host_under(host, company_domain):
        return True, None
    return False, "host_not_trusted"


def default_resolver(host):
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    addresses = []
    for info in infos:
        addresses.append(info[4][0])
    return addresses


def public_addresses(host, resolver=None):
    """All addresses the name resolves to, or None if there are none or any of them is not a public address
    (private, loopback, link-local, multicast, reserved, unspecified, carrier-grade NAT ...)."""
    resolver = resolver or default_resolver
    try:
        addresses = resolver(host)
    except Exception:
        return None
    if len(addresses) == 0:
        return None
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return None
        if not ip.is_global:
            return None
    return list(addresses)


def resolves_public(host, resolver=None):
    return public_addresses(host, resolver) is not None


class NotPublicAddress(NewConnectionError):
    """Raised at connection time when the name resolves to a non-public address."""


class PinnedHTTPSConnection(HTTPSConnection):
    """Opens the socket to an address that was checked to be public at the moment of connecting.
    TLS still verifies the certificate against the host name, because only the socket target changes."""
    resolver = None

    def _new_conn(self):
        resolver = type(self).resolver
        addresses = public_addresses(self._dns_host, resolver)
        if addresses is None:
            raise NotPublicAddress(self, "dns_not_public")
        last_error = None
        for address in addresses:
            try:
                return urllib3_connection.create_connection((address, self.port), self.timeout,
                                                            source_address=self.source_address,
                                                            socket_options=self.socket_options)
            except OSError as error:
                last_error = error
        raise NewConnectionError(self, "could not connect: " + type(last_error).__name__)


class PinnedAdapter(HTTPAdapter):
    def __init__(self, resolver=None, **kwargs):
        self._resolver = resolver
        HTTPAdapter.__init__(self, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        HTTPAdapter.init_poolmanager(self, *args, **kwargs)
        attributes = {"resolver": staticmethod(self._resolver) if self._resolver else None}
        connection_class = type("PinnedConnection", (PinnedHTTPSConnection,), attributes)
        pool_class = type("PinnedPool", (HTTPSConnectionPool,), {"ConnectionCls": connection_class})
        classes = dict(self.poolmanager.pool_classes_by_scheme)
        classes["https"] = pool_class
        self.poolmanager.pool_classes_by_scheme = classes


def make_session(resolver=None):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    session.mount("https://", PinnedAdapter(resolver=resolver))
    return session


def _result(state, started, **extra):
    info = {"ok": state == "ok", "state": state, "seconds": round(time.monotonic() - started, 2)}
    info.update(extra)
    return info


def fetch(url, session=None, max_bytes=35000000, total_timeout_s=60.0, expect_pdf=False, read_timeout_s=20.0,
          resolver=None, referer=None, company_domain=None):
    """Download one URL under all the guards. Never raises. Returns a dict with state.

    states: ok, bad_url, host_not_trusted, dns_not_public, not_found, inaccessible_http,
            timeout, too_large, not_pdf, inaccessible, too_many_redirects
    """
    started = time.monotonic()
    session = session or make_session(resolver)
    current = url
    redirects = 0
    while True:
        ok, code = check_url(current, company_domain)
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
                                   timeout=(5, min(read_timeout_s, remaining)), headers=headers)
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
            if "dns_not_public" in str(error):
                return _result("dns_not_public", started, url=current, host=host_of(current))
            return _result("inaccessible", started, url=current, detail=type(error).__name__)
        except Exception as error:
            return _result("inaccessible", started, url=current, detail=type(error).__name__)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
