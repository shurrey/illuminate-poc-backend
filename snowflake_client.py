"""
Snowflake client for the Lambda proxy.

Connections and credentials are per tenant, read from the Secrets Manager secret
illuminate/tenants/{tenant_id}/snowflake and cached per Lambda instance. A missing secret
or database means the tenant is not set up (TenantNotReady); there is no shared fallback.
"""

import functools
import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from licenses import LEARN_REFUSAL, allows_learn, schema_allowed
from semantic_layer.compiler import is_bounded_generator_call
from semantic_layer.learn import learn_columns, learn_pii
from semantic_layer.pii import PII_COLUMN_NAMES, inside_counting_aggregate
from tenancy import Tenant, TenantNotReady

if TYPE_CHECKING:
    from semantic_layer.governed_sql import Spliced

logger = logging.getLogger("API-PROXY")

CREDENTIALS_TTL_SECONDS = 300

_connections: dict = {}
_credentials: dict[str, tuple[float, dict]] = {}  # tenant_id -> (monotonic time read, secret)


def _require(tenant: Tenant | None) -> Tenant:
    if tenant is None:
        raise TenantNotReady("no tenant")
    return tenant


# Secret states an operator has to fix; anything else (throttling, outages) is transient and propagates.
_UNUSABLE_SECRET_CODES = frozenset({
    "ResourceNotFoundException", "AccessDeniedException", "InvalidRequestException", "DecryptionFailure"})


def _parse_secret(response: dict) -> tuple[dict, str | None]:
    """The secret's connection fields and None, or ({}, a reason that names no secret value)."""
    if "SecretString" not in response:
        return {}, "binary secret (no SecretString)"
    try:
        creds = json.loads(response["SecretString"])
    except ValueError:
        return {}, "malformed JSON"
    if not isinstance(creds, dict):
        return {}, "not a JSON object"
    missing = [k for k in ("account", "password", "database") if not creds.get(k)]
    if not (creds.get("user") or creds.get("username")):
        missing.append("user")
    return (creds, None) if not missing else ({}, f"missing {', '.join(missing)}")


def _not_ready(tenant_id: str, reason: str, cause: Exception | None = None):
    logger.warning("Snowflake secret for tenant %s is unusable: %s", tenant_id, reason)
    invalidate(tenant_id)
    raise TenantNotReady(tenant_id) from cause


