"""
Snowflake client for the Lambda proxy.

Connections and credentials are per tenant, read from the Secrets Manager secret
illuminate/tenants/{tenant_id}/snowflake and cached per Lambda instance. A missing secret
or database means the tenant is not set up (TenantNotReady); there is no shared fallback.
"""

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

from licenses import LEARN_REFUSAL, schema_allowed
from semantic_layer.compiler import is_bounded_generator
from semantic_layer.pii import PII_COLUMN_NAMES, inside_counting_aggregate
from tenancy import Tenant, TenantNotReady

logger = logging.getLogger("API-PROXY")

CREDENTIALS_TTL_SECONDS = 300

_connections: dict = {}
_credentials: dict[str, tuple[float, dict]] = {}  # tenant_id -> (monotonic time read, secret)


def _require(tenant: Tenant | None) -> Tenant:
    if tenant is None:
        raise TenantNotReady("no tenant")
    return tenant


def _load_credentials(tenant: Tenant) -> dict:
    """The tenant's Snowflake secret, re-read every CREDENTIALS_TTL_SECONDS.

    A changed secret drops the tenant's connection so the next query logs in with the new one;
    a deleted secret drops it too and raises TenantNotReady.
    """
    tenant_id = tenant.tenant_id
    cached = _credentials.get(tenant_id)
    if cached and time.monotonic() - cached[0] < CREDENTIALS_TTL_SECONDS:
        return cached[1]

    secret_id = f"illuminate/tenants/{tenant_id}/snowflake"
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            invalidate(tenant_id)
            raise TenantNotReady(tenant_id) from exc
        raise
    creds = json.loads(response["SecretString"])
    if cached and cached[1] != creds:
        _drop_connection(tenant_id)
        logger.info("Snowflake credentials changed for tenant %s", tenant_id)
    _credentials[tenant_id] = (time.monotonic(), creds)
    return creds


def tenant_database(tenant: Tenant | None) -> str:
    """The tenant's Snowflake database. Raises TenantNotReady if the secret or its database is missing."""
    database = _load_credentials(_require(tenant)).get("database")
    if not database:
        raise TenantNotReady(tenant.tenant_id)
    return database


def get_connection(tenant: Tenant | None):
    """Get or create the tenant's Snowflake connection (lazy, reused across requests)."""
    tenant = _require(tenant)
    creds = _load_credentials(tenant)  # first, so a changed or deleted secret drops the cached connection
    conn = _connections.get(tenant.tenant_id)
    if conn is None or conn.is_closed():
        import snowflake.connector

        tenant_database(tenant)
        conn = snowflake.connector.connect(
            account=creds.get("account", ""),
            user=creds.get("user", creds.get("username", "")),
            password=creds.get("password", ""),
            database=creds["database"],
            warehouse=creds.get("warehouse", ""),
            role=creds.get("role", ""),
            network_timeout=30,
            login_timeout=30,
        )
        _connections[tenant.tenant_id] = conn
        logger.info("Snowflake connection established for tenant %s", tenant.tenant_id)
    return conn


def invalidate(tenant_id: str):
    """Close one tenant's connection and forget its credentials so the next query re-reads the secret."""
    _drop_connection(tenant_id)
    _credentials.pop(tenant_id, None)
    logger.info("Snowflake connection and credentials invalidated for tenant %s", tenant_id)


def query_preview(schema: str, table: str, limit: int = 20, *, tenant: Tenant | None) -> dict:
    """Execute SELECT * FROM schema.table LIMIT n and return columns + rows.

    The database is set on the connection from the tenant's credentials,
    so only schema and table are needed.

    Returns:
        {"columns": ["COL1", ...], "rows": [{"COL1": val, ...}, ...], "truncated": bool}, at most MAX_ROWS rows
    """
    # Identifiers are double-quoted per Snowflake convention.
    # Callers must validate schema/table before calling this function.
    return _run(f'SELECT * FROM "{schema}"."{table}" LIMIT {limit}', None, tenant)


