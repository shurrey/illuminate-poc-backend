"""Assertions over the synthesized CloudFormation templates.

Run from cdk/ after:
  npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev
The password is a placeholder so the initial-user resources synthesize (the tenant id comes from the initialTenantIds
context, so INITIAL_TENANT_ID must be unset); --exclusively skips
Lambda asset bundling, which needs Docker.
"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "cdk.out"


def resources(stack: str, kind: str) -> list[dict]:
    template = json.loads((OUT / f"{stack}.template.json").read_text())
    return [r for r in template["Resources"].values() if r["Type"] == kind]


def _username(resource: dict):
    """The Username parameter of a custom resource's Create call: a literal string or a template token."""
    create = resource["Properties"]["Create"]
    parts = create["Fn::Join"][1] if isinstance(create, dict) else [create]
    for i, part in enumerate(parts):
        if isinstance(part, str) and '"Username":"' in part:
            tail = part.split('"Username":"', 1)[1]
            return tail.split('"', 1)[0] if tail else parts[i + 1]
    return None


def check(name: str, ok: bool) -> bool:
    print(("PASS " if ok else "FAIL ") + name)
    return ok


def main() -> int:
    [client] = resources("IlluminateBase-dev", "AWS::Cognito::UserPoolClient")
    writable = client["Properties"].get("WriteAttributes")
    [pool] = resources("IlluminateBase-dev", "AWS::Cognito::UserPool")
    required = [a for a in pool["Properties"].get("Schema", []) if a.get("Required")]
    groups = [g["Properties"]["GroupName"] for g in resources("IlluminateBase-dev", "AWS::Cognito::UserPoolGroup")]
    seed = [r for r in resources("IlluminateBase-dev", "Custom::AWS")
            if "CognitoIdentityServiceProvider" in json.dumps(r.get("Properties", {}))]
    base_text = (OUT / "IlluminateBase-dev.template.json").read_text()
    base = json.loads(base_text)["Resources"]
    create_ids = [k for k, r in base.items() if r["Type"] == "Custom::AWS" and "adminCreateUser" in json.dumps(r["Properties"].get("Create"))]
    acting = [r for r in seed if any(a in json.dumps(r["Properties"].get("Create")) for a in ("adminSetUserPassword", "adminAddUserToGroup"))]
    results = [
        check("user pool client declares its writable attributes", writable is not None),
        check("users cannot write custom:tenant_id", writable is not None and "custom:tenant_id" not in writable),
        check("required attributes are writable (Cognito rejects the client otherwise)",
              writable is not None and all(a["Name"] in writable for a in required)),
        check("illuminate-admins group exists", "illuminate-admins" in groups),
        check("author and developer role groups exist", {"illuminate-authors", "illuminate-developers"} <= set(groups)),
        check("initial user is added to the admin group", "adminAddUserToGroup" in base_text),
        check("a deleted initial user does not fail the deploy",
              any("adminAddUserToGroup" in json.dumps(r) and "UserNotFoundException" in json.dumps(r)
                  for r in resources("IlluminateBase-dev", "Custom::AWS"))),
        check("seed resources exist when the dev map has a tenant id", len(seed) >= 3),
        check("no seed custom resource defines an Update or Delete call (deploys never re-tenant the seed user)",
              all(not r["Properties"].get("Update") and not r["Properties"].get("Delete") for r in seed)),
        check("no seed resource updates user attributes (adminCreateUser stamps the tenant)",
              "adminUpdateUserAttributes" not in base_text and "InitialUserTenantId" not in base_text),
        check("the seed create fails on an existing user instead of adopting it",
              len(create_ids) == 1 and "UsernameExistsException" not in json.dumps(base[create_ids[0]])),
        check("the seed password and group calls act only on the user the create returned",
              len(acting) == 2 and all(_username(r) == {"Ref": create_ids[0]} for r in acting)),
        check("no VPC or NAT gateway (nothing runs in it)",
              not resources("IlluminateBase-dev", "AWS::EC2::VPC") and not resources("IlluminateBase-dev", "AWS::EC2::NatGateway")),
        check("no unattached WAF web ACL", not resources("IlluminateBase-dev", "AWS::WAFv2::WebACL")),
        check("no Secrets Manager secret in the base stack (tenant secrets are created outside CDK)",
              not resources("IlluminateBase-dev", "AWS::SecretsManager::Secret")),
        check("API cannot write Snowflake secrets (nothing in the API writes them)",
              "secretsmanager:PutSecretValue" not in json.dumps(resources("IlluminateApi-dev", "AWS::IAM::Policy"))),
        check("the API function has reserved concurrency 20",
              [f["Properties"].get("ReservedConcurrentExecutions") for f in resources("IlluminateApi-dev", "AWS::Lambda::Function")
               if f["Properties"].get("FunctionName") == "illuminate-api-dev"] == [20]),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
