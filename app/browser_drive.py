"""
The driving layer on top of Browserbase sessions -- "the hands".

browserbase.py manages sessions (create/release) and deliberately offered NO
tool, because a browser nobody can steer is the advertised-vs-implemented gap
this codebase detects. This module is the steering: connect to the session's
CDP endpoint with Playwright, run a SHORT list of explicit steps, read the
page, and ALWAYS release the session (sessions bill by browser-minute).

Design rules:
- Steps are explicit and bounded (MAX_STEPS, per-step timeout). This is a
  tool for "open her site, click Pricing, read it", not an open-ended agent.
- The SSRF rule applies to every navigation target AND to wherever the page
  actually lands (redirects included) -- same rule as webfetch.py, no exceptions.
  The fetch runs on Browserbase's infrastructure, but "it's their network" is
  not a reason to relay private-address content back to the model.
- Every run returns the session replay URL. The owner can watch exactly what
  the hands did -- that is the honesty mechanism for this tool.
- Never raises out of drive(); every failure is a plain-language reason the
  model can relay truthfully.

Local test note: tests exercise the same code path against a local headless
Chrome CDP endpoint, so the logic is proven without spending Browserbase
minutes. The only untested-by-CI seam is Browserbase's connect URL itself,
which browserbase.py already validated live (30 Jul).
"""

import logging
from urllib.parse import urlparse

from . import browserbase, webfetch

log = logging.getLogger(__name__)

MAX_STEPS = 8
MAX_TEXT = 12000          # chars of page text handed back to the model
STEP_TIMEOUT_MS = 15000
NAV_TIMEOUT_MS = 30000

_ALLOWED = ("goto", "click", "type", "press_enter", "wait", "read")


def is_configured() -> bool:
    return browserbase.is_configured()


def _check_public(url: str):
    """(ok, reason). The one SSRF rule, applied to a full URL."""
    if not (url or "").lower().startswith(("http://", "https://")):
        return False, "that isn't an http(s) URL"
    host = urlparse(url).hostname or ""
    return webfetch.is_public_host(host)


def _validate_steps(steps) -> tuple:
    """(ok, cleaned_or_reason). Shape-check before any browser minute is spent."""
    if steps is None:
        steps = []
    if not isinstance(steps, list):
        return False, "steps must be a list"
    if len(steps) > MAX_STEPS:
        return False, f"too many steps ({len(steps)}); the cap is {MAX_STEPS}"
    cleaned = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or (s.get("do") or "") not in _ALLOWED:
            return False, (f"step {i + 1} is invalid -- each step needs a 'do' of: "
                           + ", ".join(_ALLOWED))
        do = s["do"]
        if do == "goto":
            ok, why = _check_public(s.get("url", ""))
            if not ok:
                return False, f"step {i + 1} refused: {why}"
        if do in ("click", "type") and not (s.get("target") or "").strip():
            return False, f"step {i + 1} ({do}) needs a 'target' (visible text or CSS selector)"
        if do == "type" and s.get("text") is None:
            return False, f"step {i + 1} (type) needs 'text'"
        if do == "wait":
            try:
                secs = float(s.get("seconds", 1))
            except (TypeError, ValueError):
                return False, f"step {i + 1} (wait) has a non-numeric 'seconds'"
            if not 0 < secs <= 10:
                return False, f"step {i + 1} (wait) must be 0-10 seconds"
        cleaned.append(s)
    return True, cleaned


