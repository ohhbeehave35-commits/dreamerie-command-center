"""Single-use set-password links: works once, expires, refused after use, wrong
token refused; Annabelle's add-user / reset-password draft them, email only the
user's own address, and never show the link to the model."""

import types

import pytest

from app import annabelle_gaps as ag
from app import invite_links as il


class Fake:
    def __init__(self, monkeypatch):
        self.settings = {}
        self.users = {"nick": {"username": "nick", "email": "nick@x.com", "role": "staff"},
                      "nomail": {"username": "nomail", "email": "", "role": "staff"}}
        self.pw = []
        self.mail = []
        self.mail_ok = True
        m = monkeypatch
        m.setattr(il.crm, "set_setting", lambda k, v, sync=False: self.settings.__setitem__(k, v) or True)
        m.setattr(il.crm, "get_setting", lambda k, d="": self.settings.get(k, d))
        m.setattr(il.users, "update_user_password", lambda n, p: self.pw.append((n, p)) or True)
        m.setattr(ag.users, "get_user", lambda n: dict(self.users[n]) if n in self.users else None)
        m.setattr(ag.users, "list_users", lambda: [dict(u) for u in self.users.values()])
        m.setattr(ag.users, "add_user", lambda n, e, p, r: self.users.__setitem__(n, {"username": n, "email": e, "role": r}) or (True, ""))
        m.setattr(ag.users, "delete_user", lambda n: self.users.pop(n, None) is not None)
        ag.configure(
            send_email=lambda to, subj, body: self._send(to, subj, body),
            site_base=lambda: "https://site.example",
            after_user_added=lambda n: None,
        )
        il._burned.clear()
        ag.reset_state()

    def _send(self, to, subj, body):
        self.mail.append((to, subj, body))
        return f"Sent to {to} from x." if self.mail_ok else "Couldn't send that email: boom"


@pytest.fixture
def f(monkeypatch):
    return Fake(monkeypatch)


def test_stores_only_a_hash_never_the_token(f):
    tok = il.create("nick", "reset")
    assert all(tok not in k and tok not in v for k, v in f.settings.items())
    assert len(f.settings) == 1


def test_link_works_once_and_user_sets_own_password(f):
    tok = il.create("nick", "invite")
    ok, _ = il.consume(tok, "my-own-pass9")
    assert ok and f.pw == [("nick", "my-own-pass9")]
    ok2, why = il.consume(tok, "another-pass9")
    assert not ok2 and "already been used" in why
    assert f.pw == [("nick", "my-own-pass9")]  # second use changed nothing
    assert il.check(tok)[0] is False


def test_link_expires_after_24_hours(f):
    tok = il.create("nick", "reset", now=1000.0)
    assert il.check(tok, now=1000.0 + 24 * 3600 - 1)[0] is True
    ok, why = il.consume(tok, "my-own-pass9", now=1000.0 + 24 * 3600 + 1)
    assert not ok and "expired" in why and f.pw == []


def test_wrong_or_empty_token_refused(f):
    il.create("nick", "reset")
    for bad in ("", "short", "x" * 43):
        ok, _ = il.consume(bad, "my-own-pass9")
        assert not ok
    assert f.pw == []


def test_weak_password_refused_and_link_stays_usable(f):
    tok = il.create("nick", "reset")
    ok, why = il.consume(tok, "short")
    assert not ok and "8 characters" in why
    ok, _ = il.consume(tok, "no-digit-here")  # has symbol, passes app rule
    assert ok


def test_add_user_drafts_then_emails_only_that_address_and_model_never_sees_link(f):
    out = ag.run("add_team_user", {"username": "amy", "email": "amy@x.com", "role": "staff"}, "c", "t1")
    assert out.startswith("DRAFT") and "amy@x.com" in out
    assert "amy" not in f.users and f.mail == [] and f.settings == {}
    did = out.split("DRAFT ", 1)[1].split(":", 1)[0]
    assert "same turn" in ag.run("confirm_action", {"draft_id": did}, "c", "t1")
    assert f.mail == []
    res = ag.run("confirm_action", {"draft_id": did}, "c", "t2")
    assert "amy" in f.users and len(f.mail) == 1
    to, _subj, body = f.mail[0]
    assert to == "amy@x.com" and "https://site.example/set-password?token=" in body
    token = body.split("token=")[1].split()[0]
    assert token not in res and "http" not in res and "token=" not in res
    assert il.check(token)[0] is True


def test_reset_uses_the_users_own_address_on_file_not_one_given(f):
    out = ag.run("reset_team_user_password", {"username": "nick", "email": "evil@x.com"}, "c", "t1")
    assert out.startswith("DRAFT") and "nick@x.com" in out and "evil@x.com" not in out
    did = out.split("DRAFT ", 1)[1].split(":", 1)[0]
    res = ag.run("confirm_action", {"draft_id": did}, "c", "t2")
    assert [m[0] for m in f.mail] == ["nick@x.com"]
    assert f.mail[0][2].split("token=")[1].split()[0] not in res
    assert f.pw == []  # nothing changes until the person uses the link


def test_reset_refused_without_email_and_unknown_user(f):
    assert "no email" in ag.run("reset_team_user_password", {"username": "nomail"}, "c", "t1")
    assert "No login named" in ag.run("reset_team_user_password", {"username": "ghost"}, "c", "t1")
    assert not ag._pending and f.mail == []


def test_failed_email_undoes_new_login_and_burns_link(f):
    f.mail_ok = False
    out = ag.run("add_team_user", {"username": "amy", "email": "amy@x.com"}, "c", "t1")
    did = out.split("DRAFT ", 1)[1].split(":", 1)[0]
    res = ag.run("confirm_action", {"draft_id": did}, "c", "t2")
    assert "undid" in res and "amy" not in f.users
    assert all(v == "" for v in f.settings.values())


# ---- through the real gate ---------------------------------------------------

def test_set_password_routes_work_through_the_gate_without_a_session(f, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    m._unlock_attempts.clear()
    c = TestClient(m.app, base_url="https://testserver")
    assert c.get("/set-password").status_code == 200
    tok = il.create("nick", "reset")
    assert c.get("/api/set-password/check", params={"token": tok}).status_code == 200
    r = c.post("/api/set-password", json={"token": tok, "password": "my-own-pass9"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert f.pw == [("nick", "my-own-pass9")]
    r2 = c.post("/api/set-password", json={"token": tok, "password": "my-own-pass9"})
    assert r2.status_code == 400 and f.pw == [("nick", "my-own-pass9")]
    assert c.post("/api/set-password", json={"token": "x" * 43, "password": "my-own-pass9"}).status_code == 400
    m._unlock_attempts.clear()
