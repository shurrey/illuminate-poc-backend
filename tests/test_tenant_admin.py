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
    assert result["other_tenant"] == 0 and result["conflicts"] == 0


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


def test_rerun_keeps_old_rows_the_new_tenant_has_changed(aws):
    seeded(aws)
    with pytest.raises(RuntimeError):
        aws.migrate(dynamodb=FlakyDynamodb(aws.dynamodb, fail_on_delete=1))
    tid = aws.table("tenants").scan()["Items"][0]["tenant_id"]
    aws.table("overlays").put_item(Item={"tenant_id": tid, "metric_id": "measure:m", "expr": "edited"})
    result = aws.migrate()
    assert result["conflicts"] == 1 and result["overlays"] == 3
    assert aws.keys("overlays", OLD) == ["measure:m"]
    assert aws.table("overlays").get_item(Key={"tenant_id": tid, "metric_id": "measure:m"})["Item"]["expr"] == "edited"


def snapshot(aws):
    return (
        sorted(map(str, aws.table("overlays").scan()["Items"])),
        sorted(map(str, aws.table("conversations").scan()["Items"])),
        sorted((u["Username"], str(u["Attributes"])) for u in aws.cognito.list_users(UserPoolId=aws.pool)["Users"]),
    )


def test_dry_run_writes_nothing(aws):
    seeded(aws)
    before = snapshot(aws)
    result = aws.migrate(dry_run=True)
    assert (result["users"], result["overlays"], result["conversations"]) == (2, 4, 2)
    assert aws.table("tenants").scan()["Items"] == []
    assert snapshot(aws) == before
    names = [s["Name"] for s in aws.secrets.list_secrets()["SecretList"]]
    assert not [n for n in names if n.startswith("illuminate/tenants/")]


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


def test_conversation_of_another_tenants_user_is_counted_separately(aws):
    seeded(aws)
    other = aws.user("d@y.edu", "someone-else")
    aws.table("conversations").put_item(Item={"context_id": "c6", "owner_sub": other})
    result = aws.migrate()
    assert (result["orphaned"], result["other_tenant"]) == (1, 1)


def test_cli_never_prints_secret_values(aws, monkeypatch, capsys):
    seeded(aws)
    monkeypatch.setattr(tenant_cli, "_clients", lambda env, *a, **k: (aws.dynamodb, aws.secrets, aws.cognito, aws.pool))
    assert tenant_cli.main(["migrate-current", "--dry-run"]) == 0
    assert tenant_cli.main(["list"]) == 0
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    assert "tenant_id" in out.out
    assert tenant_cli.main(["migrate-current"]) == 0
    tid = aws.table("tenants").scan()["Items"][0]["tenant_id"]
    assert aws.secrets.get_secret_value(SecretId=f"illuminate/tenants/{tid}/snowflake")["SecretString"]
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    assert tid in out.out


def _delete_source_secret(aws):
    aws.secrets.delete_secret(SecretId=f"illuminate/{ENV}/snowflake", ForceDeleteWithoutRecovery=True)


@pytest.mark.parametrize("dry_run", [False, True])
def test_migrate_without_a_source_secret_writes_nothing(aws, dry_run):
    seeded(aws)
    _delete_source_secret(aws)
    with pytest.raises(tenant_admin.SourceSecretMissing, match=f"illuminate/{ENV}/snowflake"):
        aws.migrate(dry_run=dry_run)
    assert aws.table("tenants").scan()["Items"] == []
    assert aws.secrets.list_secrets()["SecretList"] == []
    assert aws.user_tenant("a@x.edu") == OLD
    assert aws.keys("overlays", OLD) != []


def test_a_tenant_row_with_no_secret_and_no_source_is_reported_not_finished(aws):
    seeded(aws)
    tid = aws.migrate()["tenant_id"]
    aws.secrets.delete_secret(SecretId=f"illuminate/tenants/{tid}/snowflake", ForceDeleteWithoutRecovery=True)
    _delete_source_secret(aws)
    with pytest.raises(tenant_admin.SourceSecretMissing):
        aws.migrate()


def test_migrate_resumes_from_the_tenants_own_secret_once_the_source_is_gone(aws):
    seeded(aws)
    tid = aws.migrate()["tenant_id"]
    _delete_source_secret(aws)
    result = aws.migrate()
    assert (result["tenant_id"], result["created"], result["secret_copied"]) == (tid, 0, 0)


