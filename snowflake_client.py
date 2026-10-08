"""
Snowflake client for the Lambda proxy.

Provides a lazy-initialized, reusable Snowflake connection for direct
data queries (e.g., table previews). Credentials are loaded from AWS
Secrets Manager on first use.

Imported lazily by lambda_handler.py only when Snowflake endpoints are
hit, so dictionary proxy endpoints don't pay the cold start cost.
"""

import json
import logging
import os

import boto3

from semantic_layer.pii import PII_COLUMN_NAMES, inside_counting_aggregate

logger = logging.getLogger("API-PROXY")

_sf_connection = None
_sf_credentials: dict | None = None


def _load_credentials() -> dict:
    """Load Snowflake credentials from Secrets Manager (cached)."""
    global _sf_credentials
    if _sf_credentials:
        return _sf_credentials

    secret_name = os.environ.get("SNOWFLAKE_SECRET_NAME", "illuminate/dev/snowflake")
    region = os.environ.get("AWS_REGION", "us-east-1")

    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    _sf_credentials = json.loads(response["SecretString"])
    logger.info(f"Loaded Snowflake credentials from {secret_name}")
    return _sf_credentials


def get_connection():
    """Get or create a Snowflake connection (lazy, reused across requests)."""
    global _sf_connection
    if _sf_connection is None or _sf_connection.is_closed():
        import snowflake.connector

        creds = _load_credentials()
        _sf_connection = snowflake.connector.connect(
            account=creds.get("account", ""),
            user=creds.get("user", creds.get("username", "")),
            password=creds.get("password", ""),
            database=creds.get("database", ""),
            warehouse=creds.get("warehouse", ""),
            role=creds.get("role", ""),
            network_timeout=30,
            login_timeout=30,
        )
        logger.info("Snowflake connection established")
    return _sf_connection


def invalidate():
    """Close the current connection and clear cached credentials.

    Call this after updating Snowflake credentials in Secrets Manager so the
    next query picks up the new values.
    """
    global _sf_connection, _sf_credentials
    if _sf_connection is not None:
        try:
            _sf_connection.close()
        except Exception:
            pass
    _sf_connection = None
    _sf_credentials = None
    logger.info("Snowflake connection and credentials cache invalidated")


