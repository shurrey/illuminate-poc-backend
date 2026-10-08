"""Assertions over the synthesized CloudFormation templates.

Run after `npx cdk synth -q -c environment=dev --exclusively IlluminateBase-dev`
(--exclusively skips Lambda asset bundling, which needs Docker).
"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "cdk.out"


def resources(stack: str, kind: str) -> list[dict]:
    template = json.loads((OUT / f"{stack}.template.json").read_text())
    return [r for r in template["Resources"].values() if r["Type"] == kind]


def check(name: str, ok: bool) -> bool:
    print(("PASS " if ok else "FAIL ") + name)
    return ok


def main() -> int:
    [client] = resources("IlluminateBase-dev", "AWS::Cognito::UserPoolClient")
    writable = client["Properties"].get("WriteAttributes")
    results = [
        check("user pool client declares its writable attributes", writable is not None),
        check("users cannot write custom:tenant_id", writable is not None and "custom:tenant_id" not in writable),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