def _locate(page, target: str):
    """A target is visible text first, CSS selector second. Returns a locator
    scoped to the FIRST match, or None."""
    t = (target or "").strip()
    # visible text (buttons, links, labels) -- what a human would say
    for finder in (
        lambda: page.get_by_role("button", name=t, exact=False),
        lambda: page.get_by_role("link", name=t, exact=False),
        lambda: page.get_by_label(t, exact=False),
        lambda: page.get_by_placeholder(t, exact=False),
        lambda: page.get_by_text(t, exact=False),
        lambda: page.locator(t),          # CSS/XPath, last
    ):
        try:
            loc = finder().first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _read_page(page) -> dict:
    title = ""
    text = ""
    try:
        title = page.title() or ""
    except Exception as e:
        log.debug("BROWSER_DRIVE_TITLE_READ_FAIL %s", type(e).__name__)
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception as e:
        log.warning("BROWSER_DRIVE_TEXT_READ_FAIL %s", type(e).__name__)
    text = " ".join(text.split())
    truncated = " ...[truncated]" if len(text) > MAX_TEXT else ""
    return {"title": title[:300], "url": page.url, "text": text[:MAX_TEXT] + truncated}


def _run_steps(page, url: str, steps: list) -> dict:
    """Drive an already-connected page. Split out so tests can run it against
    a local Chrome without a Browserbase session. Raises on hard failures;
    drive() translates."""
    page.set_default_timeout(STEP_TIMEOUT_MS)
    notes = []
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    done = 0
    for i, s in enumerate(steps):
        do = s["do"]
        try:
            if do == "goto":
                page.goto(s["url"], timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            elif do == "click":
                loc = _locate(page, s["target"])
                if loc is None:
                    notes.append(f"step {i + 1}: couldn't find {s['target']!r} to click")
                    break
                loc.click()
            elif do == "type":
                loc = _locate(page, s["target"])
                if loc is None:
                    notes.append(f"step {i + 1}: couldn't find {s['target']!r} to type into")
                    break
                loc.fill(str(s.get("text", "")))
            elif do == "press_enter":
                page.keyboard.press("Enter")
            elif do == "wait":
                page.wait_for_timeout(float(s.get("seconds", 1)) * 1000)
            elif do == "read":
                pass  # reading happens at the end regardless
            done += 1
        except Exception as e:
            notes.append(f"step {i + 1} ({do}) failed: {type(e).__name__}")
            break
    # Wherever we ended up -- including via redirects or clicks -- the landing
    # host must still pass the public-address rule before its text is relayed.
    ok, why = _check_public(page.url)
    if not ok:
        return {"title": "", "url": page.url, "text": "",
                "steps_done": done,
                "notes": notes + [f"refused to read the final page: {why}"]}
    out = _read_page(page)
    out["steps_done"] = done
    out["notes"] = notes
    return out


def drive(url: str, steps=None) -> tuple:
    """(ok, result_or_reason). result: {title, url, text, steps_done, notes,
    replay_url}. Never raises."""
    if not is_configured():
        return False, ("live browser driving isn't connected (no Browserbase "
                       "key set) -- scrape_page still reads pages without it")
    ok, why = _check_public((url or "").strip())
    if not ok:
        return False, f"refused: {why}"
    ok, cleaned = _validate_steps(steps)
    if not ok:
        return False, cleaned

    ok, sess = browserbase.create_session()
    if not ok:
        return False, sess
    replay = sess.get("replay_url", "")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(sess["connect_url"], timeout=NAV_TIMEOUT_MS)
            try:
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                result = _run_steps(page, url.strip(), cleaned)
                result["replay_url"] = replay
                return True, result
            finally:
                try:
                    browser.close()
                except Exception as e:
                    log.debug("BROWSER_DRIVE_CLOSE_FAIL %s", type(e).__name__)
    except Exception as e:
        log.warning("BROWSER_DRIVE_FAIL url=%s %s", url[:120], type(e).__name__)
        return False, (f"the browser session started but driving it failed "
                       f"({type(e).__name__}) -- the replay may show more: {replay}")
    finally:
        # Browser-minutes are money; the session dies no matter what happened.
        try:
            browserbase.release_session(sess.get("id", ""))
        except Exception as e:
            # release_session itself never raises, so reaching this means the
            # module was monkeypatched or something stranger -- say so.
            log.warning("BROWSER_DRIVE_RELEASE_FAIL sess=%s %s",
                        sess.get("id", ""), type(e).__name__)