def _load_credentials(tenant: Tenant) -> dict:
    """The tenant's Snowflake secret, re-read every CREDENTIALS_TTL_SECONDS.

    A changed secret drops the tenant's connection so the next query logs in with the new one; a deleted,
    unreadable or incomplete secret drops it too and raises TenantNotReady.
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
        code = exc.response.get("Error", {}).get("Code")
        if code in _UNUSABLE_SECRET_CODES:
            _not_ready(tenant_id, code, exc)
        raise
    creds, reason = _parse_secret(response)
    if reason:
        _not_ready(tenant_id, reason)
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


class WarehouseError(Exception):
    """A Snowflake failure whose message is cut at its first quote."""


_QUOTE_LIKE = re.compile("['\"`\u2018\u2019\u201c\u201d]")


def before_first_quote(message: str) -> str:
    """message up to its first quote-like character, then "…". Snowflake echoes row values in errors, and how it
    quotes them cannot be parsed reliably, so nothing from the first quote on is kept."""
    match = _QUOTE_LIKE.search(message)
    return message if match is None else message[:match.start()] + "…"


def _error_code(exc: Exception) -> str | None:
    code = getattr(exc, "errno", None) or getattr(exc, "sqlstate", None)
    return str(code) if code else None


def _warehouse_message(exc: Exception, allow_identity: bool) -> str:
    """What a caller sees: the code alone without identity rights, else the text before the first quote."""
    if allow_identity:
        return before_first_quote(str(exc))
    code = _error_code(exc)
    return f"The warehouse could not run this query (error {code})." if code else "The warehouse could not run this query."


def query_preview(schema: str, table: str, limit: int = 20, *, tenant: Tenant | None) -> dict:
    """Execute SELECT * FROM schema.table LIMIT n and return columns + rows.

    The database is set on the connection from the tenant's credentials,
    so only schema and table are needed.

    Returns:
        {"columns": ["COL1", ...], "rows": [{"COL1": val, ...}, ...], "truncated": bool}, at most MAX_ROWS rows
    """
    # Identifiers are double-quoted per Snowflake convention.
    # Callers must validate schema/table before calling this function.
    try:
        return _run(f'SELECT * FROM "{schema}"."{table}" LIMIT {limit}', None, tenant)
    except TenantNotReady:
        raise
    except Exception as exc:
        raise WarehouseError(before_first_quote(str(exc))) from None


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


def _folded(identifier) -> str:
    """Snowflake name resolution: unquoted identifiers fold to upper case, quoted ones are exact."""
    return identifier.name if identifier.args.get("quoted") else identifier.name.upper()


def _is_cte_reference(table_node) -> bool:
    """True when an unqualified table names a CTE visible at that point.

    Inside a CTE body only earlier CTEs of the same WITH are visible (its own name too when RECURSIVE);
    the main query sees them all.
    """
    import sqlglot.expressions as exp

    name = _folded(table_node.this)
    prev, node = table_node, table_node.parent
    while node is not None:
        if isinstance(node, exp.With):
            ctes = node.expressions
            visible = ctes[:ctes.index(prev) + (1 if node.args.get("recursive") else 0)] if prev in ctes else ctes
        else:
            with_ = node.args.get("with") or node.args.get("with_")
            visible = with_.expressions if with_ is not None and prev is not with_ else []
        if any(_folded(cte.args["alias"].this) == name for cte in visible):
            return True
        prev, node = node, node.parent
    return False


@functools.lru_cache(maxsize=1)
def _cdm_columns() -> dict[tuple[str, str], frozenset[str]]:
    from semantic_layer.validate import load_snapshot

    (schemas,) = load_snapshot().values()
    return {(s, t): frozenset(cols) for s, tables in schemas.items() for t, cols in tables.items()}


def _base_table(source) -> tuple[str, str] | None:
    """(SCHEMA, TABLE) of a scope source that is a schema-qualified table, else None."""
    import sqlglot.expressions as exp

    if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier) or not source.args.get("db"):
        return None
    return source.args["db"].name.upper(), _folded(source.this)


def _source_has(source, column: str) -> bool:
    """Whether a scope source is known to expose column: a snapshot table's column or a derived output name."""
    from sqlglot.optimizer.scope import Scope
    import sqlglot.expressions as exp

    if isinstance(source, Scope):
        if not isinstance(source.expression, (exp.Select, exp.SetOperation)):
            return False
        names = {n.upper() for n in source.expression.named_selects}
        return column in names or "*" in names
    table = _base_table(source)
    if table is None:
        return False
    if table[0] == "LEARN":
        return column in (learn_columns(table[1]) or ())
    return column in _cdm_columns().get(table, ())


def _is_projection_alias(col, name: str, select) -> bool:
    """Whether col names an AS alias of select other than the one whose expression contains it."""
    import sqlglot.expressions as exp

    return any(isinstance(e, exp.Alias) and e.alias.upper() == name and col.find_ancestor(exp.Alias) is not e
               for e in select.expressions)


def _resolve(col, scope) -> list | None:
    """The sources col may read from, [] for a projection alias, or None when it cannot be resolved.

    Walks out through enclosing scopes so correlated references resolve to the outer query's tables.
    """
    import sqlglot.expressions as exp

    name, qualifier = _folded(col.this), col.args.get("table")
    while scope is not None:
        selected = {alias.upper(): src for alias, (_, src) in scope.selected_sources.items()}
        if qualifier is not None:
            if qualifier.name.upper() in selected:
                return [selected[qualifier.name.upper()]]
        else:
            candidates = [src for src in selected.values() if _source_has(src, name)]
            if candidates:
                return candidates
            if isinstance(scope.expression, exp.Select) and _is_projection_alias(col, name, scope.expression):
                return []
            # Snowflake would bind to a column this LEARN table has but the snapshot does not list.
            learn = [src for src in selected.values() if (_base_table(src) or ("",))[0] == "LEARN"]
            if learn:
                return learn
        scope = scope.parent
    return None


@functools.lru_cache(maxsize=1)
def _cdm_pii() -> frozenset[str]:
    from semantic_layer.validate import load_pii_columns

    return load_pii_columns()


