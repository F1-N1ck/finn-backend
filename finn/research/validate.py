"""NSE ticker validation. Strict on purpose: the ticker ends up in an outbound URL."""

import re

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")


def validate_ticker(raw):
    """Return (symbol, error_code, message). symbol is None when invalid."""
    if not isinstance(raw, str):
        return None, "invalid_ticker", "ticker must be text"
    if not raw.isascii():
        return None, "invalid_ticker", "ticker must use plain ASCII characters"
    text = raw.strip().upper()
    if text.startswith("NSE:"):
        text = text[4:].strip()
    if text == "":
        return None, "invalid_ticker", "ticker is empty"
    if len(text) > 20:
        return None, "invalid_ticker", "ticker is longer than 20 characters"
    if not TICKER_RE.fullmatch(text):
        return None, "invalid_ticker", "ticker may contain only letters, digits, & and -"
    return text, None, None
