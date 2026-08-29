"""PII redaction at the ingestion boundary.

Applied before storage and before any LLM call. The cheapest way to survive a
personal-data incident is not to hold the data (PDPL, data minimisation).
"""

from __future__ import annotations

import re

# Saudi mobile formats, international and local, tolerant of separators.
_PHONE = re.compile(r"(?:\+?966|00966|0)\s*5\d(?:[\s\-.]?\d){7}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# National ID / Iqama: 10 digits starting 1 or 2, not part of a longer run.
_NATIONAL_ID = re.compile(r"(?<!\d)[12]\d{9}(?!\d)")

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def normalize_digits(text: str) -> str:
    """Eastern Arabic numerals to ASCII, so downstream parsing sees one form."""
    return text.translate(_ARABIC_DIGITS)


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Strip contact details. Returns the cleaned text and what was removed."""
    counts = {"phone": 0, "email": 0, "national_id": 0}
    normalized = normalize_digits(text)

    def sub(pattern: re.Pattern[str], token: str, key: str, value: str) -> str:
        nonlocal counts
        result, n = pattern.subn(token, value)
        counts[key] += n
        return result

    out = sub(_EMAIL, "[REDACTED_EMAIL]", "email", normalized)
    out = sub(_PHONE, "[REDACTED_PHONE]", "phone", out)
    out = sub(_NATIONAL_ID, "[REDACTED_ID]", "national_id", out)
    return out, counts