def _cdm_flags(table: tuple[str, str], column: str) -> bool:
    """Whether CDM SCHEMA.TABLE.COLUMN is PII: flagged (or its whole table is), or not in the snapshot."""
    if column not in _cdm_columns().get(table, ()):
        return True
    return f"{table[0]}.{table[1]}.{column}" in _cdm_pii() or f"{table[0]}.{table[1]}" in _cdm_pii()


def _dictionary_pii_columns(stmt, columns) -> set[int]:
    """ids of the columns that read a dictionary-flagged column of a CDM or LEARN table.

    A CDM or LEARN column is also PII when the snapshot does not list it. Derived (CTE, subquery) outputs
    were checked where they read their tables. An unresolvable column is PII when a referenced table flags
    its name, when a referenced CDM table is unknown, or when no referenced CDM table lists it.
    """
    import sqlglot.expressions as exp
    from sqlglot.optimizer.scope import traverse_scope

    tables = [t for t in (_base_table(t) for t in stmt.find_all(exp.Table)) if t]
    learn_tables = {t[1] for t in tables if t[0] == "LEARN"}
    cdm_tables = [t for t in tables if t[0].startswith("CDM_")]

    def unresolved_is_pii(name: str) -> bool:
        listed = [t for t in cdm_tables if name in _cdm_columns().get(t, ())]
        if any(_cdm_flags(t, name) for t in listed):
            return True
        if any(name in (learn_columns(t) or ()) and learn_pii(t, name) for t in learn_tables):
            return True
        # A table the snapshot does not know could be the one that has it.
        unknown = any(t not in _cdm_columns() for t in cdm_tables)
        return (bool(cdm_tables) or bool(learn_tables)) and (unknown or not listed)

    try:
        scope_of, scope_of_select = {}, {}
        for scope in traverse_scope(stmt):
            scope_of_select[id(scope.expression)] = scope
            for c in scope.columns:
                scope_of.setdefault(id(c), scope)
    except Exception:
        scope_of, scope_of_select = {}, {}

    flagged = set()
    for col in columns:
        name = _folded(col.this) if isinstance(col.this, exp.Identifier) else col.name.upper()
        # Scopes omit ORDER BY / HAVING references to projection aliases; resolve those in their SELECT.
        scope = scope_of.get(id(col)) or scope_of_select.get(id(col.find_ancestor(exp.Select)))
        try:
            sources = _resolve(col, scope) if scope is not None else None
        except Exception:
            flagged.add(id(col))
            continue
        if sources is None:
            if unresolved_is_pii(name):
                flagged.add(id(col))
            continue
        for source in sources:
            table = _base_table(source)
            if table and (learn_pii(table[1], name) if table[0] == "LEARN"
                          else table[0].startswith("CDM_") and _cdm_flags(table, name)):
                flagged.add(id(col))
    return flagged


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


_INFORMATION_SCHEMA_VIEWS = frozenset({"TABLES", "COLUMNS", "VIEWS", "SCHEMATA", "TABLE_CONSTRAINTS"})

TABLE_FUNCTION_REFUSAL = ("This table function is not allowed. Use FLATTEN, SPLIT_TO_TABLE, STRTOK_SPLIT_TO_TABLE,"
                          " GENERATOR with a literal ROWCOUNT, or a LATERAL subquery.")

_TABLE_FUNCTIONS = frozenset({"SPLIT_TO_TABLE", "STRTOK_SPLIT_TO_TABLE"})

# Functions that read query history, results, DDL or account state rather than the tenant's tables.
_REFUSED_FUNCTIONS = frozenset({"RESULT_SCAN", "LAST_QUERY_ID", "GET_DDL", "GET_QUERY_OPERATOR_STATS"})
_REFUSED_FUNCTION_PREFIXES = ("QUERY_HISTORY", "SYSTEM$", "EXPLAIN")
_REFUSED_QUALIFIERS = frozenset({"INFORMATION_SCHEMA", "SNOWFLAKE", "ACCOUNT_USAGE"})


