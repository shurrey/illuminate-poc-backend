import re
from pathlib import Path

import sqlglot


def test_installed_sqlglot_matches_the_lambda_pin():
    """The SQL guards depend on sqlglot's parse tree, which changes shape between releases."""
    reqs = (Path(__file__).parent.parent / "requirements-lambda.txt").read_text()
    pin = re.search(r"^sqlglot==(\S+)$", reqs, re.M)
    assert pin, "requirements-lambda.txt must pin sqlglot to an exact version"
    assert sqlglot.__version__ == pin.group(1)
