"""Session cookie signing and verification.

Session cookies use itsdangerous.URLSafeTimedSerializer so each token
carries an embedded timestamp — no server-side store needed. The
setup cookie embeds the pending TOTP secret for the 10-minute window
between password auth and TOTP confirmation.
"""

import logging

import itsdangerous

logger = logging.getLogger(__name__)

SESSION_COOKIE = "session"
SETUP_COOKIE = "totp_setup"
SESSION_MAX_AGE = 604800  # 7 days in seconds
SETUP_MAX_AGE = 600  # 10 minutes in seconds

_SENTINEL = "authenticated"


def sign_session(secret_key: str) -> str:
    """Return a signed, timestamped session token."""
    logger.debug("sign_session issuing session token")
    return itsdangerous.URLSafeTimedSerializer(secret_key).dumps(_SENTINEL)


def verify_session(token: str, secret_key: str) -> bool:
    """Return True if the token is valid and within SESSION_MAX_AGE seconds."""
    try:
        itsdangerous.URLSafeTimedSerializer(secret_key).loads(token, max_age=SESSION_MAX_AGE)
        logger.debug("verify_session accepted token")
        return True
    except itsdangerous.BadData:
        logger.debug("verify_session rejected token")
        return False


def sign_setup_cookie(totp_secret: str, signing_key: str) -> str:
    """Embed the TOTP secret in a short-lived signed cookie payload."""
    logger.debug("sign_setup_cookie issuing setup cookie")
    return itsdangerous.URLSafeTimedSerializer(signing_key).dumps(totp_secret)


def verify_setup_cookie(token: str, signing_key: str) -> str | None:
    """Return the TOTP secret if the setup cookie is valid, else None."""
    try:
        value = itsdangerous.URLSafeTimedSerializer(signing_key).loads(token, max_age=SETUP_MAX_AGE)
        logger.debug("verify_setup_cookie accepted token")
        return value
    except itsdangerous.BadData:
        logger.debug("verify_setup_cookie rejected token")
        return None