def _allowed_table_function(fn) -> bool:
    """Whether a FROM/LATERAL source that is not a named table is a subquery or an allow-listed table function.

    Only unquoted, unqualified names match: a quoted or qualified name may resolve to a UDF.
    """
    import sqlglot.expressions as exp

    if isinstance(fn, exp.Subquery) or is_bounded_generator_call(fn):
        return True
    if isinstance(fn, exp.Anonymous) and fn.name.upper() == "TABLE" and len(fn.expressions) == 1:
        fn = fn.expressions[0]
    if isinstance(fn, exp.Explode):  # sqlglot's Snowflake FLATTEN
        return True
    return isinstance(fn, exp.Anonymous) and isinstance(fn.this, str) and fn.this.upper() in _TABLE_FUNCTIONS


def _refused_function(stmt) -> str | None:
    """The name of the first call in the statement to a refused function family, else None."""
    import sqlglot.expressions as exp

    for fn in stmt.find_all(exp.Func):
        name = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).upper()
        if name in _REFUSED_FUNCTIONS or name.startswith(_REFUSED_FUNCTION_PREFIXES):
            return name
    for dot in stmt.find_all(exp.Dot):
        if isinstance(dot.expression, exp.Func):
            qualifiers = {i.name.upper() for i in dot.this.find_all(exp.Identifier)}
            if qualifiers & _REFUSED_QUALIFIERS:
                return dot.sql(dialect="snowflake").split("(")[0].upper()
    for table in stmt.find_all(exp.Table):
        qualifiers = {table.text(part).upper() for part in ("db", "catalog")}
        if not isinstance(table.this, exp.Identifier) and qualifiers & _REFUSED_QUALIFIERS:
            return table.sql(dialect="snowflake").split("(")[0].upper()
    return None


POSITIONAL_REFUSAL = "Refer to columns by name; positional ($1) and IDENTIFIER() column references are not allowed."

ALIAS_LIST_REFUSAL = "Column alias lists on tables are not allowed; alias columns in the SELECT list instead."

WHOLE_ROW_REFUSAL = "Refer to columns by name; whole-row references are not allowed."


def _whole_row_reference(stmt, inside: frozenset[int] = frozenset()) -> bool:
    """Whether an unqualified column shares a name with any table, alias or CTE in the statement.

    Snowflake may resolve such a name to the whole row, so it is refused even when a column has that name.
    Nodes whose ids are in inside (governed bodies) are ignored.
    """
    import sqlglot.expressions as exp

    row_names = {_folded(a.this) for a in stmt.find_all(exp.TableAlias)
                 if isinstance(a.this, exp.Identifier) and id(a) not in inside}
    row_names |= {_folded(t.this) for t in stmt.find_all(exp.Table)
                  if isinstance(t.this, exp.Identifier) and id(t) not in inside}
    return any(isinstance(c.this, exp.Identifier) and not c.args.get("table") and _folded(c.this) in row_names
               for c in stmt.find_all(exp.Column) if id(c) not in inside)


def _normalized(cte):
    """cte, alias and column list included, round-tripped through SQL with identifiers folded as Snowflake does."""
    import sqlglot
    import sqlglot.expressions as exp
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

    holder = exp.select("1")
    holder.set("with", exp.With(expressions=[cte.copy()]))
    return normalize_identifiers(sqlglot.parse_one(holder.sql(dialect="snowflake"), dialect="snowflake"),
                                 dialect="snowflake")


