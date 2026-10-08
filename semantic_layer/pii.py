"""Column names treated as personally identifiable wherever they appear."""

PII_COLUMN_NAMES = frozenset({
    "FIRST_NAME", "LAST_NAME", "EMAIL", "SSN", "PHONE", "ADDRESS",
    "DOB", "DATE_OF_BIRTH", "PASSWORD", "PASSWD", "PHONE_NUMBER",
    "STREET_ADDRESS", "ZIP_CODE", "ZIPCODE",
})