def _in_filter(node) -> bool:
    """True when node sits in a WHERE, HAVING, QUALIFY or JOIN ON condition, which filters rows but returns no values."""
    import sqlglot.expressions as exp

    while node.parent is not None:
        if isinstance(node.parent, (exp.Where, exp.Having, exp.Qualify)):
            return True
        if isinstance(node.parent, exp.Join) and node.arg_key == "on":
            return True
        node = node.parent
    return False


def _is_cte_reference(table_node) -> bool:
    """True when an unqualified table names a CTE visible at that point.

    Inside a CTE body only earlier CTEs of the same WITH are visible (its own name too when RECURSIVE);
    the main query sees them all.
    """
    import sqlglot.expressions as exp

    name = table_node.name.upper()
    prev, node = table_node, table_node.parent
    while node is not None:
        if isinstance(node, exp.With):
            ctes = node.expressions
            visible = ctes[:ctes.index(prev) + (1 if node.args.get("recursive") else 0)] if prev in ctes else ctes
        else:
            with_ = node.args.get("with") or node.args.get("with_")
            visible = with_.expressions if with_ is not None and prev is not with_ else []
        if any(cte.alias.upper() == name for cte in visible):
            return True
        prev, node = node, node.parent
    return False


def _qualified_functions(stmt):
    """Yield the qualifier identifiers ([database,] schema) of each schema-qualified function call."""
    import sqlglot.expressions as exp

    for dot in stmt.find_all(exp.Dot):
        if not isinstance(dot.expression, exp.Func):
            continue
        parts, node = [], dot.this
        while isinstance(node, exp.Dot):
            parts.insert(0, node.expression)
            node = node.this
        parts.insert(0, node)
        if all(isinstance(p, exp.Identifier) for p in parts):
            yield parts


