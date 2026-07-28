"""
User authentication and session management.

Replaces the old plaintext-access-code model with:
- Hashed passwords stored in Airtable Users table
- Signed session tokens (not plaintext secrets in cookies)
- Multiple named accounts per business (owner + optional staff)
"""

import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional
import hmac
import hashlib

from passlib.context import CryptContext
import httpx

from . import crm

# Password hashing context (bcrypt via passlib)
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Session signing secret - stored in environment or Settings, should be 32+ bytes
_SESSION_SECRET = os.environ.get("SESSION_SECRET", "").encode()

# Airtable Users table constants
USERS_TABLE = "Users"
_users_table_id_cache = None


def get_session_secret() -> bytes:
    """Get or create the session signing secret. Persists in Settings for
    consistency across restarts."""
    global _SESSION_SECRET
    if _SESSION_SECRET and len(_SESSION_SECRET) >= 32:
        return _SESSION_SECRET
    # Try to load from Settings (fallback if env var not set)
    if crm.is_configured():
        stored = crm.get_setting("_session_secret")
        if stored and len(stored) >= 32:
            _SESSION_SECRET = stored.encode()
            return _SESSION_SECRET
    # Generate and store new secret if missing
    secret = secrets.token_hex(32)  # 64 chars = 32 bytes
    if crm.is_configured():
        crm.set_setting("_session_secret", secret)
    _SESSION_SECRET = secret.encode()
    return _SESSION_SECRET


def validate_username(username: str) -> tuple:
    """Return (ok: bool, reason: str). Alphanumeric + underscore/dash, 3–32 chars, no colons."""
    if not username or len(username) < 3:
        return False, "Username must be at least 3 characters."
    if len(username) > 32:
        return False, "Username must be 32 characters or fewer."
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if not all(c in allowed for c in username):
        return False, "Username may only contain letters, numbers, underscores, and dashes."
    return True, ""


def validate_password(password: str) -> tuple:
    """Return (ok: bool, reason: str). Enforces min 8 chars + at least one digit or symbol."""
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if not any(c.isdigit() or not c.isalpha() for c in password):
        return False, "Password must contain at least one number or symbol."
    return True, ""


def hash_password(password: str) -> str:
    """Hash a plaintext password."""
    return _pwd_context.hash(password)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Check if plaintext matches the hashed password."""
    return _pwd_context.verify(plaintext, hashed)


def _ensure_users_table() -> str:
    """Return the Users table id, creating it if needed."""
    global _users_table_id_cache
    if _users_table_id_cache:
        return _users_table_id_cache
    if not crm.is_configured():
        raise RuntimeError("Airtable not configured")
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                  headers=crm._headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == USERS_TABLE.lower():
                _users_table_id_cache = t["id"]
                return _users_table_id_cache
        # Create Users table
        fields = [
            {"name": "Username", "type": "singleLineText"},
            {"name": "Email", "type": "email"},
            {"name": "PasswordHash", "type": "singleLineText"},
            {"name": "Role", "type": "singleSelect", "options": {"choices": [
                {"name": "owner"}, {"name": "staff"}]}},
            {"name": "CreatedAt", "type": "singleLineText"},
            {"name": "LastLogin", "type": "singleLineText"},
        ]
        r = c.post(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                   headers=crm._headers(), json={"name": USERS_TABLE, "fields": fields})
        r.raise_for_status()
        _users_table_id_cache = r.json()["id"]
        return _users_table_id_cache


def get_user(username: str) -> Optional[dict]:
    """Fetch a user record by username. Returns dict with keys:
    username, email, password_hash, role, created_at, last_login, record_id"""
    if not crm.is_configured():
        return None
    try:
        tid = _ensure_users_table()
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}",
                      headers=crm._headers(),
                      params={"filterByFormula": "{Username}='" + username.replace("'", "") + "'",
                              "pageSize": "1"})
            r.raise_for_status()
            recs = r.json().get("records", [])
            if not recs:
                return None
            rec = recs[0]
            fields = rec.get("fields", {})
            return {
                "record_id": rec["id"],
                "username": fields.get("Username", ""),
                "email": fields.get("Email", ""),
                "password_hash": fields.get("PasswordHash", ""),
                "role": fields.get("Role", "owner"),
                "created_at": fields.get("CreatedAt", ""),
                "last_login": fields.get("LastLogin", ""),
            }
    except Exception as e:
        print(f"Error fetching user {username}: {e}")
        return None


def add_user(username: str, email: str, password: str, role: str = "owner") -> bool:
    """Create a new user. Returns True on success."""
    if not crm.is_configured():
        return False
    try:
        # Check if user exists
        if get_user(username):
            return False  # User already exists
        tid = _ensure_users_table()
        hashed = hash_password(password)
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}",
                       headers=crm._headers(),
                       json={
                           "fields": {
                               "Username": username,
                               "Email": email,
                               "PasswordHash": hashed,
                               "Role": role,
                               "CreatedAt": datetime.now(timezone.utc).isoformat(),
                           },
                           "typecast": True
                       })
            r.raise_for_status()
        return True
    except Exception as e:
        print(f"Error adding user {username}: {e}")
        return False


def update_user_password(username: str, new_password: str) -> bool:
    """Update a user's password hash. Returns True on success."""
    if not crm.is_configured():
        return False
    try:
        user = get_user(username)
        if not user:
            return False
        tid = _ensure_users_table()
        hashed = hash_password(new_password)
        with httpx.Client(timeout=30) as c:
            r = c.patch(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}/{user['record_id']}",
                        headers=crm._headers(),
                        json={"fields": {"PasswordHash": hashed}, "typecast": True})
            r.raise_for_status()
        return True
    except Exception as e:
        print(f"Error updating password for {username}: {e}")
        return False


