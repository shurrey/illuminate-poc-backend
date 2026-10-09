import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_the_lambda_bundle_copies_every_root_module():
    """A root module left out of the cp list imports fine in tests and fails only in the deployed Lambda."""
    copied = set(re.search(r"'cp ([^']+) /asset-output/'", (ROOT / "cdk/lib/api/lambda-proxy.ts").read_text()).group(1).split())
    modules = {p.name for p in ROOT.glob("*.py")}
    assert modules <= copied, f"missing from the bundle: {sorted(modules - copied)}"
