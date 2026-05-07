"""Session cookie signing and verification.

Session cookies use itsdangerous.URLSafeTimedSerializer so each token
carries an embedded timestamp — no server-side store needed. The
setup cookie embeds the pending TOTP secret for the 10-minute window
between password auth and TOTP confirmation.
"""

import itsdangerous

SESSION_COOKIE = "session"
SETUP_COOKIE = "totp_setup"
SESSION_MAX_AGE = 604800  # 7 days in seconds
SETUP_MAX_AGE = 600  # 10 minutes in seconds

_SENTINEL = "authenticated"


def sign_session(secret_key: str) -> str:
    """Return a signed, timestamped session token."""
    return itsdangerous.URLSafeTimedSerializer(secret_key).dumps(_SENTINEL)


def verify_session(token: str, secret_key: str) -> bool:
    """Return True if the token is valid and within SESSION_MAX_AGE seconds."""
    try:
        itsdangerous.URLSafeTimedSerializer(secret_key).loads(token, max_age=SESSION_MAX_AGE)
        return True
    except itsdangerous.BadData:
        return False


def sign_setup_cookie(totp_secret: str, signing_key: str) -> str:
    """Embed the TOTP secret in a short-lived signed cookie payload."""
    return itsdangerous.URLSafeTimedSerializer(signing_key).dumps(totp_secret)


def verify_setup_cookie(token: str, signing_key: str) -> str | None:
    """Return the TOTP secret if the setup cookie is valid, else None."""
    try:
        return itsdangerous.URLSafeTimedSerializer(signing_key).loads(token, max_age=SETUP_MAX_AGE)
    except itsdangerous.BadData:
        return None
