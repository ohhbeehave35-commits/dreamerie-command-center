"""Per-brand sending identity: prove the wall actually holds.

The bug these guard against was silent. Both Bear Arms mode and Stinger mode
read one global `gmail_address`, so a send from either went out under the same
brand with no error anywhere -- nothing failed, it was just wrong.

So these tests assert on the RESOLVED IDENTITY, not on "did it return ok".
A test that only checked ok=True would have passed against the old code.
"""

import pytest

from app import brand_identity, emailer


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No ambient Airtable settings and no ambient env credentials."""
    monkeypatch.setattr(brand_identity.crm, "get_setting", lambda k, d="": d)
    for var in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
                "GMAIL_ADDRESS__BEAR_ARMS", "GMAIL_APP_PASSWORD__BEAR_ARMS",
                "GMAIL_ADDRESS__DREAMERIE", "GMAIL_APP_PASSWORD__DREAMERIE",
                "GOOGLE_CALENDAR_ID__BEAR_ARMS", "GOOGLE_CALENDAR_ID__DREAMERIE"):
        monkeypatch.delenv(var, raising=False)


def _settings(monkeypatch, mapping):
    monkeypatch.setattr(brand_identity.crm, "get_setting",
                        lambda k, d="", _m=mapping: _m.get(k, d))


# --- the actual bleed ------------------------------------------------------

def test_each_brand_resolves_to_its_own_address(monkeypatch):
    """The regression test for the original bug: two brands, two From lines."""
    _settings(monkeypatch, {
        "gmail_address__bear_arms": "hello@beararms.com",
        "gmail_address__dreamerie": "vinny@dreamerieindustries.com",
        "gmail_app_password": "sharedapppassword",
        "gmail_address": "vinny@dreamerieindustries.com",
    })
    beehave = brand_identity.resolve_email("bear_arms")
    dreamerie = brand_identity.resolve_email("dreamerie")
    assert beehave["from"] == "hello@beararms.com"
    assert dreamerie["from"] == "vinny@dreamerieindustries.com"
    assert beehave["from"] != dreamerie["from"], "the whole point: brands must not share a From"


def test_unconfigured_brand_refuses_instead_of_falling_back(monkeypatch):
    """Bear Arms has no address; the main account is Stinger's.

    Falling back here is exactly the bleed. Refusing is the correct answer.
    """
    _settings(monkeypatch, {
        "gmail_address": "vinny@dreamerieindustries.com",
        "gmail_app_password": "sharedapppassword",
    })
    r = brand_identity.resolve_email("bear_arms")
    assert r["ok"] is False
    assert "dreamerieindustries" not in r["error"], "must not leak/settle on the other brand"
    assert "Bear Arms" in r["error"] and "gmail_address__bear_arms" in r["error"]


def test_send_email_refuses_for_unconfigured_brand(monkeypatch):
    """End of the wire: the tool result Annabelle reads back must be a refusal,
    and no SMTP connection may be attempted at all."""
    _settings(monkeypatch, {
        "gmail_address": "vinny@dreamerieindustries.com",
        "gmail_app_password": "sharedapppassword",
    })

    def _boom(*a, **k):
        raise AssertionError("SMTP was contacted for an unconfigured brand")

    monkeypatch.setattr(emailer.smtplib, "SMTP_SSL", _boom)
    out = emailer.send_email("client@example.com", "Hi", "Body", business="bear_arms")
    assert "doesn't have its own sending address" in out


def test_combined_mode_refuses_and_asks_which_brand(monkeypatch):
    _settings(monkeypatch, {
        "gmail_address__bear_arms": "hello@beararms.com",
        "gmail_address__dreamerie": "vinny@dreamerieindustries.com",
        "gmail_app_password": "sharedapppassword",
        "gmail_address": "vinny@dreamerieindustries.com",
    })
    r = brand_identity.resolve_email("combined")
    assert r["ok"] is False
    assert "Bear Arms" in r["error"] and "NS Peptides" in r["error"]


# --- the two supported setups ---------------------------------------------

def test_alias_mode_shares_login_but_not_from(monkeypatch):
    _settings(monkeypatch, {
        "gmail_address__bear_arms": "hello@beararms.com",
        "gmail_address": "vinny@dreamerieindustries.com",
        "gmail_app_password": "sharedapppassword",
    })
    r = brand_identity.resolve_email("bear_arms")
    assert r["ok"] and r["alias"] is True
    assert r["from"] == "hello@beararms.com"
    assert r["login"] == "vinny@dreamerieindustries.com"


def test_own_account_mode_uses_its_own_login(monkeypatch):
    """Filling in one brand's password moves just that brand to its own
    account -- the documented upgrade path, with no code change."""
    _settings(monkeypatch, {
        "gmail_address__bear_arms": "hello@beararms.com",
        "gmail_app_password__bear_arms": "beararmsownpassword",
        "gmail_address": "vinny@dreamerieindustries.com",
        "gmail_app_password": "sharedapppassword",
    })
    r = brand_identity.resolve_email("bear_arms")
    assert r["ok"] and r["alias"] is False
    assert r["login"] == "hello@beararms.com"
    assert r["password"] == "beararmsownpassword"


def test_alias_send_warns_about_silent_gmail_rewrite(monkeypatch):
    """Gmail rewrites an unverified From and still reports success. We can't
    detect that over SMTP, so the confirmation has to say it out loud."""
    _settings(monkeypatch, {
        "gmail_address__bear_arms": "hello@beararms.com",
        "gmail_address": "vinny@dreamerieindustries.com",
        "gmail_app_password": "sharedapppassword",
    })

    class _FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def sendmail(self, *a):
            pass

    monkeypatch.setattr(emailer.smtplib, "SMTP_SSL", lambda *a, **k: _FakeSMTP())
    out = emailer.send_email("client@example.com", "Hi", "Body", business="bear_arms")
    assert "hello@beararms.com" in out
    assert "Send mail as" in out


# --- calendar --------------------------------------------------------------

def test_calendar_is_per_brand_and_empty_when_unset(monkeypatch):
    """Empty must stay empty. Defaulting to "primary" would quietly write one
    brand's events onto the other's calendar."""
    _settings(monkeypatch, {"google_calendar_id__dreamerie": "dreamerie-cal-id"})
    assert brand_identity.resolve_calendar_id("dreamerie") == "dreamerie-cal-id"
    assert brand_identity.resolve_calendar_id("bear_arms") == ""


def test_status_never_returns_a_password(monkeypatch):
    _settings(monkeypatch, {
        "gmail_address__bear_arms": "hello@beararms.com",
        "gmail_app_password__bear_arms": "supersecretvalue",
    })
    blob = repr(brand_identity.status())
    assert "supersecretvalue" not in blob


def test_no_business_keeps_old_single_identity_behaviour(monkeypatch):
    """The public widget and every non-mode caller must be unaffected."""
    _settings(monkeypatch, {})
    monkeypatch.setattr(emailer, "get_gmail_address", lambda: "")
    monkeypatch.setattr(emailer, "get_gmail_app_password", lambda: "")
    out = emailer.send_email("client@example.com", "Hi", "Body")
    assert "isn't connected yet" in out
