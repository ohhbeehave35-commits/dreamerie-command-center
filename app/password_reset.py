"""Password recovery.

Until now there was no way for anyone -- not even the owner -- to recover a
forgotten password. /api/change-password requires the CURRENT password, and the
only owner controls were create and delete. The single available "fix" was to
delete the account and recreate it, which looks like it works but quietly
detaches the user's history: chats and per-user settings are keyed by username,
so a recreated account is not the same account.

This module holds the pieces that recovery needs, kept out of main.py so both
deployments can carry an identical copy.
"""

import secrets

# Ambiguous glyphs removed. A temporary password is usually read aloud, texted,
# or copied by hand, and "l" vs "1" vs "I" turns a 30-second unblock into
# another round of "it says invalid password".
_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_SYMBOLS = "!@#$%*-+"
# NOT string.digits -- that contains the 0 and 1 the alphabet above deliberately
# drops, and the forced-digit pick below would have smuggled them back in.
_DIGITS = "23456789"


def generate_temporary_password(length: int = 16) -> str:
    """A strong temporary password, generated server-side.

    Generated here rather than accepted from the request body on purpose:
    nothing weak gets chosen under time pressure, and the plaintext exists in
    exactly one HTTP response -- it is never stored, echoed back, or logged.

    Guaranteed to satisfy users.validate_password (>=8 chars, at least one digit
    or symbol) by construction, so a reset can never fail validation after the
    caller has already been told a password was issued.
    """
    if length < 12:
        length = 12
    body = "".join(secrets.choice(_ALPHABET) for _ in range(length - 2))
    # Force one digit and one symbol rather than hoping the random draw produced
    # them -- a 1-in-N chance of tripping validation is not worth the gamble.
    picked = [secrets.choice(_DIGITS), secrets.choice(_SYMBOLS)]
    chars = list(body) + picked
    # Shuffle without Random(): secrets-backed Fisher-Yates keeps the whole
    # value cryptographically sourced.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)
