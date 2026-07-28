"""Shared safe-fetch helper: SSRF-guarded HTTP GET used by any tool that reads
a URL a user or prospect supplied (scrape_page, run_seo_audit, ...). One place
for the private-IP/DNS/cert-error handling so every caller gets the same
guarantees instead of re-implementing them slightly differently each time."""

import ipaddress
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StingerResearch/1.0)"}


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def safe_get(url: str, timeout: float = 12.0) -> Tuple[Optional[httpx.Response], Optional[str]]:
    """Fetch `url` after confirming it doesn't resolve to a private/internal
    address. Returns (response, None) on success or (None, human-readable
    error) on failure -- callers report the error text directly rather than
    raising, since a dead domain or bad cert is itself a finding worth
    telling the user about."""
    url = normalize_url(url)
    host = urlparse(url).hostname or ""
    if not host:
        return None, "That URL doesn't look valid -- give me a full address like https://example.com."
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None, (
            f"DNS FAILURE: {host} doesn't resolve at all right now. If this is a "
            "business's website, their site is effectively down -- that's worth reporting."
        )
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return None, f"I can't fetch {host} -- it resolves to an internal address."
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=DEFAULT_HEADERS)
    except httpx.ConnectError as e:
        msg = str(e)
        if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper():
            return None, (
                f"SSL/CERTIFICATE ERROR fetching {url}: {msg[:200]}. Visitors likely "
                "see a browser security warning on this site -- a valuable finding."
            )
        return None, f"Couldn't connect to {url}: {msg[:200]}. The site may be down or blocking requests."
    except httpx.HTTPError as e:
        return None, f"Couldn't fetch {url}: {e.__class__.__name__}: {str(e)[:200]}"
    return resp, None
