"""Director order, 14 Sep 2026: this dashboard had NO relaunch-to-update
mechanism at all (unlike the Stinger Command Center console, which had the
script but forgot to wire it in). Ported static/relaunch-pill.js here and
wired the "/" route to stamp the loaded rev, so a stale deploy can be
noticed -- Susan has no other signal a new build shipped."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index_serves_relaunch_pill_script():
    response = client.get("/")
    assert response.status_code == 200
    assert '<script src="/static/relaunch-pill.js"></script>' in response.text, (
        "the dashboard never loads relaunch-pill.js -- no way to tell Susan "
        "a newer build is ready")


def test_index_stamps_the_loaded_rev_for_relaunch_pill_to_compare():
    """relaunch-pill.js reads window.__CC_LOADED_REV from the cc-loaded-rev
    meta tag. If '/' is served as a raw static file (FileResponse) instead
    of being read and templated, the placeholder is never substituted and
    the banner's newer-build comparison is permanently broken."""
    response = client.get("/")
    assert response.status_code == 200
    assert "__CC_LOADED_REV__" not in response.text, (
        "the rev placeholder was never substituted -- relaunch-pill.js would "
        "compare the literal placeholder string against /healthz forever")
    assert 'meta name="cc-loaded-rev"' in response.text