def validate_and_execute(sql: str, params: dict | None = None, *, tenant: Tenant | None, compiled: bool = False,
                         allow_identity: bool = False, extra_pii: frozenset[str] | set[str] = frozenset()) -> dict:
    """Validate a SQL statement with sqlglot AST checks, then execute it.

    Allowed statement types: SELECT, WITH (CTE), UNION.
    Blocked: DML/DDL in any subquery, references outside CDM_*, INFORMATION_SCHEMA and (Premium only) LEARN, INFORMATION_SCHEMA.DATABASES, tables in another database, LIMIT > 1000, and
    PII columns outside counting aggregates: anywhere and with no star projections, or with
    compiled=True (semantic-layer SQL, PII-checked at definition time) in the outermost projection.
    allow_identity lifts the projection check for compiled SQL only (callers whose role may see
    identity); extra_pii adds dataset-declared PII column names to the check.

    Returns:
        {"columns": [...], "rows": [...]} on success
        {"error": "..."} on validation or execution failure
    """
    import sqlglot
    import sqlglot.expressions as exp

    stripped = sql.strip()

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
                "Only SELECT and WITH statements are permitted."
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

    # TABLE(...) sources carry no schema, so the whitelist below would pass RESULT_SCAN and friends.
    for table_node in stmt.find_all(exp.Table):
        if not isinstance(table_node.this, exp.Identifier) and not is_bounded_generator(table_node):
            return {"error": "Only the GENERATOR table function, with a literal ROWCOUNT, is allowed."
                             " Other table functions are not permitted."}

    # A database-qualified table or function must name the tenant's own database.
    own_database = None
    qualified_functions = list(_qualified_functions(stmt))
    catalogs = [t.args.get("catalog") for t in stmt.find_all(exp.Table)] + [parts[0] for parts in qualified_functions if len(parts) > 1]
    for catalog in filter(None, catalogs):
        if own_database is None:
            own_database = tenant_database(tenant).upper()
        if catalog.name.upper() != own_database:
            return {"error": f"Database '{catalog.name}' is not the tenant's database. Only the tenant's own database is accessible."}

    # INFORMATION_SCHEMA.DATABASES lists every database the role can see.
    for table_node in stmt.find_all(exp.Table):
        db_part = table_node.args.get("db")
        if db_part and db_part.name.upper() == "INFORMATION_SCHEMA" and table_node.name.upper() == "DATABASES":
            return {"error": "INFORMATION_SCHEMA.DATABASES is not accessible."}

    # Without a schema Snowflake resolves the table in the session's default schema, which may be LEARN.
    for table_node in stmt.find_all(exp.Table):
        if (isinstance(table_node.this, exp.Identifier) and not table_node.args.get("db")
                and not _is_cte_reference(table_node)):
            name = table_node.name
            return {"error": f"Name the schema for table {name.upper()} (for example CDM_LMS.{name.upper()})."}

    schemas = [t.args.get("db") for t in stmt.find_all(exp.Table)] + [parts[-1] for parts in qualified_functions]
    for schema_part in filter(None, schemas):
        schema_name = schema_part.name.strip('"').strip("'")
        if not schema_allowed(tenant, schema_name):
            if schema_name.upper() == "LEARN":
                return {"error": LEARN_REFUSAL}
            return {
                "error": (
                    f"Schema '{schema_name.upper()}' is not in the allowed list. "
                    "Only CDM_* schemas and INFORMATION_SCHEMA are accessible."
                )
            }

    # PII columns must sit inside a counting aggregate. Value-returning aggregates (MIN, ARRAY_AGG,
    # LISTAGG, ANY_VALUE, MEDIAN...) and window functions return raw values, and GROUP BY on a PII
    # column returns one row per value, so neither unlocks PII. Compiled SQL is checked on its
    # outermost projection. Freehand SQL is checked everywhere except filter conditions, since
    # UNPIVOT, FLATTEN, PIVOT and MATCH_RECOGNIZE move PII values out of the projections.
    pii_names = PII_COLUMN_NAMES | {name.upper() for name in extra_pii}
    if compiled and allow_identity:
        scopes = []
    elif compiled:
        outer = stmt.find(exp.Select)
        scopes = [(col, sel) for sel in (outer.expressions if outer else []) for col in sel.find_all(exp.Column)]
    else:
        for star in stmt.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                return {"error": "Star projections are not allowed in freehand SQL; name the columns you need."}
        scopes = [(col, stmt) for col in stmt.find_all(exp.Column) if not _in_filter(col)]
    for col_node, scope in scopes:
        col_name = col_node.name.upper().strip('"').strip("'")
        if col_name in pii_names and not inside_counting_aggregate(col_node, scope):
            return {
                "error": (
                    f"Column '{col_name}' contains personally identifiable information (PII). "
                    "FERPA rules allow PII columns only inside COUNT, COUNT_IF or "
                    "APPROX_COUNT_DISTINCT. Please revise your query to count this data."
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
        return query_sql(stripped, params, tenant=tenant)
    except TenantNotReady:
        raise
    except Exception as exc:
        logger.error("Snowflake execution error: %s", exc)
        # warehouse_error separates Snowflake failures (internal detail) from guard rejections.
        return {"error": str(exc), "warehouse_error": True}


MAX_ROWS = 1000


def query_sql(sql: str, params: dict | None = None, *, tenant: Tenant | None) -> dict:
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

    return _run(sql, params, tenant)


def _run(sql: str, params: dict | None, tenant: Tenant | None) -> dict:
    """Execute on the tenant's cached connection, reconnecting once if Snowflake has expired its session."""
    tenant = _require(tenant)
    try:
        return _execute(get_connection(tenant), sql, params)
    except Exception as exc:
        if getattr(exc, "errno", None) not in _SESSION_EXPIRED:
            raise
        logger.info("Snowflake session expired (%s); reconnecting", exc.errno)
        _drop_connection(tenant.tenant_id)
        return _execute(get_connection(tenant), sql, params)


# Session gone, or its session/master token expired: a new login fixes these.
_SESSION_EXPIRED = {390111, 390112, 390114}


def _drop_connection(tenant_id: str) -> None:
    conn = _connections.pop(tenant_id, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _execute(conn, sql: str, params: dict | None) -> dict:
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
