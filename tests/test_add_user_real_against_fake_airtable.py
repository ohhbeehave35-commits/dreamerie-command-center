"""The test the ORIGINAL Add User test should have been.

The shipped test monkeypatched users.add_user itself and asserted the route
returned 200 -- stubbing the exact save that was failing, so it could never
catch the bug. These run the REAL add_user against a fake Airtable that
enforces the one rule that broke it: a record-create naming a column the table
doesn't have returns 422 and does NOT add the column.

So the missing-migration bug REPRODUCES here (an old-shaped Users table
rejects every insert) and the fix (migrate on the existing-table branch) is
what makes it pass. Nothing under test is mocked.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.users as users  # noqa: E402
from tests.fake_airtable import FakeBase, install  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_caches():
    """The table-id caches persist across calls; clear them per test so each
    starts against a fresh fake base."""
    users._users_table_id_cache = None
    yield
    users._users_table_id_cache = None


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(users.crm, "is_configured", lambda: True)
    monkeypatch.setattr(users.crm, "AIRTABLE_BASE_ID", "appFAKE")
    monkeypatch.setattr(users.crm, "_headers", lambda: {"Authorization": "Bearer x"})
    monkeypatch.setattr(users.crm, "_API", "https://api.airtable.com")
    # _ensure_field lives in crm and is called by the migration; it uses crm.httpx
    monkeypatch.setattr(users.crm, "_ensure_field",
                        users.crm._ensure_field)  # keep the real one


def test_add_user_succeeds_on_a_fresh_base(configured, monkeypatch):
    """No Users table yet -> create it with the full schema -> insert works."""
    base = FakeBase()
    install(monkeypatch, users, base)
    monkeypatch.setattr(users.crm, "httpx", users.httpx)  # share the fake

    ok, reason = users.add_user("nick", "nick@example.com", "a-strong-pass-1", "owner")
    assert ok is True, reason
    assert reason == ""
    # the row really landed
    utable = base.tables["users"]
    assert any(r.get("Username") == "nick" for r in utable["records"].values())


def test_the_missing_migration_bug_reproduces_then_the_fix_resolves_it(configured, monkeypatch):
    """THE bug, caught honestly.

    Simulate a Users table an OLDER deploy created WITHOUT the columns the code
    now writes. Against that table, add_user must fail with a real 422 -- and
    because the fix migrates the missing columns onto the existing table first,
    it must then succeed.
    """
    base = FakeBase()
    # older deploy: table exists but is missing Email, PasswordHash, Role, etc.
    base.seed_table("Users", ["Username"])
    install(monkeypatch, users, base)
    monkeypatch.setattr(users.crm, "httpx", users.httpx)

    ok, reason = users.add_user("nick", "nick@example.com", "a-strong-pass-1", "owner")

    # With the migration fix, the columns are added first and the insert lands.
    assert ok is True, (
        "add_user failed against an old-shaped table -- the migration on the "
        f"existing-table branch is missing or broken. Reason: {reason}"
    )
    # and the columns really got migrated onto the live table
    assert "PasswordHash" in base.tables["users"]["fields"]
    assert "Email" in base.tables["users"]["fields"]


def test_without_migration_the_bug_would_surface_as_a_real_422(configured, monkeypatch):
    """Prove the fake actually enforces the rule -- otherwise the test above
    passes for the wrong reason. Bypass the migration and confirm the 422."""
    base = FakeBase()
    tid = base.seed_table("Users", ["Username"])
    install(monkeypatch, users, base)
    monkeypatch.setattr(users.crm, "httpx", users.httpx)
    # force the un-migrated table id straight into the cache, so add_user skips
    # _ensure_users_table's migration entirely
    users._users_table_id_cache = tid

    ok, reason = users.add_user("nick", "nick@example.com", "a-strong-pass-1", "owner")
    assert ok is False, "an insert of unknown columns should have been rejected"
    assert "422" in reason and "Unknown field" in reason, reason


def test_a_duplicate_user_is_refused_specifically(configured, monkeypatch):
    base = FakeBase()
    install(monkeypatch, users, base)
    monkeypatch.setattr(users.crm, "httpx", users.httpx)

    ok, _ = users.add_user("nick", "nick@example.com", "a-strong-pass-1")
    assert ok
    ok2, reason2 = users.add_user("nick", "other@example.com", "another-pass-2")
    assert ok2 is False and "already exists" in reason2