def update_last_login(username: str) -> bool:
    """Update user's LastLogin timestamp."""
    if not crm.is_configured():
        return False
    try:
        user = get_user(username)
        if not user:
            return False
        tid = _ensure_users_table()
        now = datetime.now(timezone.utc).isoformat()
        with httpx.Client(timeout=30) as c:
            r = c.patch(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}/{user['record_id']}",
                        headers=crm._headers(),
                        json={"fields": {"LastLogin": now}, "typecast": True})
            r.raise_for_status()
        return True
    except Exception:
        return False  # Silent fail on timing updates


def list_users() -> list:
    """List all users (owner view only). Returns list of dicts."""
    if not crm.is_configured():
        return []
    try:
        tid = _ensure_users_table()
        users = []
        offset = None
        with httpx.Client(timeout=30) as c:
            while True:
                params = {"pageSize": "100"}
                if offset:
                    params["offset"] = offset
                r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}",
                          headers=crm._headers(), params=params)
                r.raise_for_status()
                j = r.json()
                for rec in j.get("records", []):
                    fields = rec.get("fields", {})
                    users.append({
                        "record_id": rec["id"],
                        "username": fields.get("Username", ""),
                        "email": fields.get("Email", ""),
                        "role": fields.get("Role", "owner"),
                        "created_at": fields.get("CreatedAt", ""),
                        "last_login": fields.get("LastLogin", ""),
                    })
                offset = j.get("offset")
                if not offset:
                    break
        return users
    except Exception as e:
        print(f"Error listing users: {e}")
        return []


def delete_user(username: str) -> bool:
    """Delete a user (admin only). Returns True on success."""
    if not crm.is_configured():
        return False
    try:
        user = get_user(username)
        if not user:
            return False
        tid = _ensure_users_table()
        with httpx.Client(timeout=30) as c:
            r = c.delete(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}/{user['record_id']}",
                         headers=crm._headers())
            r.raise_for_status()
        _user_exists_cache.pop(username, None)  # kill cached "exists" so their session invalidates immediately
        return True
    except Exception as e:
        print(f"Error deleting user {username}: {e}")
        return False


# Cheap in-memory cache of "does this username still exist?" so the
# access-gate check doesn't add a per-request Airtable round-trip.
# TTL 5 min: strictly bounds how long a deleted user's cookie can outlive them.
# delete_user() invalidates its entry immediately so the same-process case is instant.
_USER_EXISTS_TTL = 300
_user_exists_cache: dict = {}


def user_exists_cached(username: str) -> bool:
    """Return True if the username exists in the Users table. Cached for 5 min."""
    if not username or not crm.is_configured():
        return False
    now = time.time()
    entry = _user_exists_cache.get(username)
    if entry and now - entry[1] < _USER_EXISTS_TTL:
        return entry[0]
    exists = get_user(username) is not None
    _user_exists_cache[username] = (exists, now)
    return exists


def create_session_token(username: str, ttl_days: int = 30) -> str:
    """Create a signed session token for a user."""
    secret = get_session_secret()
    payload = f"{username}:{int(time.time())}"
    # HMAC signature
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    token = f"{payload}:{sig}"
    return token


def verify_session_token(token: str) -> Optional[str]:
    """Verify a session token and return the username if valid. None if invalid/expired."""
    try:
        secret = get_session_secret()
        parts = token.rsplit(":", 1)
        if len(parts) != 2:
            return None
        payload, sig = parts
        # Verify signature
        expected_sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        # Check TTL (30 days = 2592000 seconds)
        user_and_ts = payload.split(":")
        if len(user_and_ts) != 2:
            return None
        username, ts_str = user_and_ts
        ts = int(ts_str)
        now = int(time.time())
        if now - ts > 30 * 24 * 60 * 60:
            return None  # Token expired
        return username
    except Exception:
        return None
