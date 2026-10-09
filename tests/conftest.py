import os

# chat_engine resolves the database at import; without this it would ask Secrets Manager.
os.environ.setdefault("SNOWFLAKE_DATABASE", "ILLUMINATE")