def _governed_bodies(stmt, spliced) -> tuple[list, frozenset[int]] | str:
    """(the part CTEs, ids of every node inside a part or helper body), or a refusal.

    Each governed CTE must be defined once, at the top, exactly as splice built it from the compiled SQL; no other
    governed_* CTE may exist, and the rest of the statement may read only the parts.
    """
    import sqlglot.expressions as exp
    from semantic_layer.governed_sql import governed_ctes, unqualified_table

    expected = governed_ctes(spliced)
    parts = frozenset(name.upper() for name in spliced.parts)
    helpers = frozenset(expected) - parts
    with_ = stmt.args.get("with") or stmt.args.get("with_")
    top = {_folded(cte.args["alias"].this): cte for cte in (with_.expressions if with_ is not None else [])}
    defined = [cte for cte in stmt.find_all(exp.CTE) if _folded(cte.args["alias"].this).upper().startswith("GOVERNED_")]
    if not parts or not expected.keys() <= top.keys() or len(defined) != len(expected):
        return "Governed parts must each be defined once, at the top of the statement, and no other CTE may be named governed_*."
    for name, cte in expected.items():
        if _normalized(top[name]) != _normalized(cte):
            return f"{top[name].alias} does not match the governed part the server compiled."
    inside = frozenset(id(node) for name in expected for node in top[name].walk())
    for table in stmt.find_all(exp.Table):
        if id(table) not in inside and unqualified_table(table) and _folded(table.this) in helpers:
            return f"{table.name} is internal to a governed part; select from the part itself."
    part_ctes = [top[name] for name in parts]
    for cte in part_ctes:
        select = cte.this if isinstance(cte.this, exp.Select) else cte.this.find(exp.Select)
        if select is None or any(not isinstance(star.parent, exp.Count)
                                 for e in select.expressions for star in e.find_all(exp.Star)):
            return f"Governed part {cte.alias} must name its columns."
    return part_ctes, inside


