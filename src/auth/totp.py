"""TOTP utilities: generate secrets, build provisioning URIs, verify codes."""

import logging

import pyotp

logger = logging.getLogger(__name__)


def generate_secret() -> str:
    """Generate a cryptographically random base32 TOTP secret."""
    logger.debug("generate_secret called")
    return pyotp.random_base32()


def build_uri(secret: str, username: str, issuer: str = "MediaRSSReader") -> str:
    """Return an otpauth:// URI suitable for QR code generation."""
    logger.debug(f"build_uri for username={username} issuer={issuer}")
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_code(secret: str, code: str) -> bool:
    """Return True if code is valid for the current ±1 time step (30 s window)."""
    if not code:
        logger.debug("verify_code rejected: empty code")
        return False
    ok = pyotp.TOTP(secret).verify(code, valid_window=1)
    logger.debug(f"verify_code result={ok}")
    return ok
