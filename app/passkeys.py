"""Passkey (WebAuthn) credential storage for the access-code deployment.

Susan signs in with a shared access code; a passkey is a per-DEVICE
fingerprint/Face-ID credential that unlocks the same door. This module only
stores and retrieves credentials -- all protocol verification lives in
py_webauthn, and the endpoints in main.py glue the two together.

Storage is a dedicated Airtable table (NOT Settings keys) because:
  - the auth path needs FRESH reads -- the Settings snapshot cache is 30s
    stale by design, and a passkey registered seconds ago must work on the
    very next sign-in;
  - sign-count updates and device removals are per-record PATCH/DELETE.

The one scalar that DOES live in Settings is `passkey_user_handle` -- the
stable WebAuthn user identity every device enrolls under (same lazy-create
pattern as the session secret).

Failure honesty: an Airtable outage raises PasskeyStoreUnavailable, mirroring
users.UserLookupUnavailable, so the endpoints can answer 503 "use your access
code" instead of "fingerprint rejected" -- the exact lesson from the login
outage bug ([auth-gate 28 Jul], fix 3).
"""

import base64
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from . import crm

PASSKEYS_TABLE = "Passkeys"
_table_id_cache: Optional[str] = None

USER_HANDLE_KEY = "passkey_user_handle"

# The lock page polls /api/passkey/enabled unauthenticated; this tiny cache
# bounds both the Airtable load and what an unauthenticated prober can learn
# to one probe a minute.
_ENABLED_TTL = 60.0
_enabled_cache: Dict[str, tuple] = {}  # rp_id -> (bool, checked_at)
_enabled_lock = threading.Lock()


class PasskeyStoreUnavailable(Exception):
    """The credential store could not be reached. Distinct from 'no such
    credential' so an outage is never reported as a bad fingerprint."""


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_dec(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_table() -> str:
    """Return the Passkeys table id, creating the table if needed. Mirrors
    users._ensure_users_table; all fields singleLineText (typecast writes),
    per the primary-field-plain-text playbook gotcha."""
    global _table_id_cache
    if _table_id_cache:
        return _table_id_cache
    if not crm.is_configured():
        raise PasskeyStoreUnavailable("Airtable not configured")
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                      headers=crm._headers())
            r.raise_for_status()
            for t in r.json().get("tables", []):
                if t.get("name", "").lower() == PASSKEYS_TABLE.lower():
                    _table_id_cache = t["id"]
                    return _table_id_cache
            fields = [
                {"name": "CredentialID", "type": "singleLineText"},  # primary, b64url
                {"name": "PublicKey", "type": "singleLineText"},     # b64url COSE bytes
                {"name": "SignCount", "type": "singleLineText"},
                {"name": "Label", "type": "singleLineText"},
                {"name": "RpID", "type": "singleLineText"},
                {"name": "Transports", "type": "singleLineText"},    # JSON list
                {"name": "CreatedAt", "type": "singleLineText"},
                {"name": "LastUsedAt", "type": "singleLineText"},
            ]
            r = c.post(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                       headers=crm._headers(),
                       json={"name": PASSKEYS_TABLE, "fields": fields})
            r.raise_for_status()
            _table_id_cache = r.json()["id"]
            return _table_id_cache
    except PasskeyStoreUnavailable:
        raise
    except Exception as e:
        raise PasskeyStoreUnavailable(str(e)) from e


def get_user_handle() -> bytes:
    """The one stable WebAuthn user identity every device enrolls under.
    Random 16 bytes, created once, persisted in Settings."""
    stored = crm.get_setting(USER_HANDLE_KEY, "")
    if stored:
        try:
            return _b64u_dec(stored)
        except Exception:
            pass  # unreadable value -> regenerate below
    handle = secrets.token_bytes(16)
    crm.set_setting(USER_HANDLE_KEY, _b64u(handle))
    return handle


def _rows(rp_id: Optional[str] = None) -> List[dict]:
    """All credential records (optionally for one rp_id), fresh from Airtable."""
    tid = _ensure_table()
    out: List[dict] = []
    offset = None
    try:
        with httpx.Client(timeout=30) as c:
            while True:
                params: Dict[str, str] = {"pageSize": "100"}
                if offset:
                    params["offset"] = offset
                r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}",
                          headers=crm._headers(), params=params)
                r.raise_for_status()
                j = r.json()
                for rec in j.get("records", []):
                    f = rec.get("fields", {})
                    if not f.get("CredentialID"):
                        continue
                    if rp_id and f.get("RpID") != rp_id:
                        continue
                    out.append({"record_id": rec["id"], **f})
                offset = j.get("offset")
                if not offset:
                    break
    except Exception as e:
        raise PasskeyStoreUnavailable(str(e)) from e
    return out


def list_credentials(rp_id: Optional[str] = None) -> List[dict]:
    return _rows(rp_id)


def get_credential(cred_id_b64: str) -> Optional[dict]:
    for row in _rows():
        if row.get("CredentialID") == cred_id_b64:
            return row
    return None


def add_credential(cred_id_b64: str, public_key_b64: str, sign_count: int,
                   label: str, rp_id: str, transports: str) -> None:
    tid = _ensure_table()
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}",
                       headers=crm._headers(),
                       json={"fields": {
                           "CredentialID": cred_id_b64,
                           "PublicKey": public_key_b64,
                           "SignCount": str(int(sign_count)),
                           "Label": (label or "This device")[:60],
                           "RpID": rp_id,
                           "Transports": transports[:200],
                           "CreatedAt": _now_iso(),
                           "LastUsedAt": "",
                       }, "typecast": True})
            r.raise_for_status()
    except Exception as e:
        raise PasskeyStoreUnavailable(str(e)) from e
    _invalidate_enabled_cache()


def touch(record_id: str, new_sign_count: int) -> None:
    """Update sign count + last-used after a successful authentication.
    Best-effort: a failed bookkeeping write must not fail the sign-in."""
    try:
        tid = _ensure_table()
        with httpx.Client(timeout=15) as c:
            c.patch(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}/{record_id}",
                    headers=crm._headers(),
                    json={"fields": {"SignCount": str(int(new_sign_count)),
                                     "LastUsedAt": _now_iso()},
                          "typecast": True})
    except Exception:
        pass


def delete_credential(cred_id_b64: str) -> bool:
    row = get_credential(cred_id_b64)
    if not row:
        return False
    tid = _ensure_table()
    try:
        with httpx.Client(timeout=30) as c:
            r = c.delete(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}/{row['record_id']}",
                         headers=crm._headers())
            r.raise_for_status()
    except Exception as e:
        raise PasskeyStoreUnavailable(str(e)) from e
    _invalidate_enabled_cache()
    return True


def _invalidate_enabled_cache() -> None:
    with _enabled_lock:
        _enabled_cache.clear()


def enabled_cached(rp_id: str) -> bool:
    """Does at least one credential exist for this rp_id? Cached ~60s.
    Returns False (never raises) on store trouble -- the lock page must render
    either way, and 'no fingerprint button' is the safe degradation."""
    now = time.time()
    with _enabled_lock:
        hit = _enabled_cache.get(rp_id)
        if hit and now - hit[1] < _ENABLED_TTL:
            return hit[0]
    try:
        val = bool(_rows(rp_id))
    except Exception:
        return False
    with _enabled_lock:
        _enabled_cache[rp_id] = (val, now)
    return val


def reset_for_tests() -> None:
    global _table_id_cache
    _table_id_cache = None
    _invalidate_enabled_cache()