def test_cli_exits_with_a_clear_message_when_the_source_secret_is_missing(aws, monkeypatch, capsys):
    seeded(aws)
    _delete_source_secret(aws)
    monkeypatch.setattr(tenant_cli, "_clients", lambda env, *a, **k: (aws.dynamodb, aws.secrets, aws.cognito, aws.pool))
    assert tenant_cli.main(["migrate-current"]) == 1
    out = capsys.readouterr()
    assert f"illuminate/{ENV}/snowflake" in out.err and "Nothing was written" in out.err
    assert aws.table("tenants").scan()["Items"] == []


# --- create / add-users / set-connection / set-license ---

def _groups(aws):
    for g in ("illuminate-admins", "illuminate-authors", "illuminate-developers"):
        aws.cognito.create_group(UserPoolId=aws.pool, GroupName=g)


def _user_groups(aws, email):
    resp = aws.cognito.admin_list_groups_for_user(UserPoolId=aws.pool, Username=email)
    return sorted(g["GroupName"] for g in resp["Groups"])


def _claims(aws, email):
    attrs = aws.cognito.admin_get_user(UserPoolId=aws.pool, Username=email)["UserAttributes"]
    return {a["Name"]: a["Value"] for a in attrs}


def _create(aws, **kw):
    args = dict(dynamodb=aws.dynamodb, secrets=aws.secrets, cognito=aws.cognito, user_pool_id=aws.pool, env=ENV,
                name="Acme", license="core", connection=SECRET, users=[])
    return tenant_admin.create_tenant(**{**args, **kw})


def _secret_of(aws, tid):
    return json.loads(aws.secrets.get_secret_value(SecretId=f"illuminate/tenants/{tid}/snowflake")["SecretString"])


def test_create_tenant_with_copied_connection_and_two_users(aws):
    _groups(aws)
    source = _create(aws)["tenant_id"]
    result = _create(aws, connection=None, copy_connection_from=source, name="Beta", license="premium",
                     users=[("a@x.edu", "Admin"), ("v@x.edu", "Viewer")])
    tid = result["tenant_id"]
    assert result["users_created"] == ["a@x.edu", "v@x.edu"] and result["users_refused"] == []
    assert _secret_of(aws, tid) == SECRET
    row = aws.table("tenants").get_item(Key={"tenant_id": tid})["Item"]
    assert (row["name"], row["license"], row["mcp_enabled"]) == ("Beta", "premium", False)
    assert row["created_at"] == row["updated_at"]
    claims = _claims(aws, "a@x.edu")
    assert claims["custom:tenant_id"] == tid and claims["email_verified"] == "true"
    assert _user_groups(aws, "a@x.edu") == ["illuminate-admins"]
    assert _user_groups(aws, "v@x.edu") == []


def test_create_tenant_writes_the_row_only_after_the_secret(aws):
    with pytest.raises(ValueError):
        _create(aws, connection=None, copy_connection_from="no-such-tenant")
    with pytest.raises(ValueError):
        _create(aws, connection={"account": "a"})
    assert aws.table("tenants").scan()["Items"] == []


@pytest.mark.parametrize("kw", [
    {"license": "gold"},
    {"users": [("a@x.edu", "Superuser")]},
    {"connection": None},
    {"copy_connection_from": "x"},
    {"send_email": False},
])
def test_create_tenant_rejects_bad_input_before_writing(aws, kw):
    with pytest.raises(ValueError):
        _create(aws, **kw)
    assert aws.table("tenants").scan()["Items"] == []
    assert aws.cognito.list_users(UserPoolId=aws.pool)["Users"] == []


def test_create_tenant_unknown_role_creates_no_users_or_tenant(aws):
    with pytest.raises(ValueError):
        _create(aws, users=[("a@x.edu", "Admin"), ("b@x.edu", "Boss")])
    assert aws.cognito.list_users(UserPoolId=aws.pool)["Users"] == []


def test_migrate_rejects_unknown_license(aws):
    with pytest.raises(ValueError):
        aws.migrate(license="gold")


