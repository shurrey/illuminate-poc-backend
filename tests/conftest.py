import os

# Route tests resolve the database from here rather than a tenant's secret.
os.environ.setdefault("TEST_SNOWFLAKE_DATABASE", "ILLUMINATE")
# chat_engine resolves its database at import; without this it would ask Secrets Manager.
os.environ.setdefault("SNOWFLAKE_DATABASE", "ILLUMINATE")
