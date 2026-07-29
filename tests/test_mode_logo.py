"""The top-left logo must follow the selected business tab.

Requested 28 Jul 2026: switching to Bear Arms / NS Peptides should swap the
topbar mark the same way the watermark already swaps, so the workspace visibly
becomes that company. Modes without their own art (Suzy D) fall back to the
owner's custom logo or the house logo -- never another brand's mark.
"""

import re
import pathlib

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8", errors="replace")


def test_mode_logo_map_exists_and_every_file_is_real():
    html = _html()
    m = re.search(r"const MODE_LOGO = \{(.*?)\};", html, re.S)
    assert m, "MODE_LOGO map is gone -- the per-tab logo has no source of truth"
    paths = re.findall(r"'/static/([^']+)'", m.group(1))
    assert paths, "MODE_LOGO is empty"
    for p in paths:
        assert (STATIC / p).is_file(), f"MODE_LOGO points at a missing file: {p} (404 logo)"


def test_mode_switch_applies_the_logo():
    html = _html()
    assert "function applyModeLogo" in html
    styles = re.search(r"function applyModeStyles\(\) \{(.*?)\n  \}", html, re.S)
    assert styles and "applyModeLogo()" in styles.group(1), \
        "applyModeStyles no longer applies the logo -- tab clicks won't swap it"


def test_brand_fetch_cannot_stomp_the_mode_logo():
    """applyBrand used to set logoTopbar.src directly, which would overwrite
    the per-tab mark on every brand fetch. It must route through
    applyModeLogo() instead."""
    html = _html()
    brand = re.search(r"function applyBrand\(data\) \{(.*?)\n  \}", html, re.S)
    assert brand, "applyBrand refactored away?"
    body = brand.group(1)
    assert "logoT.src = " not in body, "applyBrand sets the topbar logo directly again"
    assert "applyModeLogo()" in body and "brandLogoUrl" in body
