"""Technical SEO audit -- a real, checklist-based read of a site's on-page
signals (title/description, mobile viewport, canonical, headings, image alt
coverage, structured data, robots.txt/sitemap, HTTPS, response time).

This exists because the only "SEO" ground-truth Annabelle had before was a
single LLM-eyeballed line inside research_prospect (scrape the page, ask the
model to describe what it saw). That's fine as a vibe check but isn't an
audit -- nothing here is inferred, every line below is a specific tag or
file that was actually checked."""

import re
import time
from urllib.parse import urljoin, urlparse

from . import webfetch

_MAX_BYTES = 900_000


def _find(pattern: str, html: str, flags=re.I | re.S):
    m = re.search(pattern, html, flags)
    return m.group(1).strip() if m else None


def _check_line(ok: bool, label: str, detail: str) -> str:
    return f"{'PASS' if ok else 'WARN'} -- {label}: {detail}"


def run_audit(url: str) -> str:
    """Fetch `url` and run a fixed checklist of on-page SEO signals. Returns
    a formatted multi-section report string (used directly as the tool's
    answer). Fetch failures return webfetch's plain-language error instead
    of raising -- a dead domain is itself a finding."""
    t0 = time.perf_counter()
    resp, err = webfetch.safe_get(url, timeout=15.0)
    if err:
        return f"SEO AUDIT FAILED for {url}\n{err}"
    load_seconds = round(time.perf_counter() - t0, 2)

    final_url = str(resp.url)
    parsed = urlparse(final_url)
    html = resp.content[:_MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")

    lines = [f"SEO AUDIT -- {final_url}", ""]

    # -- Reachability & transport ------------------------------------------
    lines.append("REACHABILITY")
    lines.append(_check_line(resp.status_code < 400, "HTTP status", str(resp.status_code)))
    lines.append(_check_line(parsed.scheme == "https", "HTTPS", parsed.scheme.upper()))
    redirected = final_url != webfetch.normalize_url(url)
    if redirected:
        lines.append(f"NOTE -- Redirected from {webfetch.normalize_url(url)} to {final_url}")
    lines.append(_check_line(load_seconds < 2.5, "Response time", f"{load_seconds}s"
                              + (" (slow -- over 2.5s hurts both rankings and visitors)" if load_seconds >= 2.5 else "")))
    lines.append("")

    # -- On-page basics -------------------------------------------------------
    title = _find(r"<title[^>]*>(.*?)</title>", html)
    title = re.sub(r"\s+", " ", title) if title else None
    desc = _find(r'<meta[^>]+name=["\']description["\'][^>]*content=["\'](.*?)["\']', html)
    viewport = _find(r'<meta[^>]+name=["\']viewport["\'][^>]*content=["\'](.*?)["\']', html)
    canonical = _find(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](.*?)["\']', html)
    h1s = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)

    lines.append("ON-PAGE BASICS")
    if title:
        n = len(title)
        lines.append(_check_line(10 <= n <= 60, "Title tag", f'"{title[:80]}" ({n} chars'
                                  + ("" if 10 <= n <= 60 else ", outside the ~10-60 char sweet spot for search results") + ")"))
    else:
        lines.append(_check_line(False, "Title tag", "MISSING -- every page needs one, this is the single highest-impact fix here"))
    if desc:
        n = len(desc)
        lines.append(_check_line(70 <= n <= 160, "Meta description", f'"{desc[:120]}" ({n} chars'
                                  + ("" if 70 <= n <= 160 else ", outside the ~70-160 char sweet spot") + ")"))
    else:
        lines.append(_check_line(False, "Meta description", "MISSING -- search engines will auto-generate a snippet instead of using your own words"))
    lines.append(_check_line(bool(viewport), "Mobile viewport tag", viewport or "MISSING -- page likely won't render properly on phones"))
    lines.append(_check_line(bool(canonical), "Canonical tag", canonical or "MISSING -- minor, only matters if the same content is reachable at multiple URLs"))
    lines.append(_check_line(len(h1s) == 1, "H1 headings", f"{len(h1s)} found"
                              + ("" if len(h1s) == 1 else " -- exactly one H1 is the convention; zero or multiple confuses topical relevance")))
    lines.append("")

    # -- Images ----------------------------------------------------------------
    imgs = re.findall(r"<img\b[^>]*>", html, re.I)
    imgs_with_alt = [i for i in imgs if re.search(r'alt=["\'][^"\']+["\']', i, re.I)]
    if imgs:
        pct = round(100 * len(imgs_with_alt) / len(imgs))
        lines.append("IMAGES")
        lines.append(_check_line(pct >= 80, "Alt-text coverage", f"{len(imgs_with_alt)}/{len(imgs)} images ({pct}%)"
                                  + ("" if pct >= 80 else " -- missing alt text hurts accessibility and image search")))
        lines.append("")

    # -- Structured data & social --------------------------------------------
    has_jsonld = bool(re.search(r'<script[^>]+type=["\']application/ld\+json["\']', html, re.I))
    has_og = bool(re.search(r'<meta[^>]+property=["\']og:', html, re.I))
    lines.append("STRUCTURED DATA & SOCIAL")
    lines.append(_check_line(has_jsonld, "Schema.org JSON-LD", "present" if has_jsonld else "MISSING -- local businesses benefit a lot from LocalBusiness schema (address, hours, phone) for rich search results"))
    lines.append(_check_line(has_og, "Open Graph tags", "present" if has_og else "MISSING -- links shared on Facebook/LinkedIn/iMessage will show a blank or ugly preview"))
    lines.append("")

    # -- robots.txt & sitemap --------------------------------------------------
    root = f"{parsed.scheme}://{parsed.netloc}"
    robots_resp, robots_err = webfetch.safe_get(urljoin(root, "/robots.txt"), timeout=8.0)
    lines.append("CRAWLABILITY")
    if robots_err or not robots_resp or robots_resp.status_code >= 400:
        lines.append(_check_line(True, "robots.txt", "not found -- fine, search engines default to crawling everything"))
        sitemap_url = urljoin(root, "/sitemap.xml")
    else:
        robots_txt = robots_resp.text[:5000]
        blocks_everything = bool(re.search(r"User-agent:\s*\*\s*\n\s*Disallow:\s*/\s*$", robots_txt, re.I | re.M))
        lines.append(_check_line(not blocks_everything, "robots.txt", "blocks ALL crawling with 'Disallow: /' -- this would hide the entire site from search engines" if blocks_everything else "present, not blocking the whole site"))
        m = re.search(r"Sitemap:\s*(\S+)", robots_txt, re.I)
        sitemap_url = m.group(1) if m else urljoin(root, "/sitemap.xml")
    sm_resp, sm_err = webfetch.safe_get(sitemap_url, timeout=8.0)
    sitemap_ok = not sm_err and sm_resp and sm_resp.status_code < 400
    lines.append(_check_line(sitemap_ok, "XML sitemap", sitemap_url if sitemap_ok else f"not found at {sitemap_url} -- not fatal, but a sitemap helps search engines find every page faster"))

    return "\n".join(lines)