def query_preview(schema: str, table: str, limit: int = 20) -> dict:
    """Execute SELECT * FROM schema.table LIMIT n and return columns + rows.

    The database is set on the connection from Secrets Manager credentials,
    so only schema and table are needed.

    Returns:
        {"columns": ["COL1", ...], "rows": [{"COL1": val, ...}, ...], "truncated": bool}, at most MAX_ROWS rows
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Identifiers are double-quoted per Snowflake convention.
        # Callers must validate schema/table before calling this function.
        cursor.execute(f'SELECT * FROM "{schema}"."{table}" LIMIT {limit}')
        columns = [desc[0] for desc in cursor.description]
        fetched = cursor.fetchmany(MAX_ROWS + 1)
        rows = [dict(zip(columns, row)) for row in fetched[:MAX_ROWS]]
        return {"columns": columns, "rows": rows, "truncated": len(fetched) > MAX_ROWS}
    finally:
        cursor.close()


def validate_and_execute(sql: str, params: dict | None = None, *, compiled: bool = False) -> dict:
    """Validate a SQL statement with sqlglot AST checks, then execute it.

    Allowed statement types: SELECT, WITH (CTE), SHOW, DESCRIBE.
    Blocked: DML/DDL in any subquery, non-CDM/INFORMATION_SCHEMA references, LIMIT > 1000, and
    PII columns outside counting aggregates: in every projection and with no star projections, or
    with compiled=True (semantic-layer SQL, PII-checked at definition time) the outermost only.

    Returns:
        {"columns": [...], "rows": [...]} on success
        {"error": "..."} on validation or execution failure
    """
    import sqlglot
    import sqlglot.expressions as exp

    stripped = sql.strip()
    upper = stripped.upper()

    # SHOW / DESCRIBE — allow with only a basic safety check
    if upper.startswith("SHOW") or upper.startswith("DESCRIBE"):
        dangerous = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE"]
        for kw in dangerous:
            if kw in upper:
                return {"error": f"Statement contains disallowed keyword: {kw}"}
        try:
            return query_sql(stripped, params)
        except Exception as exc:
            return {"error": str(exc)}

    # Parse with sqlglot
    try:
        statements = sqlglot.parse(stripped, dialect="snowflake")
    except Exception as exc:
        return {"error": f"SQL parse error: {exc}"}

    if not statements or statements[0] is None:
        return {"error": "Could not parse SQL statement."}

    if len(statements) > 1:
        return {"error": "Multiple statements are not allowed. Submit one query at a time."}

    stmt = statements[0]

    # Only allow SELECT/With at the top level
    allowed_top_level = (exp.Select, exp.With, exp.Union)
    if not isinstance(stmt, allowed_top_level):
        return {
            "error": (
                f"Statement type '{type(stmt).__name__}' is not allowed. "
                "Only SELECT, WITH, SHOW, and DESCRIBE statements are permitted."
            )
        }

    # Block DML/DDL anywhere in the AST (catches subquery abuse)
    blocked_node_types = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.TruncateTable,
    )
    for node in stmt.walk():
        if isinstance(node, blocked_node_types):
            return {
                "error": (
                    f"Statement contains a disallowed operation: {type(node).__name__}. "
                    "Only read-only queries are permitted."
                )
            }

    # Schema whitelist — only CDM_* schemas and INFORMATION_SCHEMA
    for table_node in stmt.find_all(exp.Table):
        db_part = table_node.args.get("db")
        schema_name = (db_part.name.upper() if db_part else "").strip('"').strip("'")
        if schema_name and not (
            schema_name.startswith("CDM_")
            or schema_name == "INFORMATION_SCHEMA"
        ):
            return {
                "error": (
                    f"Schema '{schema_name}' is not in the allowed list. "
                    "Only CDM_* schemas and INFORMATION_SCHEMA are accessible."
                )
            }

    # PII columns in the outermost SELECT must sit inside a counting aggregate. Value-returning
    # aggregates (MIN, ARRAY_AGG, LISTAGG, ANY_VALUE, MEDIAN...) and window functions return raw
    # values, and GROUP BY on a PII column returns one row per value, so neither unlocks PII.
    if not compiled:
        for star in stmt.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                return {"error": "Star projections are not allowed in freehand SQL; name the columns you need."}
    selects = [stmt.find(exp.Select)] if compiled else list(stmt.find_all(exp.Select))
    for select in filter(None, selects):
        for sel in select.expressions:
            for col_node in sel.find_all(exp.Column):
                col_name = col_node.name.upper().strip('"').strip("'")
                if col_name in PII_COLUMN_NAMES and not inside_counting_aggregate(col_node, sel):
                    return {
                        "error": (
                            f"Column '{col_name}' contains personally identifiable information (PII). "
                            "FERPA rules allow PII columns in the result only inside COUNT, COUNT_IF "
                            "or APPROX_COUNT_DISTINCT. Please revise your query to count this data."
                        )
                    }

    # Enforce LIMIT <= 1000
    for limit_node in stmt.find_all(exp.Limit):
        limit_expr = limit_node.args.get("expression")
        if limit_expr is not None:
            try:
                limit_val = int(limit_expr.name)
                if limit_val > 1000:
                    return {
                        "error": (
                            f"LIMIT {limit_val} exceeds the maximum allowed value of 1000. "
                            "Please reduce your LIMIT clause."
                        )
                    }
            except (ValueError, AttributeError):
                pass  # Non-literal LIMIT; allow and let Snowflake enforce

    # Validation passed — execute
    try:
        return query_sql(stripped, params)
    except Exception as exc:
        logger.error("Snowflake execution error: %s", exc)
        # warehouse_error separates Snowflake failures (internal detail) from guard rejections.
        return {"error": str(exc), "warehouse_error": True}


MAX_ROWS = 1000


def query_sql(sql: str, params: dict | None = None) -> dict:
    """Execute a read-only SQL statement and return columns + rows.

    Only SELECT and WITH statements are allowed. Supports Snowflake bind
    variables via the optional `params` dict — use `:name` syntax in SQL
    and pass `{"name": "value"}` as params.

    Returns:
        {"columns": ["COL1", ...], "rows": [{"COL1": val, ...}, ...], "truncated": bool}, at most MAX_ROWS rows

    Raises:
        ValueError: If the SQL is not a SELECT/WITH statement.
        Exception: On Snowflake execution errors (caller should catch).
    """
    # Strip SQL comments before checking prefix (LLMs often add -- comments)
    import re
    stripped = re.sub(r'--[^\n]*\n?', '', sql).strip()
    normalized = stripped.upper()
    if not (normalized.startswith("SELECT") or normalized.startswith("WITH")):
        raise ValueError("Only SELECT and WITH queries are allowed")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        columns = [desc[0] for desc in cursor.description]
        fetched = cursor.fetchmany(MAX_ROWS + 1)
        rows = [dict(zip(columns, row)) for row in fetched[:MAX_ROWS]]
        return {"columns": columns, "rows": rows, "truncated": len(fetched) > MAX_ROWS}
    finally:
        cursor.close()
