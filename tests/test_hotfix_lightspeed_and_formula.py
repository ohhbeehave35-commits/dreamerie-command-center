"""Regression tests for the merged dreamerie hotfix (commit bc5a037):

  1. lightspeed_connect() was missing its `request: Request` parameter, so its
     call to `_make_oauth_state(request)` raised NameError -> HTTP 500 on every
     owner Lightspeed connect. These tests prove the route now reaches its
     redirect (and never 500s on that path).

  2. crm._formula_literal() escaped apostrophes with a backslash, which Airtable
     formula string literals do NOT honor -- so the guard was both injection-
     ineffective AND broke legitimate values like `O'Brien`. The fix turns each
     apostrophe into an inert string concatenation (`' & "'" & '`). These tests
     prove, by evaluating the resulting formula's string semantics, that a real
     apostrophe value reconstructs exactly (matches correctly) and that an
     injection payload collapses to inert string content (cannot break out).
"""

import inspect

import pytest
from fastapi.testclient import TestClient

from app import main as appmain
from app import crm
from app import users


# ---------------------------------------------------------------------------
# 1. lightspeed_connect NameError regression
# ---------------------------------------------------------------------------

OWNER_CODE = "owner-shared-code"


@pytest.fixture
def owner_client(monkeypatch):
    # Activate the gate and authenticate as the owner via the shared access code.
    monkeypatch.setattr(appmain, "ACCESS_CODE", OWNER_CODE, raising=False)
    monkeypatch.setattr(appmain, "get_access_code", lambda: OWNER_CODE)
    # Deterministic signing secret so signed_tokens.mint() has no network need.
    monkeypatch.setattr(users, "get_session_secret", lambda: b"x" * 32)
    client = TestClient(appmain.app)
    client.cookies.set("cc_access", OWNER_CODE)
    return client


def test_lightspeed_connect_signature_has_request_param():
    """The exact bug: the handler must accept `request`, or _make_oauth_state
    inside it raises NameError. Guards the regression at the source."""
    params = inspect.signature(appmain.lightspeed_connect).parameters
    assert "request" in params, \
        "lightspeed_connect lost its `request` param -- NameError regression"


def test_lightspeed_connect_redirects_and_does_not_500(owner_client, monkeypatch):
    monkeypatch.setenv("LIGHTSPEED_CLIENT_ID", "test-client-id-123")
    r = owner_client.get("/lightspeed/connect", follow_redirects=False)
    assert r.status_code != 500, "lightspeed_connect 500'd (the NameError regression)"
    assert r.status_code in (302, 303, 307), \
        f"expected a redirect to Lightspeed, got {r.status_code}"
    location = r.headers.get("location", "")
    assert location.startswith(
        "https://cloud.lightspeedapp.com/auth/oauth/authorize"), location
    assert "client_id=test-client-id-123" in location
    assert "state=" in location  # the CSRF state was minted without error


def test_lightspeed_connect_missing_client_id_is_400_not_500(owner_client, monkeypatch):
    monkeypatch.delenv("LIGHTSPEED_CLIENT_ID", raising=False)
    r = owner_client.get("/lightspeed/connect", follow_redirects=False)
    assert r.status_code == 400, \
        f"missing client id should be a clean 400, got {r.status_code}"


# ---------------------------------------------------------------------------
# 2. crm._formula_literal escaping
# ---------------------------------------------------------------------------

def _eval_airtable_str(rhs: str) -> str:
    """Evaluate the STRING value of an Airtable formula right-hand side that is
    a concatenation of quoted literals, e.g.  'O' & "'" & 'Brien'.

    Airtable formula string literals honor NO escaping: a single-quoted literal
    runs to the next single quote, a double-quoted literal to the next double
    quote. This parser understands exactly that grammar plus the ``&``
    concatenation operator and whitespace -- nothing else. If any character
    appears OUTSIDE a quoted literal (a bare ``{Field}`` reference, a bare
    ``=``, a bare ``,``), it raises: that is precisely the "broke out of the
    string" condition an injection needs, and the test asserts it never happens.
    """
    out = []
    i = 0
    expect_operand = True
    n = len(rhs)
    while i < n:
        ch = rhs[i]
        if ch in " \t":
            i += 1
            continue
        if expect_operand and ch in "'\"":
            close = rhs.index(ch, i + 1)  # raises ValueError if unterminated
            out.append(rhs[i + 1:close])
            i = close + 1
            expect_operand = False
            continue
        if not expect_operand and ch == "&":
            i += 1
            expect_operand = True
            continue
        # Anything else is bare, unquoted formula content -> a breakout.
        raise AssertionError(
            f"unquoted formula content at index {i}: {rhs[i:i + 20]!r}")
    return "".join(out)


def _rhs_for(value: str) -> str:
    """Reproduce a real call site: ``{Field}='...'`` -> just the ``'...'`` RHS."""
    return "'" + crm._formula_literal(value) + "'"


def test_apostrophe_name_matches_correctly():
    # The documented example. The formula RHS must evaluate back to O'Brien.
    assert crm._formula_literal("O'Brien") == "O' & \"'\" & 'Brien"
    assert _eval_airtable_str(_rhs_for("O'Brien")) == "O'Brien"


def test_injection_payload_is_neutralised_to_inert_string():
    payload = "x',{Bad}='y"
    rhs = _rhs_for(payload)
    # The whole payload is trapped inside string literals: evaluating the RHS
    # yields the literal payload text, and NO character escapes the quotes into
    # formula logic (no bare {Bad}, no bare comparison). If it had broken out,
    # _eval_airtable_str would raise on the unquoted content.
    assert _eval_airtable_str(rhs) == payload


def test_a_value_that_is_only_a_quote_stays_a_string():
    assert _eval_airtable_str(_rhs_for("'")) == "'"
    assert _eval_airtable_str(_rhs_for("''")) == "''"


def test_ordinary_value_is_untouched_and_round_trips():
    for value in ["Dreamerie", "chat_abc123", "a=b,c", "back\\slash", "{Name}"]:
        assert _eval_airtable_str(_rhs_for(value)) == value


def test_backslash_is_left_as_an_ordinary_character():
    # Airtable does not treat backslash specially; the old code's \\' was the bug.
    assert "\\" in crm._formula_literal("O\\'Brien")
    assert _eval_airtable_str(_rhs_for("O\\'Brien")) == "O\\'Brien"
