import pytest

import chat_engine


class FakeSecrets:
    def __init__(self, secret_string):
        self.secret_string = secret_string

    def get_secret_value(self, SecretId):
        return {"SecretString": self.secret_string}


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.delenv("SNOWFLAKE_DATABASE", raising=False)

    def use(secret_string):
        monkeypatch.setattr(chat_engine.boto3, "client", lambda *a, **k: FakeSecrets(secret_string))
    return use


def test_database_comes_from_the_secret(secret):
    secret('{"database": "TENANT_DB"}')
    assert chat_engine._resolve_database() == "TENANT_DB"


@pytest.mark.parametrize("secret_string", ["kq8#placeholder", '{"account": "a"}', '{"database": ""}'])
def test_a_secret_without_a_database_fails_loudly(secret, secret_string):
    secret(secret_string)
    with pytest.raises(RuntimeError, match="set-snowflake-secret"):
        chat_engine._resolve_database()
