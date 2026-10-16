import json
import sys
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import tenant_admin

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import tenant as tenant_cli  # noqa: E402

ENV = "dev"
OLD = "blackboard-dev"
PASSWORD = "s3cr3t-pw-do-not-print"
SECRET = {"account": "acct", "user": "svc", "password": PASSWORD, "database": "DB", "warehouse": "WH", "role": "R"}


class Clients:
    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        self.secrets = boto3.client("secretsmanager", region_name="us-east-1")
        self.cognito = boto3.client("cognito-idp", region_name="us-east-1")
        self.pool = self.cognito.create_user_pool(PoolName="p")["UserPool"]["Id"]
        self.dynamodb.create_table(
            TableName=f"illuminate-tenants-{ENV}", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"}])
        self.dynamodb.create_table(
            TableName=f"illuminate-overlays-{ENV}", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"}, {"AttributeName": "metric_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"}, {"AttributeName": "metric_id", "AttributeType": "S"}])
        self.dynamodb.create_table(
            TableName=f"illuminate-conversations-{ENV}", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "context_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "context_id", "AttributeType": "S"}])
        self.secrets.create_secret(Name=f"illuminate/{ENV}/snowflake", SecretString=json.dumps(SECRET))

    def user(self, email, tenant):
        attrs = [{"Name": "email", "Value": email}] + ([{"Name": "custom:tenant_id", "Value": tenant}] if tenant else [])
        resp = self.cognito.admin_create_user(UserPoolId=self.pool, Username=email, UserAttributes=attrs)
        return next(a["Value"] for a in resp["User"]["Attributes"] if a["Name"] == "sub")

    def user_tenant(self, email):
        attrs = self.cognito.admin_get_user(UserPoolId=self.pool, Username=email)["UserAttributes"]
        return next((a["Value"] for a in attrs if a["Name"] == "custom:tenant_id"), None)

    def table(self, name):
        return self.dynamodb.Table(f"illuminate-{name}-{ENV}")

    def keys(self, name, tenant):
        items = self.table(name).scan()["Items"]
        return sorted(i["metric_id"] for i in items if i["tenant_id"] == tenant)

    def migrate(self, **kw):
        return tenant_admin.migrate_current(
            dynamodb=kw.pop("dynamodb", self.dynamodb), secrets=self.secrets, cognito=self.cognito,
            user_pool_id=self.pool, env=ENV, **kw)


@pytest.fixture
def aws(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    with mock_aws():
        yield Clients()


def seeded(aws):
    s1 = aws.user("a@x.edu", OLD)
    s2 = aws.user("b@x.edu", OLD)
    other = aws.user("c@y.edu", "someone-else")
    for key in ("measure:m", "measure:m#v000001", "measure:m#v000002", "filter:f"):
        aws.table("overlays").put_item(Item={"tenant_id": OLD, "metric_id": key, "expr": "1"})
    aws.table("overlays").put_item(Item={"tenant_id": "someone-else", "metric_id": "measure:z"})
    t = aws.table("conversations")
    t.put_item(Item={"context_id": "c1", "owner_sub": s1})
    t.put_item(Item={"context_id": "c2", "owner_sub": s2})
    t.put_item(Item={"context_id": "c3", "owner_sub": "ghost-sub"})
    t.put_item(Item={"context_id": "c4"})
    t.put_item(Item={"context_id": "c5", "owner_sub": f"already#{s1}"})
    return s1, s2


def test_migrate_creates_one_premium_tenant(aws):
    seeded(aws)
    result = aws.migrate()
    rows = aws.table("tenants").scan()["Items"]
    assert len(rows) == 1
    row = rows[0]
    assert row["tenant_id"] == result["tenant_id"] and result["created"] == 1
    assert (row["name"], row["license"], row["mcp_enabled"], row["migrated_from"]) == ("Northbridge University", "premium", False, OLD)
    assert row["created_at"].endswith("+00:00") and row["updated_at"]


def test_migrate_copies_secret_once(aws):
    seeded(aws)
    tid = aws.migrate()["tenant_id"]
    copied = json.loads(aws.secrets.get_secret_value(SecretId=f"illuminate/tenants/{tid}/snowflake")["SecretString"])
    assert copied == SECRET
    aws.secrets.put_secret_value(SecretId=f"illuminate/tenants/{tid}/snowflake", SecretString=json.dumps({**SECRET, "password": "rotated"}))
    assert aws.migrate()["secret_copied"] == 0
    assert json.loads(aws.secrets.get_secret_value(SecretId=f"illuminate/tenants/{tid}/snowflake")["SecretString"])["password"] == "rotated"


def test_migrate_moves_users_overlays_history_and_conversations(aws):
    s1, s2 = seeded(aws)
    result = aws.migrate()
    tid = result["tenant_id"]
    assert aws.user_tenant("a@x.edu") == tid and aws.user_tenant("b@x.edu") == tid
    assert aws.user_tenant("c@y.edu") == "someone-else"
    assert aws.keys("overlays", OLD) == []
    assert aws.keys("overlays", tid) == ["filter:f", "measure:m", "measure:m#v000001", "measure:m#v000002"]
    assert aws.keys("overlays", "someone-else") == ["measure:z"]
    owners = {i["context_id"]: i.get("owner_sub") for i in aws.table("conversations").scan()["Items"]}
    assert owners == {"c1": f"{tid}#{s1}", "c2": f"{tid}#{s2}", "c3": "ghost-sub", "c4": None, "c5": f"already#{s1}"}
    assert (result["users"], result["overlays"], result["conversations"], result["orphaned"], result["ownerless"]) == (2, 4, 2, 1, 1)


def test_migrate_twice_is_a_no_op_second_time(aws):
    seeded(aws)
    first = aws.migrate()
    second = aws.migrate()
    assert second["tenant_id"] == first["tenant_id"]
    assert (second["created"], second["secret_copied"], second["users"], second["overlays"], second["conversations"]) == (0, 0, 0, 0, 0)
    assert len(aws.table("tenants").scan()["Items"]) == 1


class FlakyDynamodb:
    """Wraps a boto3 dynamodb resource; the overlay table's Nth delete raises once."""

    def __init__(self, real, fail_on_delete):
        self.real, self.remaining = real, fail_on_delete

    def Table(self, name):
        table = self.real.Table(name)
        if "overlays" not in name:
            return table
        outer, real_delete = self, table.delete_item

        def delete_item(**kw):
            outer.remaining -= 1
            if outer.remaining == 0:
                raise RuntimeError("boom")
            return real_delete(**kw)

        table.delete_item = delete_item
        return table


def test_migrate_resumes_after_partial_failure(aws):
    seeded(aws)
    with pytest.raises(RuntimeError):
        aws.migrate(dynamodb=FlakyDynamodb(aws.dynamodb, fail_on_delete=3))
    tid = aws.table("tenants").scan()["Items"][0]["tenant_id"]
    assert 0 < len(aws.keys("overlays", OLD)) < 4
    result = aws.migrate()
    assert result["tenant_id"] == tid
    assert aws.keys("overlays", OLD) == []
    assert aws.keys("overlays", tid) == ["filter:f", "measure:m", "measure:m#v000001", "measure:m#v000002"]
    assert len(aws.table("tenants").scan()["Items"]) == 1


def test_dry_run_writes_nothing(aws):
    seeded(aws)
    result = aws.migrate(dry_run=True)
    assert (result["users"], result["overlays"], result["conversations"]) == (2, 4, 2)
    assert aws.table("tenants").scan()["Items"] == []
    assert aws.user_tenant("a@x.edu") == OLD
    assert len(aws.keys("overlays", OLD)) == 4
    assert all("#" not in i.get("owner_sub", "") or i["context_id"] == "c5" for i in aws.table("conversations").scan()["Items"])
    with pytest.raises(ClientError):
        aws.secrets.get_secret_value(SecretId=f"illuminate/tenants/{result['tenant_id']}/snowflake")


def test_list_tenants_returns_rows_without_secrets(aws):
    seeded(aws)
    tid = aws.migrate()["tenant_id"]
    rows = tenant_admin.list_tenants(dynamodb=aws.dynamodb, env=ENV)
    assert [r["tenant_id"] for r in rows] == [tid]
    assert rows[0]["name"] == "Northbridge University"
    assert PASSWORD not in json.dumps(rows, default=str)


def test_scans_paginate(aws):
    for i in range(30):
        aws.table("conversations").put_item(Item={"context_id": f"p{i}", "owner_sub": f"{i}" * 100_000 if i < 12 else "ghost"})
    seeded(aws)
    assert aws.migrate()["orphaned"] == 31


def test_cli_never_prints_secret_values(aws, monkeypatch, capsys):
    seeded(aws)
    monkeypatch.setattr(tenant_cli, "_clients", lambda env, *a, **k: (aws.dynamodb, aws.secrets, aws.cognito, aws.pool))
    assert tenant_cli.main(["migrate-current", "--dry-run"]) == 0
    assert tenant_cli.main(["list"]) == 0
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    assert "tenant_id" in out.out
