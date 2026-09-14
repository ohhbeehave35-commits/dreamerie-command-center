"""Director order, 14 Sep 2026 (v2): static/basic.html is Susan's actual
phone shell -- index.html auto-redirects small screens there -- so it needs
the same relaunch banner wiring as the desktop page (see
test_relaunch_banner.py), not just the desktop page alone."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_basic_shell_serves_relaunch_pill_script():
    response = client.get("/static/basic.html")
    assert response.status_code == 200
    assert '<script src="/static/relaunch-pill.js"></script>' in response.text, (
        "Susan's phone shell never loads relaunch-pill.js -- no way to tell "
        "her a newer build is ready on the page she actually uses")


def test_basic_shell_stamps_the_loaded_rev():
    """Confirms the explicit @app.get('/static/basic.html') route (registered
    before the /static mount) actually wins and substitutes the rev, rather
    than the mounted StaticFiles handler serving the raw, un-templated file."""
    response = client.get("/static/basic.html")
    assert response.status_code == 200
    assert "__CC_LOADED_REV__" not in response.text, (
        "served as a raw static file -- the mount is shadowing the "
        "templated route, or the route was never added")
    assert 'meta name="cc-loaded-rev"' in response.text
