"""add_user returns (ok, reason) -- the same contract as the sister app.

For weeks the two apps shared a function name, a docstring, and a lineage, but
had drifted to INCOMPATIBLE return types: Dreamerie returned (ok, reason) after
its Add User bug was fixed, Stinger still returned a bare bool. Any code copied
between the two repos -- which is exactly how client deployments are built,
by cloning -- would break silently: `created, why = add_user(...)` unpacking a
bool, or `if add_user(...)` treating a truthy tuple as success even on failure.

These tests pin the contract in this app. They can't reach across to the other
repo, so they assert the shape precisely enough that a regression here shows up
immediately.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.users as users  # noqa: E402


def test_returns_a_two_tuple_of_bool_and_str(monkeypatch):
    monkeypatch.setattr(users.crm, "is_configured", lambda: False)
    result = users.add_user("x", "x@x.com", "pw", "owner")
    assert isinstance(result, tuple) and len(result) == 2
    ok, reason = result
    assert isinstance(ok, bool) and isinstance(reason, str)


def test_failure_carries_a_real_reason_not_a_catch_all(monkeypatch):
    """The bug this whole thing came from: one message for every cause."""
    monkeypatch.setattr(users.crm, "is_configured", lambda: False)
    ok, reason = users.add_user("x", "x@x.com", "pw")
    assert ok is False
    assert reason and "isn't connected" in reason
    assert "already exists or error" not in reason


def test_existing_user_says_so_specifically(monkeypatch):
    monkeypatch.setattr(users.crm, "is_configured", lambda: True)
    monkeypatch.setattr(users, "get_user", lambda u: {"username": u})
    ok, reason = users.add_user("dupe", "d@d.com", "pw")
    assert ok is False and "already exists" in reason


def test_an_airtable_rejection_surfaces_the_status(monkeypatch):
    monkeypatch.setattr(users.crm, "is_configured", lambda: True)
    monkeypatch.setattr(users, "get_user", lambda u: None)
    monkeypatch.setattr(users, "_ensure_users_table", lambda: "tbl")
    monkeypatch.setattr(users, "hash_password", lambda p: "h")

    class R:
        status_code = 422
        text = 'Unknown field name: "CreatedAt"'

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return R()

    monkeypatch.setattr(users.httpx, "Client", lambda **k: C())
    ok, reason = users.add_user("nick", "n@n.com", "pw")
    assert ok is False
    assert "422" in reason and "CreatedAt" in reason


def test_success_is_true_with_an_empty_reason(monkeypatch):
    monkeypatch.setattr(users.crm, "is_configured", lambda: True)
    monkeypatch.setattr(users, "get_user", lambda u: None)
    monkeypatch.setattr(users, "_ensure_users_table", lambda: "tbl")
    monkeypatch.setattr(users, "hash_password", lambda p: "h")

    class R:
        status_code = 200
        text = ""

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return R()

    monkeypatch.setattr(users.httpx, "Client", lambda **k: C())
    ok, reason = users.add_user("nick", "n@n.com", "pw")
    assert ok is True and reason == ""


def test_no_caller_treats_the_return_as_a_bare_bool():
    """`if users.add_user(...)` on the new tuple is always truthy -- it would
    report success even on failure. Guard against a caller regressing to it."""
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8", errors="replace")
    import re
    # any `if ...add_user(...)` with no unpacking is the bug
    for m in re.finditer(r"if\s+users\.add_user\(", src):
        raise AssertionError("a caller uses `if users.add_user(...)` -- that treats the "
                             "(ok, reason) tuple as always-true and reports success on "
                             "failure. Unpack it: `created, why = users.add_user(...)`.")


def test_the_setup_bootstrap_unpacks_the_tuple():
    """/api/setup calls add_user too; it must not have been left on the old
    shape."""
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8", errors="replace")
    for line in src.splitlines():
        if "users.add_user(" in line and "def " not in line:
            assert "," in line.split("users.add_user")[0] or "=" in line.split("users.add_user")[0], \
                f"caller does not unpack the tuple: {line.strip()}"
