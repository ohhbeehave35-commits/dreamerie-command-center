"""Every nav menu item in the desktop UI must actually be wired to a handler.

"Settings is not responding" (Susan, 28 Jul 2026): the top-nav dropdowns
defined twelve panel links but only five had click handlers -- a menu
restructure truncated the hand-written wiring list, leaving Settings, History,
Results, Chats, Flow, Pending, Schedule, Artifacts and SEO as href="#" links
that did NOTHING when clicked. No error, no console line, no beacon event --
an unwired anchor is silent by construction, which is why this needs a test
instead of monitoring.
"""

import re
import pathlib

INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


def _html() -> str:
    return INDEX.read_text(encoding="utf-8", errors="replace")


def test_every_nav_id_in_the_markup_is_wired():
    html = _html()
    nav_ids = set(re.findall(r'id="(nav[A-Za-z0-9]+)"', html))
    assert len(nav_ids) >= 10, f"markup restructure? only found {sorted(nav_ids)}"
    unwired = []
    for nid in sorted(nav_ids):
        directly = f"{nid}.addEventListener" in html
        by_lookup = f"getElementById('{nid}').addEventListener" in html
        in_loop = f"['{nid}'," in html  # the data-driven wiring list
        if not (directly or by_lookup or in_loop):
            unwired.append(nid)
    assert not unwired, (
        f"menu items with NO click handler -- they will silently do nothing: {unwired}"
    )


def test_no_panel_is_toggled_by_two_handlers():
    """Two handlers on one menu item toggle the panel twice per click --
    open+close, indistinguishable from dead. The data-driven list is the one
    source of wiring; no nav id may ALSO have a standalone handler."""
    html = _html()
    loop_ids = set(re.findall(r"\['(nav[A-Za-z0-9]+)',\s*panel", html))
    assert loop_ids, "the data-driven wiring list is gone -- was it refactored away?"
    doubled = [nid for nid in loop_ids
               if f"{nid}.addEventListener" in html
               or f"getElementById('{nid}').addEventListener" in html]
    assert not doubled, f"wired both in the list AND standalone (double-toggle): {doubled}"


def test_settings_panel_and_renderer_still_exist():
    html = _html()
    assert 'id="panelSettings"' in html
    assert "function renderSettings" in html or "renderSettings = " in html or "renderSettings()" in html
    assert "['navSettings', panelSettings]" in html.replace('"', "'"), \
        "navSettings is not mapped to panelSettings in the wiring list"