def test_add_users_refuses_other_tenant_email(aws):
    _groups(aws)
    aws.user("other@x.edu", "someone-else")
    aws.user("none@x.edu", None)
    result = tenant_admin.add_users(cognito=aws.cognito, user_pool_id=aws.pool, tenant_id="t1",
                                    users=[("other@x.edu", "Admin"), ("none@x.edu", "Admin"), ("new@x.edu", "Author")])
    assert result == {"users_created": ["new@x.edu"], "users_refused": ["other@x.edu", "none@x.edu"]}
    assert aws.user_tenant("other@x.edu") == "someone-else"
    assert aws.user_tenant("none@x.edu") is None
    assert _user_groups(aws, "other@x.edu") == [] and _user_groups(aws, "none@x.edu") == []
    assert _user_groups(aws, "new@x.edu") == ["illuminate-authors"]


def test_add_users_is_idempotent_for_same_tenant(aws):
    _groups(aws)
    users = [("a@x.edu", "Developer")]
    kw = dict(cognito=aws.cognito, user_pool_id=aws.pool, tenant_id="t1")
    assert tenant_admin.add_users(users=users, **kw)["users_created"] == ["a@x.edu"]
    again = tenant_admin.add_users(users=users, **kw)
    assert again == {"users_created": [], "users_refused": []}
    assert _user_groups(aws, "a@x.edu") == ["illuminate-developers"]
    tenant_admin.add_users(users=[("a@x.edu", "Admin")], **kw)
    assert _user_groups(aws, "a@x.edu") == ["illuminate-admins", "illuminate-developers"]


def test_add_users_no_email_requires_and_uses_a_temporary_password(aws):
    kw = dict(cognito=aws.cognito, user_pool_id=aws.pool, tenant_id="t1", users=[("a@x.edu", "Viewer")])
    with pytest.raises(ValueError):
        tenant_admin.add_users(send_email=False, **kw)
    assert tenant_admin.add_users(send_email=False, temporary_password="Tmp-Pass-1!", **kw)["users_created"] == ["a@x.edu"]
    assert aws.cognito.admin_get_user(UserPoolId=aws.pool, Username="a@x.edu")["UserStatus"] == "FORCE_CHANGE_PASSWORD"


def test_set_license_updates_row_and_rejects_unknown(aws):
    tid = _create(aws)["tenant_id"]
    before = aws.table("tenants").get_item(Key={"tenant_id": tid})["Item"]
    tenant_admin.set_license(dynamodb=aws.dynamodb, env=ENV, tenant_id=tid, license="premium")
    after = aws.table("tenants").get_item(Key={"tenant_id": tid})["Item"]
    assert after["license"] == "premium" and after["updated_at"] >= before["updated_at"]
    with pytest.raises(ValueError):
        tenant_admin.set_license(dynamodb=aws.dynamodb, env=ENV, tenant_id=tid, license="gold")
    with pytest.raises(ValueError):
        tenant_admin.set_license(dynamodb=aws.dynamodb, env=ENV, tenant_id="missing", license="core")
    assert aws.table("tenants").get_item(Key={"tenant_id": tid})["Item"]["license"] == "premium"


def test_set_license_takes_effect_after_registry_cache(aws, monkeypatch):
    import tenancy

    tid = _create(aws)["tenant_id"]
    monkeypatch.setattr(tenancy, "_table", aws.table("tenants"))
    monkeypatch.setattr(tenancy, "_cache", {})
    clock = [1000.0]
    monkeypatch.setattr(tenancy.time, "monotonic", lambda: clock[0])
    assert tenancy.get_tenant(tid).license == "core"
    tenant_admin.set_license(dynamodb=aws.dynamodb, env=ENV, tenant_id=tid, license="premium")
    assert tenancy.get_tenant(tid).license == "core"
    clock[0] += 61
    assert tenancy.get_tenant(tid).license == "premium"


def test_set_connection_requires_tenant_and_validates(aws):
    with pytest.raises(ValueError):
        tenant_admin.set_connection(dynamodb=aws.dynamodb, secrets=aws.secrets, env=ENV, tenant_id="missing",
                                    connection=SECRET)
    tid = _create(aws)["tenant_id"]
    with pytest.raises(ValueError):
        tenant_admin.set_connection(dynamodb=aws.dynamodb, secrets=aws.secrets, env=ENV, tenant_id=tid)
    assert _secret_of(aws, tid) == SECRET


