import pytest

from lambda_handler import _scrub_pii


@pytest.mark.parametrize("text", [
    "There were 123456789 page views this term.",
    "Course 1234567890 has 42 students.",
    "Revenue grew to 2500000000 credits.",
])
def test_plain_large_numbers_survive(text):
    assert _scrub_pii(text) == text


@pytest.mark.parametrize("text,redacted", [
    ("SSN 123-45-6789 on file", "[SSN REDACTED]"),
    ("email jane.doe@example.edu now", "[EMAIL REDACTED]"),
    ("call (555) 123-4567 today", "[PHONE REDACTED]"),
    ("call 555-123-4567 today", "[PHONE REDACTED]"),
    ("call 555.123.4567 today", "[PHONE REDACTED]"),
    ("card 4111 1111 1111 1111 used", "[CARD REDACTED]"),
])
def test_formatted_pii_is_redacted(text, redacted):
    assert redacted in _scrub_pii(text)