def validate_and_execute(sql: str, params: dict | None = None, *, tenant: Tenant | None, compiled: bool = False,
                         allow_identity: bool = False, extra_pii: frozenset[str] | set[str] = frozenset(),
                         spliced: "Spliced | None" = None) -> dict:
    """Validate a SQL statement with sqlglot AST checks, then execute it.

    Allowed statement types: SELECT, WITH (CTE), UNION.
    Blocked: DML/DDL in any subquery, references outside CDM_*, INFORMATION_SCHEMA and (Premium only) LEARN,
    INFORMATION_SCHEMA views other than TABLES, COLUMNS, VIEWS, SCHEMATA and TABLE_CONSTRAINTS, table
    functions outside the allow-list, query-history and system functions, tables in another database, LIMIT > 1000, and
    PII columns outside counting aggregates: anywhere and with no star projections, or with
    compiled=True (semantic-layer SQL, PII-checked at definition time) in the outermost projection.
    allow_identity (callers whose role may see identity) lifts the PII-column check for compiled and
    freehand SQL. Freehand SQL's structural refusals (star, positional, IDENTIFIER, alias-list,
    whole-row) and every other check still apply to every role. extra_pii adds dataset-declared PII
    column names to the check; freehand SQL also treats columns the CDM or LEARN dictionary flags,
    and LEARN columns it does not list, as PII. spliced: the splice freehand SQL was built from; each of its
    part and helper CTEs must appear in sql exactly as splice built it. Those bodies get the compiled PII rule (a
    part's own projection is its outermost), only parts may read helpers, a part must name its columns,
    the rest of the statement gets the freehand rules, and every other check covers the whole statement.

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

    # Table functions carry no schema, so the whitelist below would pass RESULT_SCAN and friends.
    sources = [t.this for t in stmt.find_all(exp.Table) if not isinstance(t.this, exp.Identifier)]
    sources += [lateral.this for lateral in stmt.find_all(exp.Lateral)]
    if stmt.find(exp.Unnest) is not None or not all(map(_allowed_table_function, sources)):
        return {"error": TABLE_FUNCTION_REFUSAL}

    refused = _refused_function(stmt)
    if refused:
        return {"error": f"Function {refused} is not allowed."}

    # A database-qualified table or function must name the tenant's own database.
    own_database = None
    qualified_functions = list(_qualified_functions(stmt))
    catalogs = [t.args.get("catalog") for t in stmt.find_all(exp.Table)] + [parts[0] for parts in qualified_functions if len(parts) > 1]
    for catalog in filter(None, catalogs):
        if own_database is None:
            own_database = tenant_database(tenant).upper()
        if _folded(catalog) != own_database:
            return {"error": f"Database '{catalog.name}' is not the tenant's database. Only the tenant's own database is accessible."}

    # Other views list databases, roles, grants, stages and load history beyond the tenant's tables.
    for table_node in stmt.find_all(exp.Table):
        db_part = table_node.args.get("db")
        if (db_part and db_part.name.upper() == "INFORMATION_SCHEMA" and isinstance(table_node.this, exp.Identifier)
                and _folded(table_node.this) not in _INFORMATION_SCHEMA_VIEWS):
            return {"error": f"INFORMATION_SCHEMA.{table_node.name.upper()} is not accessible. "
                             "Only TABLES, COLUMNS, VIEWS, SCHEMATA and TABLE_CONSTRAINTS are."}

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
            allowed = "CDM_* schemas, LEARN and INFORMATION_SCHEMA are" if allows_learn(tenant) else \
                "CDM_* schemas and INFORMATION_SCHEMA are"
            return {"error": f"Schema '{schema_name.upper()}' is not in the allowed list. Only {allowed} accessible."}

    # PII columns must sit inside a counting aggregate. Value-returning aggregates (MIN, ARRAY_AGG,
    # LISTAGG, ANY_VALUE, MEDIAN...) and window functions return raw values, and GROUP BY on a PII
    # column returns one row per value, so neither unlocks PII. Compiled SQL is checked on its
    # outermost projection. Freehand SQL is checked everywhere except filter conditions, since
    # UNPIVOT, FLATTEN, PIVOT and MATCH_RECOGNIZE move PII values out of the projections.
    pii_names = PII_COLUMN_NAMES | {name.upper() for name in extra_pii}
    scopes: list = []
    freehand: list = []  # the columns the dictionary check also covers
    if compiled:
        if spliced is not None:
            return {"error": "Governed parts can only be spliced into freehand SQL."}
        if not allow_identity:
            outer = stmt.find(exp.Select)
            scopes = [(col, sel) for sel in (outer.expressions if outer else []) for col in sel.find_all(exp.Column)]
    else:
        parts, inside = [], frozenset()
        if spliced is not None:
            try:
                found = _governed_bodies(stmt, spliced)
            except Exception:
                logger.exception("Governed part check failed; refusing")
                found = "The governed parts could not be checked."
            if isinstance(found, str):
                return {"error": found}
            parts, inside = found
        # Structural checks apply to every role: they stop a statement from naming columns the PII scan cannot see.
        for star in stmt.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count) and id(star) not in inside:
                return {"error": "Star projections are not allowed in freehand SQL; name the columns you need."}
        # $1 and IDENTIFIER('COL') name columns without a Column node, so the PII checks cannot see them.
        if any((isinstance(n, exp.Parameter) or (isinstance(n, exp.Anonymous) and n.name.upper() == "IDENTIFIER"))
               and id(n) not in inside for n in stmt.walk()):
            return {"error": POSITIONAL_REFUSAL}
        # FROM t AS a (C1) renames t's columns before the name-based PII check sees them. CTE column lists
        # rename projections checked where they read their tables; sqlglot fills in LATERAL FLATTEN's list.
        if any(a.columns and not isinstance(a.parent, (exp.CTE, exp.Lateral)) and id(a) not in inside
               for a in stmt.find_all(exp.TableAlias)):
            return {"error": ALIAS_LIST_REFUSAL}
        try:
            whole_row = _whole_row_reference(stmt, inside)
        except Exception:
            logger.exception("Whole-row reference check failed; refusing")
            whole_row = True
        if whole_row:
            return {"error": WHOLE_ROW_REFUSAL}
        if not allow_identity:
            freehand = [col for col in stmt.find_all(exp.Column) if not _in_filter(col) and id(col) not in inside]
            scopes = [(col, stmt) for col in freehand]
            for part in parts:
                select = part.this if isinstance(part.this, exp.Select) else part.this.find(exp.Select)
                scopes += [(col, sel) for sel in (select.expressions if select else []) for col in sel.find_all(exp.Column)]
    if scopes:
        try:
            dictionary_pii_ids = _dictionary_pii_columns(stmt, freehand) if freehand else set()
        except Exception:
            logger.exception("Dictionary PII resolution failed; treating every column as PII")
            dictionary_pii_ids = {id(col) for col, _ in scopes}
        for col_node, scope in scopes:
            col_name = col_node.name.upper().strip('"').strip("'")
            if (col_name in pii_names or id(col_node) in dictionary_pii_ids) and not inside_counting_aggregate(col_node, scope):
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
        message = _warehouse_message(exc, allow_identity)
        logger.error("Snowflake execution error %s: %s", _error_code(exc), before_first_quote(str(exc)))
        # warehouse_error separates Snowflake failures (internal detail) from guard rejections.
        return {"error": message, "warehouse_error": True}


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