def test_set_connection_takes_effect_after_credential_ttl(aws, monkeypatch):
    import snowflake_client
    from tenancy import Tenant

    tid = _create(aws)["tenant_id"]
    clock = [1000.0]
    monkeypatch.setattr(snowflake_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(snowflake_client, "_credentials", {})
    monkeypatch.setattr(snowflake_client, "_drop_connection", lambda tenant_id: None)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    tenant = Tenant(tid, "Acme", "core", False)
    assert snowflake_client.tenant_database(tenant) == "DB"
    tenant_admin.set_connection(dynamodb=aws.dynamodb, secrets=aws.secrets, env=ENV, tenant_id=tid,
                                connection={**SECRET, "database": "NEWDB"})
    assert snowflake_client.tenant_database(tenant) == "DB"
    clock[0] += snowflake_client.CREDENTIALS_TTL_SECONDS + 1
    assert snowflake_client.tenant_database(tenant) == "NEWDB"


def test_cli_creates_tenant_and_never_prints_password_or_secret(aws, monkeypatch, capsys, tmp_path):
    _groups(aws)
    monkeypatch.setattr(tenant_cli, "_clients", lambda env, *a, **k: (aws.dynamodb, aws.secrets, aws.cognito, aws.pool))
    monkeypatch.setenv("ILLUMINATE_TEMP_PASSWORD", "Tmp-Pass-do-not-print-1!")
    conn = tmp_path / "c.json"
    conn.write_text(json.dumps(SECRET))
    users = tmp_path / "u.csv"
    users.write_text("email,role\na@x.edu,Admin\nb@x.edu,Viewer\n")
    assert tenant_cli.main(["create", "--name", "Acme", "--license", "core", "--snowflake", str(conn),
                            "--users", str(users), "--no-email"]) == 0
    tid = json.loads(capsys.readouterr().out)["tenant_id"]
    assert tenant_cli.main(["add-users", tid, "--users", str(users), "--no-email"]) == 0
    assert tenant_cli.main(["set-connection", tid, "--snowflake", str(conn)]) == 0
    assert tenant_cli.main(["set-connection", tid, "--copy-connection-from", tid]) == 0
    assert tenant_cli.main(["set-license", tid, "premium"]) == 0
    out = capsys.readouterr()
    for secret in (PASSWORD, "Tmp-Pass-do-not-print-1!"):
        assert secret not in out.out + out.err
    assert aws.table("tenants").get_item(Key={"tenant_id": tid})["Item"]["license"] == "premium"


def test_cli_prints_refused_users_and_exits_zero(aws, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(tenant_cli, "_clients", lambda env, *a, **k: (aws.dynamodb, aws.secrets, aws.cognito, aws.pool))
    aws.user("o@x.edu", "someone-else")
    users = tmp_path / "u.csv"
    users.write_text("email,role\no@x.edu,Viewer\n")
    assert tenant_cli.main(["add-users", "t1", "--users", str(users)]) == 0
    assert json.loads(capsys.readouterr().out)["users_refused"] == ["o@x.edu"]


def test_cli_no_email_without_env_password_fails(aws, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(tenant_cli, "_clients", lambda env, *a, **k: (aws.dynamodb, aws.secrets, aws.cognito, aws.pool))
    monkeypatch.delenv("ILLUMINATE_TEMP_PASSWORD", raising=False)
    users = tmp_path / "u.csv"
    users.write_text("email,role\na@x.edu,Viewer\n")
    assert tenant_cli.main(["add-users", "t1", "--users", str(users), "--no-email"]) == 1
    assert aws.cognito.list_users(UserPoolId=aws.pool)["Users"] == []


def test_set_snowflake_secret_rejects_non_uuid(tmp_path):
    import os
    import subprocess

    log = tmp_path / "aws.log"
    fake = tmp_path / "aws"
    fake.write_text(f'#!/bin/bash\necho "$@" >> {log}\n')
    fake.chmod(0o755)
    script = Path(__file__).parent.parent / "scripts" / "set-snowflake-secret.sh"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    bad = subprocess.run([str(script), "../other/tenant"], env=env, capture_output=True, text=True)
    assert bad.returncode != 0 and "UUID" in bad.stderr
    assert not log.exists()
    good = subprocess.run([str(script), "123e4567-e89b-12d3-a456-426614174000"], env=env, capture_output=True, text=True)
    assert "UUID" not in good.stderr
