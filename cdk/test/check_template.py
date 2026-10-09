"""Assertions over the synthesized CloudFormation templates.

Run from cdk/ after:
  npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev
The password is a placeholder so the initial-user resources synthesize; --exclusively skips
Lambda asset bundling, which needs Docker.
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
    groups = [g["Properties"]["GroupName"] for g in resources("IlluminateBase-dev", "AWS::Cognito::UserPoolGroup")]
    base_text = (OUT / "IlluminateBase-dev.template.json").read_text()
    results = [
        check("user pool client declares its writable attributes", writable is not None),
        check("users cannot write custom:tenant_id", writable is not None and "custom:tenant_id" not in writable),
        check("illuminate-admins group exists", "illuminate-admins" in groups),
        check("initial user is added to the admin group", "adminAddUserToGroup" in base_text),
        check("a deleted initial user does not fail the deploy",
              any("adminAddUserToGroup" in json.dumps(r) and "UserNotFoundException" in json.dumps(r)
                  for r in resources("IlluminateBase-dev", "Custom::AWS"))),
        check("no VPC or NAT gateway (nothing runs in it)",
              not resources("IlluminateBase-dev", "AWS::EC2::VPC") and not resources("IlluminateBase-dev", "AWS::EC2::NatGateway")),
        check("no unattached WAF web ACL", not resources("IlluminateBase-dev", "AWS::WAFv2::WebACL")),
        check("Snowflake credentials are not in the template",
              all("SecretString" not in r["Properties"] for r in resources("IlluminateBase-dev", "AWS::SecretsManager::Secret"))),
        check("API cannot write the Snowflake secret (nothing in the API writes it)",
              "secretsmanager:PutSecretValue" not in json.dumps(resources("IlluminateApi-dev", "AWS::IAM::Policy"))),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
