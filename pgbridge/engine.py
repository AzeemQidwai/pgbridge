"""
Migration engine: connection handling, introspection, preflight, transport, verification.

Deliberately free of any Tk import. The UI drives this through `Transport.run()` and
consumes events off a queue, so the engine is testable and scriptable on its own.
"""

from __future__ import annotations

import csv
import datetime
import decimal
import json
import hashlib
import copy
from contextlib import closing
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Iterable

UTC = datetime.timezone.utc

# Tables Django owns on the target side and must not receive source rows.
PROTECTED_TABLES = {"django_migrations"}

# Postgres types with no ORM-visible SQL Server equivalent. Loading these needs a
# remodel first, so preflight treats them as blocking.
UNSUPPORTED_TYPES = {
    "ARRAY", "hstore", "tsvector", "tsquery",
    "int4range", "int8range", "numrange", "tsrange", "tstzrange", "daterange",
    "cube", "ltree", "geometry", "geography",
}

# Postgres -> SQL Server, used only for the assisted DDL in the Preflight stage.
TYPE_MAP = {
    "bigint": "bigint", "int8": "bigint",
    "integer": "int", "int4": "int",
    "smallint": "smallint", "int2": "smallint",
    "boolean": "bit", "bool": "bit",
    "numeric": "decimal({p},{s})", "decimal": "decimal({p},{s})",
    "double precision": "float(53)", "real": "real",
    "text": "nvarchar(max)",
    "character varying": "nvarchar({n})", "varchar": "nvarchar({n})",
    "character": "nchar({n})", "bpchar": "nchar({n})",
    "uuid": "char(32)",
    "json": "nvarchar(max)", "jsonb": "nvarchar(max)",
    "bytea": "varbinary(max)",
    "date": "date", "time without time zone": "time",
    "timestamp without time zone": "datetime2", "timestamp with time zone": "datetime2",
    "interval": "bigint", "inet": "nvarchar(39)", "cidr": "nvarchar(43)",
    "macaddr": "nvarchar(17)", "xml": "nvarchar(max)",
}

# SQL Server error codes worth retrying rather than failing on.
# Retry only transaction rollbacks; connection loss/timeouts may hide a committed batch.
RETRYABLE = {"40001", "40P01", "1205"}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class PgConfig:
    host: str = "localhost"
    port: int = 5432
    dbname: str = ""
    user: str = ""
    password: str = ""
    sslmode: str = "prefer"
    schema: str = "public"


@dataclass
class MsConfig:
    server: str = "localhost"
    database: str = ""
    driver: str = "ODBC Driver 18 for SQL Server"
    auth: str = "sql"           # "sql" | "windows"
    user: str = ""
    password: str = ""
    encrypt: bool = True
    trust_cert: bool = False
    schema: str = "dbo"

    def dsn(self, database: str | None = None) -> str:
        def quote(value):
            return "{" + str(value).replace("}", "}}") + "}"
        parts = [
            f"DRIVER={quote(self.driver)}",
            f"SERVER={quote(self.server)}",
            f"DATABASE={quote(database or self.database or 'master')}",
            f"Encrypt={'yes' if self.encrypt else 'no'}",
            f"TrustServerCertificate={'yes' if self.trust_cert else 'no'}",
        ]
        if self.auth == "windows":
            parts.append("Trusted_Connection=yes")
        else:
            parts += [f"UID={quote(self.user)}", f"PWD={quote(self.password)}"]
        return ";".join(parts)


@dataclass
class Options:
    chunk_size: int = 50_000
    fast_executemany: bool = True
    clear_target: bool = True
    disable_constraints: bool = True
    preserve_identity: bool = True
    reseed_identity: bool = True
    workers: int = 1
    max_retries: int = 3
    retry_backoff: float = 2.0
    on_row_error: str = "abort"     # "abort" | "quarantine"
    dry_run: bool = False
    resume: bool = False
    mode: str = "both"              # "schema" | "data" | "both"
    output_dir: str = "./pgbridge_output"

    def validate(self):
        if self.mode not in ("both", "schema", "data"):
            raise ValueError("Migration mode must be both, schema, or data.")
        if not 1 <= self.chunk_size <= 1_000_000:
            raise ValueError("Batch size must be between 1 and 1,000,000 rows.")
        if not 1 <= self.workers <= 4:
            raise ValueError("Parallel tables must be between 1 and 4.")
        if self.on_row_error not in ("abort", "quarantine"):
            raise ValueError("Row error policy must be abort or quarantine.")
        if self.max_retries < 0 or self.retry_backoff < 1:
            raise ValueError("Retries must be nonnegative and backoff at least 1.")

    @property
    def with_schema(self) -> bool:
        return self.mode in ("schema", "both")

    @property
    def with_data(self) -> bool:
        return self.mode in ("data", "both")


# ---------------------------------------------------------------------------
# Introspection results
# ---------------------------------------------------------------------------
@dataclass
class Column:
    name: str
    pg_type: str
    nullable: bool
    is_pk: bool
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    default: str | None = None


# What a single table does in a run. The global mode is the default; a table
# blocked by preflight can take its own route without holding back the rest.
PLANS = {
    "auto":   "follow the run's mode",
    "schema": "create the table, move no rows",
    "data":   "move rows, create nothing",
    "clean":  "move rows, minus the ones that would fail",
    "skip":   "leave this table alone",
}


@dataclass
class Table:
    name: str
    source_rows: int = 0
    target_exists: bool = False
    target_rows: int = 0
    target_has_identity: bool = False
    columns: list[Column] = field(default_factory=list)
    selected: bool = True

    plan: str = "auto"
    # A "clean" plan is these two: columns the target cannot take, and a
    # predicate that leaves behind the rows that would be rejected.
    exclude_columns: list[str] = field(default_factory=list)
    row_filter: str = ""                 # a Postgres predicate, source-side
    exclusion_notes: list[str] = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def migrating_columns(self) -> list[str]:
        """The columns actually sent. Excluded ones keep their target default."""
        skip = set(self.exclude_columns)
        return [c.name for c in self.columns if c.name not in skip]

    def does_schema(self, options: "Options") -> bool:
        if self.plan in ("skip", "data", "clean"):
            return False
        if self.plan == "schema":
            return True
        return options.with_schema

    def does_data(self, options: "Options") -> bool:
        if self.plan in ("skip", "schema"):
            return False
        if self.plan in ("data", "clean"):
            return True
        return options.with_data

    def plan_summary(self, options: "Options") -> str:
        """One phrase for the list view: what this table will actually do."""
        if self.plan == "skip" or not self.selected:
            return "skipped"
        does_s, does_d = self.does_schema(options), self.does_data(options)
        if does_s and not self.target_exists and not does_d:
            return "create only"
        if not does_d:
            return "schema only"
        bits = []
        if does_s and not self.target_exists:
            bits.append("create")
        if self.row_filter or self.exclude_columns:
            filtered = []
            if self.row_filter:
                filtered.append("rows")
            if self.exclude_columns:
                filtered.append(f"{len(self.exclude_columns)} col"
                                + ("s" if len(self.exclude_columns) > 1 else ""))
            bits.append("clean data (−" + ", −".join(filtered) + ")")
        else:
            bits.append("all data")
        return " + ".join(bits)


@dataclass
class Issue:
    level: str          # "stop" | "warn" | "note"
    table: str
    check: str
    detail: str
    remedy: str = ""
    # Ordered label -> value pairs: the column, its definition on both sides,
    # the counts behind the finding. "52 NULLs" is only actionable when you can
    # see which column, how it is declared, and what the target expects.
    facts: dict = field(default_factory=dict)
    # The column(s) the finding is about, so the findings list can name them
    # without anyone having to open the detail first.
    columns: list[str] = field(default_factory=list)
    # What a per-table plan could do about this finding, if anything:
    #   {"plans": ["clean", "schema", "skip"],
    #    "exclude_columns": [...], "row_filter": "...", "note": "..."}
    # Empty means the only way past it is to fix the schema or skip the table.
    fix: dict = field(default_factory=dict)

    @property
    def column_label(self) -> str:
        if not self.columns:
            return "—"
        if len(self.columns) <= 3:
            return ", ".join(self.columns)
        return f"{', '.join(self.columns[:3])} +{len(self.columns) - 3} more"


def describe_pg_column(c: Column) -> str:
    """A Postgres column as it would read in a DDL line."""
    kind = c.pg_type
    if c.max_length:
        kind += f"({c.max_length})"
    elif c.precision and c.pg_type in ("numeric", "decimal"):
        kind += f"({c.precision},{c.scale or 0})"
    bits = [kind, "NULL" if c.nullable else "NOT NULL"]
    if c.is_pk:
        bits.append("primary key")
    if c.default:
        bits.append(f"default {c.default}")
    return ", ".join(bits)


def describe_ms_column(row: dict) -> str:
    """A SQL Server column from sys.columns, with the byte/char length sorted out."""
    kind = row["type"]
    length = row["max_length"]
    if kind in ("nvarchar", "nchar", "varchar", "char", "varbinary", "binary"):
        if length == -1:
            kind += "(max)"
        else:
            kind += f"({length // 2 if kind.startswith('n') else length})"
    elif kind in ("decimal", "numeric"):
        kind += f"({row['precision']},{row['scale']})"
    bits = [kind, "NULL" if row["nullable"] else "NOT NULL"]
    if row["identity"]:
        bits.append("IDENTITY")
    if row["default"]:
        bits.append(f"default {row['default']}")
    return ", ".join(bits)


# ---------------------------------------------------------------------------
# Value adaptation
# ---------------------------------------------------------------------------
def adapt(value: Any, use_tz: bool = True) -> Any:
    """psycopg2 Python types -> what pyodbc/SQL Server expects."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if "\x00" in value:
            return value.replace("\x00", "")
        return value
    if isinstance(value, uuid.UUID):
        return value.hex                                # UUIDField is char(32)
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)           # jsonb -> nvarchar(max)
    if isinstance(value, memoryview):
        return bytes(value)                             # bytea -> varbinary
    if isinstance(value, datetime.timedelta):
        return (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds   # interval -> microseconds
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None and use_tz:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, (set, tuple)):
        return json.dumps(list(value), default=str)
    if isinstance(value, float):
        import math
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, decimal.Decimal):
        if value.is_nan() or value.is_infinite():
            return None
        return value
    return value


def _driver_error() -> str:
    missing = []
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        missing.append("psycopg2-binary")
    try:
        import pyodbc  # noqa: F401
    except ImportError:
        missing.append("pyodbc")
    return ", ".join(missing)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
class Connections:
    """Creates connections on demand. Each worker thread gets its own pair."""

    def __init__(self, pg: PgConfig, ms: MsConfig):
        self.pg = pg
        self.ms = ms

    def source(self, database: str | None = None, autocommit: bool = True):
        import psycopg2
        from psycopg2.extras import register_uuid
        conn = psycopg2.connect(
            host=self.pg.host, port=self.pg.port,
            dbname=database or self.pg.dbname or "postgres",
            user=self.pg.user, password=self.pg.password,
            sslmode=self.pg.sslmode, connect_timeout=10,
        )
        register_uuid(conn_or_curs=conn)
        conn.autocommit = autocommit
        return conn

    def target(self, database: str | None = None):
        import pyodbc
        conn = pyodbc.connect(self.ms.dsn(database), timeout=10, autocommit=False)
        # No setencoding/setdecoding overrides here, deliberately. SQL Server's
        # wide types are UTF-16LE, and pyodbc already defaults to that. Forcing
        # utf-8 on SQL_WCHAR sends a 32-char uuid as 32 bytes that the server
        # reads as 16 wide chars and truncates, so distinct keys arrive equal:
        # "0FEB8D..." comes back as "䘰䉅䐸" and collides on the primary key.
        return conn

    def target_database_exists(self, name: str) -> bool:
        with closing(self.target(database="master")) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM sys.databases WHERE name = ?", (name,))
            return cur.fetchone() is not None

    def create_target_database(self, name: str):
        """CREATE DATABASE, which SQL Server refuses to run inside a transaction,
        hence its own autocommit connection."""
        if "]" in name or not name.strip():
            raise ValueError(f"Unusable database name: {name!r}")
        conn = self.target(database="master")
        conn.autocommit = True
        try:
            conn.cursor().execute(f"CREATE DATABASE [{name}]")
        finally:
            conn.close()

    def ensure_target_database(self, name: str) -> bool:
        """Returns True when the database had to be created."""
        if self.target_database_exists(name):
            return False
        self.create_target_database(name)
        return True

    def ensure_target_schema(self):
        """dbo always exists; a named schema may not."""
        if self.ms.schema.lower() == "dbo":
            return
        with closing(self.target()) as conn:
            conn.autocommit = True
            conn.cursor().execute(
                "IF SCHEMA_ID(?) IS NULL EXEC(?)",
                (self.ms.schema, f"CREATE SCHEMA [{self.ms.schema}]"))

    # -- probes used by the Connect stage ---------------------------------
    def probe_source(self) -> dict:
        with closing(self.source(database="postgres")) as c:
            cur = c.cursor()
            cur.execute("SELECT version(), current_database()")
            version, db = cur.fetchone()
            cur.execute("SELECT datname FROM pg_database "
                        "WHERE NOT datistemplate ORDER BY datname")
            databases = [r[0] for r in cur.fetchall()]
        return {"version": version.split(" on ")[0], "database": db,
                "databases": databases}

    def probe_target(self) -> dict:
        conn = self.target(database="master")
        cur = conn.cursor()
        cur.execute("SELECT @@VERSION, DB_NAME()")
        version, db = cur.fetchone()
        cur.execute("SELECT name FROM sys.databases "
                    "WHERE database_id > 4 ORDER BY name")
        databases = [r[0] for r in cur.fetchall()]
        rcsi = None
        collation = None
        if self.ms.database:
            cur.execute(
                "SELECT is_read_committed_snapshot_on, collation_name "
                "FROM sys.databases WHERE name = ?", (self.ms.database,))
            row = cur.fetchone()
            if row:
                rcsi, collation = bool(row[0]), row[1]
        conn.close()
        return {"version": version.splitlines()[0].strip(), "database": db,
                "databases": databases, "rcsi": rcsi, "collation": collation}


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------
class Introspector:
    def __init__(self, conns: Connections):
        self.conns = conns

    def discover(self, progress: Callable[[str], None] | None = None) -> list[Table]:
        tables: dict[str, Table] = {}

        src = self.conns.source()
        cur = src.cursor()
        cur.execute("""
            SELECT c.relname, COALESCE(c.reltuples, 0)::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND n.nspname = %s
            ORDER BY c.relname
        """, (self.conns.pg.schema,))
        for name, est in cur.fetchall():
            tables[name] = Table(name=name, source_rows=max(int(est), 0))

        cur.execute("""
            SELECT c.table_name, c.column_name, c.data_type, c.is_nullable,
                   c.character_maximum_length, c.numeric_precision, c.numeric_scale,
                   CASE WHEN c.is_identity = 'YES' THEN 'GENERATED IDENTITY'
                        ELSE c.column_default END,
                   COALESCE(pk.is_pk, false)
            FROM information_schema.columns c
            LEFT JOIN (
                SELECT tc.table_name, kcu.column_name, true AS is_pk
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON kcu.constraint_name = tc.constraint_name
                 AND kcu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = %s
            ) pk ON pk.table_name = c.table_name AND pk.column_name = c.column_name
            WHERE c.table_schema = %s
            ORDER BY c.table_name, c.ordinal_position
        """, (self.conns.pg.schema, self.conns.pg.schema))
        for (tbl, col, dtype, nullable, maxlen, prec, scale, default, is_pk) in cur.fetchall():
            if tbl in tables:
                tables[tbl].columns.append(Column(
                    name=col, pg_type=dtype, nullable=(nullable == "YES"),
                    is_pk=bool(is_pk), max_length=maxlen,
                    precision=prec, scale=scale, default=default,
                ))

        # Exact counts, one statement for the whole schema.
        if tables:
            if progress:
                progress("Counting source rows")
            union = " UNION ALL ".join(
                f'SELECT {_lit(t)} AS t, COUNT(*) AS n FROM "{self.conns.pg.schema}"."{t}"'
                for t in tables
            )
            cur.execute(union)
            for name, n in cur.fetchall():
                tables[name].source_rows = int(n)
        src.close()

        if progress:
            progress("Reading target schema")
        tgt = self.conns.target()
        tcur = tgt.cursor()
        tcur.execute("""
            SELECT t.name,
                   OBJECTPROPERTY(t.object_id, 'TableHasIdentity'),
                   SUM(CASE WHEN p.index_id IN (0,1) THEN p.rows ELSE 0 END)
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            LEFT JOIN sys.partitions p ON p.object_id = t.object_id
            WHERE s.name = ?
            GROUP BY t.name, t.object_id
        """, (self.conns.ms.schema,))
        for name, has_identity, rows in tcur.fetchall():
            if name in tables:
                tables[name].target_exists = True
                tables[name].target_has_identity = bool(has_identity)
                tables[name].target_rows = int(rows or 0)
        existing = [t for t in tables.values() if t.target_exists]
        if existing:
            union = " UNION ALL ".join(
                f"SELECT {_lit(t.name)}, COUNT_BIG(*) FROM [{self.conns.ms.schema}].[{t.name}]"
                for t in existing)
            tcur.execute(union)
            for name, count in tcur.fetchall():
                tables[name].target_rows = int(count)
        tgt.close()

        for t in tables.values():
            if t.name in PROTECTED_TABLES:
                t.selected = False
        return list(tables.values())


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def fetch(conn, sql: str, params: tuple = ()) -> list:
    """Run a query, take every row, close the cursor.

    SQL Server without MARS allows one active result set per connection, so a
    cursor left holding unread rows makes the next command on that connection
    fail with "Connection is busy with results for another command". Preflight
    and Verifier share one connection across many checks, so every read goes
    through here rather than leaving that to each caller's discipline.
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall()
    finally:
        cur.close()


def fetch_one(conn, sql: str, params: tuple = ()):
    rows = fetch(conn, sql, params)
    return rows[0] if rows else None


def format_bytes(value: int | float | None) -> str:
    """Decimal units, so MB and GB have their standard SI meaning."""
    if value is None:
        return "Unavailable"
    size = max(0.0, float(value))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1000 or unit == "PB":
            return f"{int(size)} B" if unit == "B" else f"{size:,.2f} {unit}"
        size /= 1000


def estimate_transfer_size(conns: Connections, tables: list[Table], options: Options,
                           sample_rows: int = 1000) -> dict:
    """Estimate selected source-value bytes from a bounded sample per table.

    Text uses PostgreSQL's text representation, bytea its raw length. This is
    not database disk space, SQL Server allocation, or measured network traffic.
    Existing schema-discovery counts scale each sample; filters get fresh counts.
    """
    if sample_rows < 1 or sample_rows > 10000:
        raise ValueError("Size sample must contain between 1 and 10,000 rows.")
    chosen = [t for t in tables if t.selected and t.does_data(options)]
    report = {"database_bytes": None, "estimated_bytes": 0, "rows": 0,
              "tables": len(chosen), "sampled_rows": 0, "errors": [], "per_table": []}
    quote = lambda value: '"' + value.replace('"', '""') + '"'
    with closing(conns.source()) as source:
        try:
            report["database_bytes"] = int(fetch_one(source, "SELECT pg_database_size(current_database())")[0])
        except Exception:
            pass  # Disk-size privilege is independent of permission to read data.
        for table in chosen:
            try:
                columns = [c for c in table.columns if c.name in table.migrating_columns]
                if not columns:
                    raise ValueError("No columns selected")
                qualified = quote(conns.pg.schema) + "." + quote(table.name)
                where = f" WHERE {table.row_filter}" if table.row_filter else ""
                count = table.source_rows
                if where:
                    count = int(fetch_one(source, f"SELECT COUNT(*) FROM {qualified}{where}")[0])
                expressions = [f"COALESCE(OCTET_LENGTH({quote(c.name)}" +
                               ("" if c.pg_type == "bytea" else "::text") + "), 0)::bigint"
                               for c in columns]
                average, sampled = fetch_one(source,
                    "SELECT COALESCE(AVG(row_bytes), 0), COUNT(*) FROM (SELECT "
                    + " + ".join(expressions) + f" AS row_bytes FROM {qualified}{where} LIMIT %s) AS sample",
                    (sample_rows,))
                if count and not sampled:
                    raise ValueError("Source rows changed since schema discovery; reload the schema")
                size = round(float(average) * count)
                report["rows"] += count
                report["estimated_bytes"] += size
                report["sampled_rows"] += int(sampled)
                report["per_table"].append({"table": table.name, "rows": count, "estimated_bytes": size})
            except Exception as exc:
                report["errors"].append(f"{table.name}: {exc}")
    if report["errors"]:
        report["estimated_bytes"] = None  # Never present an incomplete sum as the total.
    return report


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
class Preflight:
    """Every check that can be answered before a single row moves."""

    def __init__(self, conns: Connections, tables: list[Table], options: Options):
        self.conns = conns
        self.tables = [t for t in tables if t.selected]
        self.options = options

    def _target_columns(self, table: Table, tgt) -> dict[str, dict]:
        """Target column definitions, once per table, keyed by name."""
        cached = self._tgt_cols.get(table.name)
        if cached is not None:
            return cached
        rows = fetch(tgt, """
            SELECT c.name, ty.name, c.max_length, c.precision, c.scale,
                   c.is_nullable, c.is_identity,
                   OBJECT_DEFINITION(c.default_object_id)
            FROM sys.columns c
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            JOIN sys.tables t ON t.object_id = c.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE t.name = ? AND s.name = ?
            ORDER BY c.column_id
        """, (table.name, self.conns.ms.schema))
        cols = {r[0]: {"type": r[1], "max_length": r[2], "precision": r[3],
                       "scale": r[4], "nullable": bool(r[5]),
                       "identity": bool(r[6]), "default": r[7]}
                for r in rows}
        self._tgt_cols[table.name] = cols
        return cols

    def _source_column(self, table: Table, name: str) -> Column | None:
        return next((c for c in table.columns if c.name == name), None)

    def _column_facts(self, table: Table, name: str, tgt) -> dict:
        """The same column seen from both sides — the heart of every finding."""
        facts = {"Column": f"{table.name}.{name}"}
        src = self._source_column(table, name)
        facts["Source column"] = (describe_pg_column(src) if src
                                  else "not present in the source")
        target = self._target_columns(table, tgt).get(name)
        facts["Target column"] = (describe_ms_column(target) if target
                                  else "not present in the target")
        if src:
            mapped = TYPE_MAP.get(src.pg_type)
            facts["Type mapping"] = (f"{src.pg_type} \u2192 {mapped}" if mapped
                                     else f"{src.pg_type} \u2192 no mapping")
        return facts

    def run(self, progress: Callable[[str], None] | None = None) -> list[Issue]:
        issues: list[Issue] = []
        self._tgt_cols: dict[str, dict] = {}
        if not self.tables:
            return [Issue("stop", "—", "Selection", "No tables selected.",
                          "Choose at least one table on the Tables stage.")]

        src = self.conns.source()
        tgt = self.conns.target()

        issues += self._database_settings(tgt)
        for i, table in enumerate(self.tables):
            if progress:
                progress(f"Checking {table.name} ({i + 1}/{len(self.tables)})")
            issues += self._table_exists(table)
            # Source-side, so it is worth knowing even for a table that does
            # not exist on the target yet.
            issues += self._unsupported_types(table)
            if not table.target_exists:
                if self.options.with_schema:
                    for col in table.columns:
                        try:
                            sql_type(col)
                        except ValueError as exc:
                            issues.append(Issue("stop", table.name, "Type mapping", str(exc),
                                                "Create a reviewed target column type before migrating.",
                                                columns=[col.name], fix={"plans": ["skip"]}))
                continue
            issues += self._columns_align(table, tgt)
            issues += self._target_not_empty(table)
            issues += self._nullable_unique(table, src, tgt)
            issues += self._index_key_size(table, tgt)
            issues += self._identity_expectation(table, tgt)
            issues += self._key_collisions(table, src, tgt)

        src.close()
        tgt.close()
        return issues

    # -- individual checks -------------------------------------------------
    def _database_settings(self, tgt) -> list[Issue]:
        out = []
        row = fetch_one(tgt, "SELECT is_read_committed_snapshot_on, collation_name "
                             "FROM sys.databases WHERE name = DB_NAME()")
        self.collation = (row[1] if row else None) or ""
        if row:
            rcsi, collation = bool(row[0]), row[1]
            db = self.conns.ms.database
            if not rcsi:
                out.append(Issue(
                    "warn", "—", "Snapshot isolation",
                    "READ_COMMITTED_SNAPSHOT is off. SQL Server will use locks where "
                    "Postgres used MVCC, so readers and writers will block each other.",
                    f"ALTER DATABASE [{db}] SET READ_COMMITTED_SNAPSHOT ON "
                    "WITH ROLLBACK IMMEDIATE;\n\n"
                    "Run it in a maintenance window: ROLLBACK IMMEDIATE kills open "
                    "transactions.",
                    {"Database": f"{db} on {self.conns.ms.server}",
                     "READ_COMMITTED_SNAPSHOT": "OFF",
                     "Source behaviour": "PostgreSQL MVCC — readers never blocked",
                     "Target behaviour": "shared locks — readers block writers"}))
            if collation and "_CI_" in collation:
                out.append(Issue(
                    "warn", "—", "Collation",
                    f"Target collation {collation} is case-insensitive. Postgres "
                    "comparisons were case-sensitive, so lookups and uniqueness "
                    "will behave differently after cutover.",
                    "Decide deliberately: rebuild with a _CS_AS collation, or audit "
                    "every uniqueness and authorization check that assumed case.",
                    {"Database": f"{db} on {self.conns.ms.server}",
                     "Target collation": collation,
                     "Case sensitivity": "insensitive (_CI_)",
                     "Source": "PostgreSQL default collation is case-sensitive",
                     "Consequence": "'Ali' and 'ali' collide on the target and "
                                    "did not on the source"}))
        return out

    def _table_exists(self, table: Table) -> list[Issue]:
        if table.target_exists:
            return []
        pk = [c.name for c in table.columns if c.is_pk]
        unmapped = [c.name for c in table.columns if c.pg_type not in TYPE_MAP]
        facts = {"Target database": f"{self.conns.ms.database} on "
                                    f"{self.conns.ms.server}",
                 "Target schema": self.conns.ms.schema,
                 "Source columns": str(len(table.columns)),
                 "Source rows": f"{table.source_rows:,}",
                 "Primary key": ", ".join(pk) or "none detected",
                 "Identity on create": ", ".join(
                     c.name for c in table.columns if is_serial(c)) or "none",
                 "Columns needing review": ", ".join(unmapped) or "none"}
        if self.options.with_schema:
            return [Issue(
                "note", table.name, "Will be created",
                "No matching table in the target database; the transfer creates "
                "it from the source shape.",
                "Columns, primary key and identity only — no foreign keys, "
                "indexes, defaults or check constraints. Run `manage.py migrate` "
                "against the target instead if you want Django's own schema.",
                facts, unmapped or pk)]
        return [Issue(
            "stop", table.name, "Missing target table",
            "No matching table in the target database, and this run is data only.",
            "Switch the migrate mode to schema + data on the Tables stage, run "
            "`manage.py migrate` against the target first, or run the assisted "
            "DDL below by hand.", facts, unmapped or pk,
            {"plans": ["schema", "skip"],
             "note": "creating it here gives columns, primary key and identity "
                     "only — no foreign keys, indexes or defaults"})]

    def _columns_align(self, table: Table, tgt) -> list[Issue]:
        target_cols = self._target_columns(table, tgt)
        source_cols = set(table.column_names)
        out = []
        missing = source_cols - set(target_cols)
        if missing:
            facts = {"Columns missing on the target": str(len(missing))}
            for name in sorted(missing):
                src_col = self._source_column(table, name)
                mapped = TYPE_MAP.get(src_col.pg_type) if src_col else None
                facts[f"  {name}"] = (
                    f"{describe_pg_column(src_col)}  \u2192  needs "
                    f"{mapped or 'a reviewed type'} on the target"
                    if src_col else "unknown")
            facts["Target has"] = ", ".join(sorted(target_cols)) or "no columns"
            out.append(Issue(
                "stop", table.name, "Column mismatch",
                f"Present in source but not target: {', '.join(sorted(missing))}.",
                "The target schema is behind. Apply the outstanding migrations "
                "(`manage.py migrate`), or add the columns by hand with the "
                "definitions listed above.", facts, sorted(missing),
                {"plans": ["clean", "skip"],
                 "exclude_columns": sorted(missing),
                 "note": f"sends the {len(target_cols)} column(s) both sides "
                         f"share and drops {', '.join(sorted(missing))}; "
                         "that data does not arrive"}))
        extra = set(target_cols) - source_cols
        if extra:
            blocking = {n for n in extra
                        if not target_cols[n]["nullable"]
                        and not target_cols[n]["identity"]
                        and not target_cols[n]["default"]}
            facts = {}
            for name in sorted(extra):
                facts[f"  {name}"] = describe_ms_column(target_cols[name])
            if blocking:
                facts["Blocking"] = (", ".join(sorted(blocking))
                                     + " — NOT NULL, no default, not identity")
                out.append(Issue(
                    "stop", table.name, "Column mismatch",
                    f"Target requires {', '.join(sorted(blocking))}, which the "
                    "source cannot supply and which has no default.",
                    "Give the column a database default, make it nullable, or "
                    "drop it if the migration made it obsolete.", facts,
                    sorted(blocking),
                    {"plans": ["skip"],
                     "note": "no filter helps: the target demands a value the "
                             "source has not got"}))
            else:
                out.append(Issue(
                    "note", table.name, "Extra target columns",
                    f"Target has {', '.join(sorted(extra))}; these will take their "
                    "defaults.", "", facts, sorted(extra)))
        return out

    def _unsupported_types(self, table: Table) -> list[Issue]:
        bad = [c for c in table.columns
               if c.pg_type in UNSUPPORTED_TYPES or c.pg_type.endswith("[]")]
        if not bad:
            return []
        facts = {"Columns affected": str(len(bad))}
        for c in bad:
            facts[f"  {c.name}"] = describe_pg_column(c)
        facts["Why"] = ("SQL Server has no column type that holds these values "
                        "without a remodel")
        return [Issue(
            "stop", table.name, "Unsupported type",
            ", ".join(f"{c.name} ({c.pg_type})" for c in bad),
            "Arrays become a child table keyed back to this one; hstore/json "
            "become nvarchar(max); tsvector is dropped and rebuilt on SQL Server "
            "full-text. Deselect this table until the model is changed.", facts,
            [c.name for c in bad],
            {"plans": ["clean", "schema", "skip"],
             "exclude_columns": [c.name for c in bad],
             "note": "moves every other column and leaves these empty on the "
                     "target; only safe where the target column is nullable "
                     "or absent"})]

    def _target_not_empty(self, table: Table) -> list[Issue]:
        if table.target_rows == 0:
            return []
        pk = [c.name for c in table.columns if c.is_pk]
        facts = {"Rows in target now": f"{table.target_rows:,}",
                 "Rows to load from source": f"{table.source_rows:,}",
                 "Primary key": ", ".join(pk) or "none detected",
                 "Clear target before load": "on" if self.options.clear_target
                                             else "off"}
        if self.options.clear_target:
            return [Issue(
                "note", table.name, "Target holds rows",
                f"{table.target_rows:,} existing rows will be deleted before load.",
                "", facts, pk)]
        return [Issue(
            "stop", table.name, "Target holds rows",
            f"{table.target_rows:,} rows already present and 'Clear target' is off. "
            "Primary keys will collide.",
            "Turn on 'Clear target tables before load' on the Transfer stage, or "
            "deselect this table.", facts, pk,
            {"plans": ["schema", "skip"],
             "note": "the existing rows stay; only a schema pass or skipping "
                     "avoids the key collision"})]

    def _nullable_unique(self, table: Table, src, tgt) -> list[Issue]:
        """Postgres permits many NULLs in a unique column; SQL Server permits one."""
        candidates = fetch(src, """
            SELECT a.attname, ci.relname
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_class ci ON ci.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE i.indisunique AND NOT i.indisprimary
              AND NOT a.attnotnull AND c.relname = %s AND n.nspname = %s
        """, (table.name, self.conns.pg.schema))
        out = []
        for col, index_name in candidates:
            nulls = fetch_one(
                src, f'SELECT COUNT(*) FROM "{self.conns.pg.schema}".'
                     f'"{table.name}" WHERE "{col}" IS NULL')[0]
            if nulls > 1:
                facts = self._column_facts(table, col, tgt)
                facts["Source unique index"] = f"{index_name} (unique, not primary)"
                facts["NULLs in source"] = (
                    f"{nulls:,} of {table.source_rows:,} rows"
                    + (f" ({nulls / table.source_rows:.1%})"
                       if table.source_rows else ""))
                facts["SQL Server limit"] = "one NULL per unique index"
                facts["Rows that would fail"] = (
                    f"{nulls - 1:,} — the first NULL inserts, every later one "
                    "violates the index")
                target = self._target_columns(table, tgt).get(col)
                facts["Target index"] = (
                    self._ms_index_for(table, col, tgt)
                    or ("no unique index on this column yet"
                        if target else "column does not exist on the target"))
                out.append(Issue(
                    "stop", table.name, "Nullable unique column",
                    f"{col} holds {nulls:,} NULLs; SQL Server allows one NULL in a "
                    f"unique index, so {nulls - 1:,} rows would be rejected.",
                    "Either make the target index filtered:\n\n"
                    f"  CREATE UNIQUE INDEX [UX_{table.name}_{col}]\n"
                    f"    ON [{self.conns.ms.schema}].[{table.name}] ([{col}])\n"
                    f"    WHERE [{col}] IS NOT NULL;\n\n"
                    "(drop the existing unique index or constraint on that column "
                    "first), or clean the source so at most one NULL remains:\n\n"
                    f"  SELECT * FROM \"{self.conns.pg.schema}\".\"{table.name}\" "
                    f"WHERE \"{col}\" IS NULL;",
                    facts, [col],
                    {"plans": ["clean", "schema", "skip"],
                     "row_filter": f'"{col}" IS NOT NULL',
                     "note": f"leaves {nulls:,} row(s) where {col} is NULL "
                             f"behind; {table.source_rows - nulls:,} move"}))
        return out

    def _ms_index_for(self, table: Table, column: str, tgt) -> str:
        """Whichever target index already keys on this column."""
        rows = fetch(tgt, """
            SELECT i.name, i.is_unique, i.has_filter
            FROM sys.indexes i
            JOIN sys.index_columns ic ON ic.object_id = i.object_id
                                     AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id
                              AND c.column_id = ic.column_id
            JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE t.name = ? AND s.name = ? AND c.name = ?
        """, (table.name, self.conns.ms.schema, column))
        found = [f"{n} ({'unique' if u else 'non-unique'}"
                 f"{', filtered' if f else ''})" for n, u, f in rows]
        return ", ".join(found)

    def _index_key_size(self, table: Table, tgt) -> list[Issue]:
        rows = fetch(tgt, """
            SELECT i.name, c.name, c.max_length, ty.name
            FROM sys.indexes i
            JOIN sys.index_columns ic ON ic.object_id = i.object_id
                                     AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id
                              AND c.column_id = ic.column_id
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE t.name = ? AND s.name = ? AND i.is_unique = 1
              AND ic.is_included_column = 0
            ORDER BY i.name, ic.key_ordinal
        """, (table.name, self.conns.ms.schema))
        by_index: dict[str, list] = {}
        for index_name, col, length, kind in rows:
            by_index.setdefault(index_name, []).append((col, length, kind))
        out = []
        for index_name, cols in by_index.items():
            size = sum(max(length, 0) for _c, length, _k in cols)
            if size <= 900:
                continue
            facts = {"Index": index_name,
                     "Key bytes": f"{size} (SQL Server allows 900)",
                     "Over by": f"{size - 900} bytes"}
            for col, length, kind in cols:
                facts[f"  {col}"] = f"{kind}, {length} bytes"
            out.append(Issue(
                "warn", table.name, "Index key size",
                f"Unique index {index_name} keys on {size} bytes across "
                f"{len(cols)} column(s); the limit is 900, so inserts past it "
                "will fail.",
                "Shorten the widest column, index a checksum or hash of it "
                "instead, or drop the column from the key.", facts,
                [c for c, _l, _k in cols]))
        return out

    def _target_key(self, table: Table, tgt) -> tuple[str, list[str]]:
        """The target's primary key: its name and the columns it spans."""
        rows = fetch(tgt, """
            SELECT i.name, c.name
            FROM sys.indexes i
            JOIN sys.index_columns ic ON ic.object_id = i.object_id
                                     AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id
                              AND c.column_id = ic.column_id
            JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE t.name = ? AND s.name = ? AND i.is_primary_key = 1
            ORDER BY ic.key_ordinal
        """, (table.name, self.conns.ms.schema))
        if not rows:
            return "", []
        return rows[0][0], [r[1] for r in rows]

    def _key_collisions(self, table: Table, src, tgt) -> list[Issue]:
        """Rows that are distinct in Postgres but equal under the target's key.

        This is the check "Cannot insert duplicate key" needs. That error fails
        a transfer part-way through, after other tables have already loaded,
        and its cause is always visible before a single row moves:

          * the target's primary key spans fewer columns than the source's, so
            rows the source kept apart share one target key;
          * the target collation is case- or accent-insensitive while Postgres
            compared byte for byte, so 'Ali' and 'ali' become one key.
        """
        key_name, target_key = self._target_key(table, tgt)
        if not target_key:
            return []
        source_cols = {c.name: c for c in table.columns}
        if any(name not in source_cols for name in target_key):
            return []                      # a column mismatch, reported already

        sending = set(table.migrating_columns)
        missing = [n for n in target_key if n not in sending]
        if missing:
            return [Issue(
                "stop", table.name, "Key column not sent",
                f"The target primary key {key_name} needs "
                f"{', '.join(missing)}, which this plan excludes.",
                "A key column cannot be left out. Choose another plan for this "
                "table, or skip it.",
                {"Target primary key": f"{key_name} ({', '.join(target_key)})",
                 "Excluded columns": ", ".join(table.exclude_columns) or "none"},
                missing, {"plans": ["skip"]})]

        insensitive = any(m in self.collation.upper() for m in ("_CI", "_AI"))
        narrower = len(target_key) < len([c for c in table.columns if c.is_pk])
        if not insensitive and not narrower:
            return []

        # Group the source by the target's key, compared the way the target
        # will compare it, and see whether any group holds more than one row.
        def norm(name: str) -> str:
            col = source_cols[name]
            texty = col.pg_type in ("text", "character varying", "varchar",
                                    "character", "bpchar", "uuid", "citext")
            return (f'lower("{name}"::text)' if insensitive and texty
                    else f'"{name}"::text')

        grouped = ", ".join(norm(n) for n in target_key)
        where = f" WHERE {table.row_filter}" if table.row_filter else ""
        table_ref = f'"{self.conns.pg.schema}"."{table.name}"'
        rows = fetch(src, (
            f"SELECT {grouped}, COUNT(*) AS n FROM {table_ref}{where} "
            f"GROUP BY {grouped} HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 5"))
        if not rows:
            return []
        extra = fetch_one(src, (
            "SELECT COALESCE(SUM(n - 1), 0) FROM (SELECT COUNT(*) AS n FROM "
            f"{table_ref}{where} GROUP BY {grouped} HAVING COUNT(*) > 1) d"))[0]

        facts = {"Target primary key": f"{key_name} ({', '.join(target_key)})",
                 "Source primary key": ", ".join(
                     c.name for c in table.columns if c.is_pk) or "none",
                 "Rows that would be rejected": f"{extra:,}",
                 "Compared as": grouped}
        if insensitive:
            facts["Target collation"] = (
                f"{self.collation} — ignores case or accents; "
                "PostgreSQL compared byte for byte")
        if narrower:
            facts["Key width"] = (
                f"target keys on {len(target_key)} column(s), the source on "
                f"{len([c for c in table.columns if c.is_pk])}")
        for row in rows:
            value = ", ".join(str(v) for v in row[:-1])
            facts[f"  {value}"] = f"{row[-1]:,} source rows"

        return [Issue(
            "stop", table.name, "Duplicate target key",
            f"{extra:,} row(s) collide on the target primary key {key_name}; "
            "the load would fail part-way through.",
            "Make the key unique the way the target compares it, rebuild the "
            "target with a case-sensitive collation, or widen the target key. "
            "To list them:\n\n"
            f"  SELECT {grouped}, COUNT(*)\n"
            f"  FROM {table_ref}\n"
            f"  GROUP BY {grouped} HAVING COUNT(*) > 1;",
            facts, list(target_key), {"plans": ["schema", "skip"]})]

    def _identity_expectation(self, table: Table, tgt) -> list[Issue]:
        pk = [c for c in table.columns if c.is_pk]
        if not (self.options.preserve_identity and table.target_has_identity
                and len(pk) == 1
                and pk[0].pg_type not in ("bigint", "integer", "smallint")):
            return []
        target_cols = self._target_columns(table, tgt)
        identity = next((f"{n}: {describe_ms_column(c)}"
                         for n, c in target_cols.items() if c["identity"]),
                        "unknown")
        facts = self._column_facts(table, pk[0].name, tgt)
        facts["Target identity column"] = identity
        facts["Preserve primary keys"] = "on (IDENTITY_INSERT will be used)"
        facts["Risk"] = ("IDENTITY_INSERT only accepts values the identity "
                         "column's type can hold")
        return [Issue(
            "warn", table.name, "Identity mismatch",
            f"Target has an identity column but the source primary key "
            f"{pk[0].name} is {pk[0].pg_type}, which is not an integer type.",
            "Check that every source key fits the target identity column, or "
            "turn off 'Preserve primary keys' and let the target assign new "
            "ones — only safe if nothing references these keys.", facts,
            [pk[0].name])]

    # -- assisted DDL ------------------------------------------------------
    def generate_ddl(self, table: Table) -> str:
        return generate_ddl(self.conns.ms.schema, table)


def checkpoint_path_for(output_dir: str, pair: list[str] | None = None) -> str:
    """Finds pair-specific checkpoint file if it exists, or fallback to standard checkpoint.json."""
    if pair and len(pair) >= 2 and pair[0] and pair[1]:
        pair_name = f"checkpoint_{pair[0]}_{pair[1]}.json"
        p = os.path.join(output_dir, pair_name)
        if os.path.exists(p):
            return p
    return os.path.join(output_dir, "checkpoint.json")


def read_checkpoint(output_dir: str, pair: list[str]) -> dict | None:
    """What an earlier run of this pair left behind, or None if there is nothing
    to resume. Lets the UI offer a resume without building a Transport."""
    paths_to_check = []
    if pair and len(pair) >= 2 and pair[0] and pair[1]:
        paths_to_check.append(os.path.join(output_dir, f"checkpoint_{pair[0]}_{pair[1]}.json"))
    paths_to_check.append(os.path.join(output_dir, "checkpoint.json"))

    for path in paths_to_check:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            continue
        tables = saved.get("tables") or {}
        if not tables or saved.get("pair", pair) != pair:
            continue
        done = [k for k, v in tables.items() if v.get("status") == "done"]
        unfinished = [k for k, v in tables.items() if v.get("status") != "done"]
        if not done and not unfinished:
            continue
        return {"updated": saved.get("updated", ""), "mode": saved.get("mode", "both"),
                "done": sorted(done), "unfinished": sorted(unfinished),
                "path": path}
    return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def sql_type(c: Column) -> str:
    base = TYPE_MAP.get(c.pg_type)
    if base is None:
        t_lower = c.pg_type.lower()
        if "enum" in t_lower or "status" in t_lower:
            return "nvarchar(64)"
        return "nvarchar(max) /* review: unmapped " + c.pg_type + " */"
    if "{n}" in base:
        n = c.max_length
        if n is None or n > 4000:
            return "nvarchar(max)"
        return base.format(n=n)
    if "{p}" in base:
        if c.precision is None or c.scale is None:
            raise ValueError(f"{c.name}: unconstrained numeric requires an explicit precision and scale")
        precision, scale = c.precision or 38, c.scale or 0
        if precision > 38 or scale < 0 or scale > precision:
            raise ValueError(f"{c.name}: decimal({precision},{scale}) requires an explicit SQL Server mapping")
        return base.format(p=precision, s=scale)
    return base


def is_serial(c: Column) -> bool:
    """A Postgres serial/identity key, which becomes IDENTITY on the target so
    the application keeps getting keys after cutover."""
    return (c.is_pk and TYPE_MAP.get(c.pg_type) in ("int", "bigint", "smallint")
            and (c.default or "").startswith(("nextval(", "GENERATED", "generated")))


def generate_ddl(schema: str, table: Table) -> str:
    """CREATE TABLE for one table: columns, primary key, identity.

    ponytail: tables, PKs and identity only — no foreign keys, indexes, check
    constraints or defaults. That is deliberate: Django migrations remain the
    schema source of truth, and this covers the "empty target, get me moving"
    case. Add the rest here if a non-Django source ever needs it.
    """
    body = []
    for c in table.columns:
        identity = " IDENTITY(1,1)" if is_serial(c) else ""
        body.append(f"    [{c.name}] {sql_type(c)}{identity} "
                    f"{'NULL' if c.nullable else 'NOT NULL'}")
    pk = [c.name for c in table.columns if c.is_pk]
    if pk:
        cols = ", ".join(f"[{p}]" for p in pk)
        body.append(f"    CONSTRAINT [PK_{table.name}] PRIMARY KEY ({cols})")
    return (f"CREATE TABLE [{schema}].[{table.name}] (\n"
            + ",\n".join(body) + "\n);")


class SchemaBuilder:
    """Creates the target tables that do not exist yet, from the source shape."""

    def __init__(self, conns: Connections, tables: list[Table], options: Options):
        self.conns = conns
        self.tables = tables
        self.options = options

    def missing(self) -> list[Table]:
        return [t for t in self.tables if not t.target_exists]

    def create_missing(self, log: Callable[[str, str], None] | None = None) -> list[str]:
        pending = self.missing()
        if not pending:
            return []
        if self.options.dry_run:
            # "Write nothing" includes not connecting to write nothing.
            for table in pending:
                if log:
                    log("info", f"Would create [{table.name}]")
            return []
        self.conns.ensure_target_schema()
        created = []
        conn = self.conns.target()
        conn.autocommit = True
        try:
            for table in pending:
                ddl = generate_ddl(self.conns.ms.schema, table)
                conn.cursor().execute(ddl)
                # The table now exists, and the copy needs to know whether to
                # turn IDENTITY_INSERT on for it.
                table.target_exists = True
                table.target_has_identity = any(is_serial(c) for c in table.columns)
                table.target_rows = 0
                created.append(table.name)
                if log:
                    log("info", f"Created [{self.conns.ms.schema}].[{table.name}]")
        finally:
            conn.close()
        return created


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class Transport:
    """Moves rows. Emits events onto `self.events`; never touches the UI."""

    def __init__(self, conns: Connections, tables: list[Table], options: Options):
        self.conns = copy.copy(conns)
        self.conns.pg = copy.deepcopy(conns.pg)
        self.conns.ms = copy.deepcopy(conns.ms)
        self.tables = copy.deepcopy([t for t in tables if t.selected])
        self.options = copy.deepcopy(options)
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.running = threading.Event()
        self.running.set()
        self._lock = threading.Lock()
        self.results: dict[str, dict] = {}
        os.makedirs(options.output_dir, exist_ok=True)
        endpoint = [conns.pg.host, conns.pg.port, conns.pg.dbname, conns.pg.schema,
                    conns.ms.server, conns.ms.database, conns.ms.schema]
        digest = hashlib.sha256(json.dumps(endpoint).encode()).hexdigest()[:20]
        self.checkpoint_path = os.path.join(options.output_dir, f"checkpoint_{digest}.json")
        self.rejected: dict[str, int] = {}
        self.errors: list[str] = []
        # Tables a previous run left half-loaded: they are cleared before reload
        # whatever "Clear target tables" says, or the reload doubles the rows.
        self.force_clear: set[str] = set()

    # -- events ------------------------------------------------------------
    def emit(self, kind: str, **payload):
        self.events.put({"kind": kind, **payload})

    def log(self, level: str, message: str):
        self.emit("log", level=level, message=message)

    # -- checkpointing -----------------------------------------------------
    def load_checkpoint(self) -> dict:
        paths = [self.checkpoint_path, os.path.join(self.options.output_dir, "checkpoint.json")]
        for path in paths:
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    saved = json.load(fh)
                if not isinstance(saved, dict) or not isinstance(saved.get("tables", {}), dict):
                    raise ValueError("Invalid checkpoint structure")
                if any(not isinstance(v, dict) for v in saved.get("tables", {}).values()):
                    raise ValueError("Invalid checkpoint table results")
                return saved
            except (OSError, ValueError) as exc:
                raise ValueError("Checkpoint cannot be read. Turn off Resume to start a reviewed fresh run.") from exc
        return {}

    @property
    def pair(self) -> list[str]:
        return [self.conns.pg.dbname, self.conns.ms.database]

    @property
    def resume_context(self) -> dict:
        """Bind recovery to endpoints and the exact data selection, never secrets."""
        return {
            "source": [self.conns.pg.host, self.conns.pg.port,
                       self.conns.pg.dbname, self.conns.pg.schema],
            "target": [self.conns.ms.server, self.conns.ms.database, self.conns.ms.schema],
            "mode": self.options.mode,
            "dry_run": self.options.dry_run,
            "preserve_identity": self.options.preserve_identity,
            "tables": sorted([
                [t.name, t.plan, sorted(t.exclude_columns), t.row_filter,
                 t.migrating_columns] for t in self.tables
            ], key=lambda item: item[0]),
        }

    def save_checkpoint(self):
        """Written after every table, and atomically, so a kill at any moment
        leaves a readable file describing exactly what had finished."""
        if self.options.dry_run:
            return
        with self._lock:
            tmp = self.checkpoint_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"updated": datetime.datetime.now().isoformat(),
                           "pair": self.pair, "mode": self.options.mode,
                           "context": self.resume_context,
                           "errors": list(self.errors),
                           "tables": self.results}, fh, indent=2, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.checkpoint_path)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, name="pgbridge-transport",
                                  daemon=True)
        thread.start()
        return thread

    def pause(self):
        self.running.clear()
        self.log("warn", "Paused. In-flight batch will finish first.")

    def resume(self):
        self.running.set()
        self.log("info", "Resumed.")

    def stop(self):
        self.cancel.set()
        self.running.set()          # release anything blocked on pause
        self.log("warn", "Stopping after the current batch.")

    # -- main --------------------------------------------------------------
    def _run(self):
        started = time.time()
        pending = list(self.tables)
        try:
            self.options.validate()
            if not pending or not any(t.plan != "skip" for t in pending):
                raise ValueError("No tables selected for migration.")
            saved = self.load_checkpoint() if self.options.resume and not self.options.dry_run else {}
        except (ValueError, OSError) as exc:
            self.log("error", str(exc))
            self.emit("finished", cancelled=False, error=str(exc), tables_ok=0,
                      tables_total=len(pending), rows=0, elapsed=time.time() - started)
            return

        if self.options.resume and not self.options.dry_run:
            if not saved.get("tables") or saved.get("context") != self.resume_context or saved.get("errors"):
                self.log("error", "Resume refused: checkpoint server, schema, or plan "
                         "does not match (or is from an older version). Review the target "
                         "and explicitly start a fresh run with the required clearing policy.")
                self.emit("finished", cancelled=False, error="Checkpoint context mismatch",
                          tables_ok=0, tables_total=len(pending),
                          rows=0, elapsed=time.time() - started)
                return
            states = {k: v for k, v in saved.get("tables", {}).items()
                      if k in {t.name for t in self.tables}}
            done = {k for k, v in states.items() if v.get("status") == "done"}
            # Anything the checkpoint saw start but never finish was interrupted
            # part-way through its rows.
            self.force_clear = {k for k, v in states.items()
                                if v.get("status") in ("running", "failed", "stopped", "partial")}
            self.results.update({k: v for k, v in states.items() if k in done})
            if done:
                self.log("info", f"Resuming {' -> '.join(self.pair)}: "
                         f"{len(done)} tables already done, skipping them.")
                pending = [t for t in pending if t.name not in done]
            if self.force_clear:
                self.log("warn", f"{len(self.force_clear)} table(s) were "
                         "interrupted mid-load: "
                         + ", ".join(sorted(self.force_clear))
                         + ". They are cleared and reloaded.")
            if not done and not self.force_clear:
                self.log("info", "No checkpoint to resume from; starting fresh.")

        control = None
        try:
            building = [t for t in pending if t.does_schema(self.options)]
            if building:
                self.emit("phase", name="schema")
                created = SchemaBuilder(self.conns, building,
                                        self.options).create_missing(self.log)
                self.log("info", f"Schema: {len(created)} table(s) created."
                         if created else "Schema: every target table already existed.")

            # Tables doing no data are finished the moment the schema is in.
            moving = []
            for table in pending:
                if table.plan == "skip":
                    self.results[table.name] = {"status": "skipped", "rows": 0, "plan": "skip"}
                    continue
                if table.does_data(self.options):
                    moving.append(table)
                    continue
                self.results[table.name] = {
                    "status": "done", "rows": 0, "schema_only": True,
                    "plan": table.plan}
                self.emit("table_done", table=table.name, rows=0, seconds=0.0,
                          status="done")
                self.log("info", f"{table.name}: schema only, no rows moved.")
            pending = moving
            if not pending:
                return

            if self.options.disable_constraints and not self.options.dry_run:
                control = self.conns.target()
                self._set_constraints(control, pending, check=False)

            self.emit("phase", name="transfer")
            if self.options.workers > 1:
                with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
                    list(pool.map(self._safe_table, pending))
            else:
                for table in pending:
                    self._safe_table(table)

        except Exception as exc:                       # noqa: BLE001
            self.errors.append(str(exc))
            self.log("error", f"Transfer aborted: {exc}")
        finally:
            if control is None and self.options.reseed_identity and not self.options.dry_run and pending:
                try:
                    control = self.conns.target()
                except Exception as exc:
                    self.errors.append(f"Identity maintenance connection failed: {exc}")
            if control is not None:
                try:
                    self.emit("phase", name="constraints")
                    if self.options.disable_constraints:
                        self._set_constraints(control, pending, check=True)
                except Exception as exc:               # noqa: BLE001
                    self.errors.append(str(exc))
                    self.log("error",
                             f"Could not re-enable constraints: {exc}. "
                             f"Foreign keys are left disabled — fix before use.")
                if self.options.reseed_identity and not self.options.dry_run:
                    try:
                        self._reseed(control, pending)
                    except Exception as exc:           # noqa: BLE001
                        self.errors.append(f"Identity reseed failed: {exc}")
                        self.log("error", f"Identity reseed failed: {exc}")
                try:
                    control.close()
                except Exception as exc:
                    self.errors.append(str(exc))

            try:
                self.save_checkpoint()
            except OSError as exc:
                self.errors.append(f"Checkpoint write failed: {exc}")
            ok = sum(1 for r in self.results.values() if r["status"] == "done")
            rows = sum(r.get("rows", 0) for r in self.results.values())
            self.emit("finished",
                      elapsed=time.time() - started,
                      tables_ok=ok,
                      tables_total=len(self.tables),
                      rows=rows,
                      cancelled=self.cancel.is_set(),
                      error="; ".join(self.errors), dry_run=self.options.dry_run)

    def _safe_table(self, table: Table):
        if self.cancel.is_set():
            self.results[table.name] = {"status": "skipped", "rows": 0}
            return
        try:
            self._copy_table(table)
        except Exception as exc:                       # noqa: BLE001
            previous = self.results.get(table.name, {})
            self.results[table.name] = {**previous, "status": "failed",
                                        "rows": previous.get("rows", 0), "error": str(exc)}
            self.emit("table_error", table=table.name, message=str(exc))
            self.log("error", f"{table.name}: {exc}")
            if self.options.on_row_error == "abort":
                self.errors.append(f"{table.name}: {exc}")
                self.cancel.set()
        finally:
            try:
                self.save_checkpoint()
            except OSError as exc:
                self.errors.append(f"Checkpoint write failed: {exc}")
                self.cancel.set()

    def _copy_table(self, table: Table):
        columns = table.migrating_columns
        if not columns:
            raise ValueError(f"{table.name}: every column is excluded, "
                             "so there is nothing to send")
        where = f' WHERE {table.row_filter}' if table.row_filter else ""
        total = table.source_rows
        excluded = 0
        if table.row_filter:
            # Count what the filter leaves behind before moving anything: an
            # excluded row is a deliberate omission and has to be on the record.
            with closing(self.conns.source()) as count_conn:
                total = fetch_one(
                    count_conn,
                    f'SELECT COUNT(*) FROM "{self.conns.pg.schema}"."{table.name}"'
                    + where)[0]
            excluded = max(table.source_rows - total, 0)
            self.log("warn", f"{table.name}: leaving {excluded:,} row(s) behind "
                     f"({table.row_filter}); {total:,} will move.")
        if table.exclude_columns:
            self.log("warn", f"{table.name}: not sending "
                     + ", ".join(table.exclude_columns)
                     + " — the target keeps its own value for those.")

        self.emit("table_start", table=table.name, total=total)
        t0 = time.time()
        self.results[table.name] = {"status": "running", "rows": 0}
        self.save_checkpoint()

        src = self.conns.source(autocommit=False)
        tgt = None
        try:
            tgt = self.conns.target()
            tcur = tgt.cursor()
            interrupted = table.name in self.force_clear
            if (self.options.clear_target or interrupted) and not self.options.dry_run:
                if interrupted:
                    self.log("info", f"{table.name}: clearing a partial load "
                             "from the interrupted run.")
                tcur.execute(f"DELETE FROM [{self.conns.ms.schema}].[{table.name}]")
                tgt.commit()

            identity_on = (self.options.preserve_identity
                           and table.target_has_identity
                           and not self.options.dry_run)
            if identity_on:
                tcur.execute(
                    f"SET IDENTITY_INSERT [{self.conns.ms.schema}].[{table.name}] ON")

            if table.target_has_identity and not self.options.preserve_identity:
                identity = fetch_one(tgt, "SELECT c.name FROM sys.columns c "
                    "JOIN sys.tables t ON t.object_id=c.object_id "
                    "JOIN sys.schemas s ON s.schema_id=t.schema_id "
                    "WHERE s.name=? AND t.name=? AND c.is_identity=1",
                    (self.conns.ms.schema, table.name))
                if identity:
                    columns = [c for c in columns if c != identity[0]]
                if not columns:
                    raise ValueError("Cannot regenerate an identity-only table; enable Preserve primary keys.")
            collist = ", ".join(f"[{c}]" for c in columns)
            params = ", ".join("?" * len(columns))
            insert = (f"INSERT INTO [{self.conns.ms.schema}].[{table.name}] "
                      f"({collist}) VALUES ({params})")
            tcur.fast_executemany = self.options.fast_executemany

            select = ('SELECT ' + ", ".join(f'"{c}"' for c in columns) +
                      f' FROM "{self.conns.pg.schema}"."{table.name}"' + where)
            scur = src.cursor(name=f"pgbridge_{table.name}_{os.getpid()}")
            scur.itersize = self.options.chunk_size
            scur.execute(select)

            moved = 0
            while True:
                self.running.wait()
                if self.cancel.is_set():
                    self.log("warn", f"{table.name}: stopped at {moved:,} rows.")
                    break
                rows = scur.fetchmany(self.options.chunk_size)
                if not rows:
                    break
                batch = [[adapt(v) for v in row] for row in rows]
                if not self.options.dry_run:
                    moved += self._write_batch(tgt, tcur, insert, batch, table.name)
                else:
                    moved += len(batch)
                elapsed = max(time.time() - t0, 1e-6)
                self.results[table.name] = {"status": "running", "rows": moved}
                self.save_checkpoint()
                self.emit("progress", table=table.name, done=moved,
                          total=max(total, moved), rate=moved / elapsed)

            scur.close()
            if identity_on:
                tcur.execute(
                    f"SET IDENTITY_INSERT [{self.conns.ms.schema}].[{table.name}] OFF")
            tgt.commit()

            status = ("stopped" if self.cancel.is_set() else
                      "partial" if self.rejected.get(table.name, 0) else "done")
            self.results[table.name] = {
                "status": status, "rows": moved, "seconds": time.time() - t0,
                "plan": table.plan, "rows_rejected": self.rejected.get(table.name, 0)}
            if excluded:
                self.results[table.name]["rows_excluded"] = excluded
                self.results[table.name]["row_filter"] = table.row_filter
            if table.exclude_columns:
                self.results[table.name]["columns_excluded"] = \
                    list(table.exclude_columns)
            self.emit("table_done", table=table.name, rows=moved,
                      seconds=time.time() - t0, status=status)
        finally:
            try:
                src.close()
            finally:
                if tgt is not None:
                    tgt.close()

    def _write_batch(self, tgt, cursor, insert: str, batch: list, table: str) -> int:
        """Batch insert with retry, then per-row fallback so one bad row can't
        cost the whole batch."""
        attempt = 0
        while True:
            try:
                cursor.executemany(insert, batch)
                tgt.commit()
                return len(batch)
            except Exception as exc:                   # noqa: BLE001
                tgt.rollback()
                code = _sqlstate(exc)
                attempt += 1
                if code in RETRYABLE and attempt <= self.options.max_retries:
                    delay = self.options.retry_backoff ** attempt
                    self.log("warn", f"{table}: {code} on batch, retry "
                                     f"{attempt}/{self.options.max_retries} "
                                     f"in {delay:.0f}s")
                    if self.cancel.wait(delay):
                        raise RuntimeError("Stopped during batch retry") from exc
                    continue
                if self.options.on_row_error == "quarantine":
                    return self._row_by_row(tgt, cursor, insert, batch, table)
                raise _explain(exc, table)

    def _row_by_row(self, tgt, cursor, insert: str, batch: list, table: str) -> int:
        cursor.fast_executemany = False
        written, bad = 0, []
        for row in batch:
            try:
                cursor.execute(insert, row)
                tgt.commit()
                written += 1
            except Exception as exc:                   # noqa: BLE001
                tgt.rollback()
                bad.append((row, str(exc)))
        cursor.fast_executemany = self.options.fast_executemany
        if bad:
            self.rejected[table] = self.rejected.get(table, 0) + len(bad)
            path = os.path.join(self.options.output_dir, f"quarantine_{table}.csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if new:
                    writer.writerow(["error", "row"])
                for row, err in bad:
                    writer.writerow([err, json.dumps(row, default=str)])
            self.log("warn", f"{table}: {len(bad)} rows quarantined -> {path}")
        return written

    # -- constraint and identity handling ----------------------------------
    def _set_constraints(self, conn, tables: Iterable[Table], check: bool):
        verb = "WITH CHECK CHECK" if check else "NOCHECK"
        cur = conn.cursor()
        failed = []
        for table in tables:
            if not table.target_exists:
                continue
            try:
                cur.execute(
                    f"ALTER TABLE [{self.conns.ms.schema}].[{table.name}] "
                    f"{verb} CONSTRAINT ALL")
                conn.commit()
            except Exception as exc:
                conn.rollback()
                failed.append((table.name, str(exc)))
        if check:
            if failed:
                raise RuntimeError("Constraint validation failed: " + "; ".join(f"{name}: {error}" for name, error in failed))
            else:
                self.log("info", "Constraints re-enabled and re-validated.")
        else:
            if failed:
                raise RuntimeError("Could not disable constraints: " + "; ".join(f"{name}: {error}" for name, error in failed))
            self.log("info", "Foreign keys disabled for load.")

    def _reseed(self, conn, tables: Iterable[Table]):
        cur = conn.cursor()
        for table in tables:
            if table.target_exists and table.target_has_identity:
                cur.execute(
                    f"DBCC CHECKIDENT ('[{self.conns.ms.schema}].[{table.name}]', "
                    f"RESEED)")
        conn.commit()
        self.log("info", "Identity columns reseeded from current maximum.")


# SQL Server errors worth translating: the driver text names a constraint but
# not what to do, and these all have a preflight check that sees them first.
_KNOWN = {
    "2627": "a duplicate primary key or unique constraint",
    "2601": "a duplicate row in a unique index",
    "515": "a NULL in a column the target declares NOT NULL",
    "8152": "a value too long for its target column",
    "245": "a value that will not convert to the target type",
    "547": "a foreign key with no matching parent row",
}


def _explain(exc: Exception, table: str) -> Exception:
    """Say which check would have caught this, so the next run does not repeat it."""
    text = str(exc)
    for code, meaning in _KNOWN.items():
        if f"({code})" in text or f"[{code}]" in text:
            return RuntimeError(
                f"{table}: the target rejected a batch because of {meaning}. "
                f"Preflight reports this before any row moves — re-run the "
                f"checks on this pair and read the finding for {table}. "
                f"Driver said: {text}")
    return exc


def _show(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, decimal.Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _same(a, b) -> bool:
    """Equal across two engines: numbers by value, everything else by text.

    Decimal(5) and 5 are the same number; 'x ' and 'x' are not the same string,
    except that SQL Server's CHAR pads and Postgres does not, so trailing blanks
    are ignored.
    """
    if a is None or b is None:
        return a is None and b is None
    try:
        return decimal.Decimal(str(a)) == decimal.Decimal(str(b))
    except (decimal.InvalidOperation, ValueError):
        return str(a).rstrip() == str(b).rstrip()


def _same_text(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    sa, sb = str(a).rstrip(), str(b).rstrip()
    if sa == sb:
        return True
    if sa.lower() == sb.lower():
        return True                      # uuid/hex casing differs by engine
    try:
        return decimal.Decimal(sa) == decimal.Decimal(sb)
    except (decimal.InvalidOperation, ValueError):
        return False


def _sqlstate(exc: Exception) -> str:
    args = getattr(exc, "args", None)
    if args and isinstance(args[0], str):
        return args[0]
    return ""


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
class Verifier:
    def __init__(self, conns: Connections, tables: list[Table], options: Options):
        self.conns = conns
        self.tables = [t for t in tables if t.selected]
        self.options = options
        self._tgt_cols: dict[str, dict] = {}
        self.collation = ""

    # How hard to look. Each level includes the ones before it.
    LEVELS = {
        "counts":  "row counts only",
        "profile": "row counts, then every column's nulls, extremes, totals "
                   "and text lengths",
        "full":    "everything above, then a sample of rows read back and "
                   "compared field by field",
    }

    def run(self, deep: bool = True, level: str = "profile",
            sample_size: int = 200,
            progress: Callable[[str], None] | None = None) -> dict:
        if level not in self.LEVELS:
            level = "profile"
        if not deep:
            level = "counts"              # kept for older callers
        rows = []
        with closing(self.conns.source()) as src, closing(self.conns.target()) as tgt:
            for i, table in enumerate(self.tables):
                if progress:
                    progress(f"Verifying {table.name} ({i + 1}/{len(self.tables)})")
                if table.plan in ("skip", "schema"):
                    rows.append({"table": table.name, "status": "skip",
                                 "source": table.source_rows, "target": table.target_rows,
                                 "detail": f"plan is '{table.plan}': no rows were expected to move"})
                    continue
                try:
                    entry = self._counts(table, src, tgt)
                    if level != "counts" and entry["status"] == "ok":
                        entry.update(self._profile(table, src, tgt))
                        if entry.get("status") != "fail" and level == "full":
                            detail = entry.get("detail", "")
                            entry.update(self._sample(table, src, tgt, sample_size))
                            if detail:
                                entry["detail"] = detail + "; " + entry.get("detail", "")
                except Exception as exc:
                    entry = {"table": table.name, "source": table.source_rows,
                             "target": 0, "status": "fail", "detail": f"Verification query failed: {exc}"}
                rows.append(entry)
            untrusted = self._untrusted_keys(tgt)
            seeds = self._identity_seeds(tgt)

        failed = [r for r in rows if r["status"] == "fail"]
        checks = sum(len(r.get("profile", [])) for r in rows)
        return {
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "level": level,
            "level_description": self.LEVELS[level],
            "tables": rows,
            "tables_checked": sum(1 for r in rows if r["status"] != "skip"),
            "tables_failed": len(failed),
            "column_checks_failed": checks,
            "rows_sampled": sum(r.get("sampled", 0) for r in rows),
            "untrusted_foreign_keys": untrusted,
            "identity_problems": seeds,
            "passed": any(r["status"] == "ok" for r in rows) and not failed and not untrusted and not seeds,
            "incomplete": not any(r["status"] != "skip" for r in rows),
            "rows_verified": sum(r["target"] for r in rows if r["status"] == "ok"),
        }

    def _counts(self, table: Table, src, tgt) -> dict:
        """Compare like with like: a filtered table is measured against the rows
        it was actually asked to move, and says how many it left behind."""
        where = f" WHERE {table.row_filter}" if table.row_filter else ""
        total = fetch_one(
            src, f'SELECT COUNT(*) FROM "{self.conns.pg.schema}"."{table.name}"')[0]
        expected = total if not where else fetch_one(
            src, f'SELECT COUNT(*) FROM "{self.conns.pg.schema}"."{table.name}"'
                 + where)[0]
        target = fetch_one(
            tgt, f"SELECT COUNT(*) FROM [{self.conns.ms.schema}].[{table.name}]")[0]
        ok = expected == target
        detail = "" if ok else f"{abs(expected - target):,} row difference"
        if where:
            note = (f"{total - expected:,} row(s) excluded by "
                    f"{table.row_filter}")
            detail = f"{detail}; {note}" if detail else note
        if table.exclude_columns:
            note = "columns not sent: " + ", ".join(table.exclude_columns)
            detail = f"{detail}; {note}" if detail else note
        return {"table": table.name, "source": expected, "target": target,
                "status": "ok" if ok else "fail", "detail": detail,
                "source_total": total}

    def _aggregates(self, table: Table, src, tgt) -> dict:
        """Sum every numeric column on both sides. Catches silent type coercion
        that a row count cannot see."""
        sending = set(table.migrating_columns)
        numeric = [c.name for c in table.columns
                   if c.name in sending
                   and c.pg_type in ("bigint", "integer", "smallint", "numeric",
                                     "decimal", "double precision", "real")]
        if not numeric:
            return {}
        s_vals = fetch_one(
            src, "SELECT " + ", ".join(f'SUM("{c}")' for c in numeric)
            + f' FROM "{self.conns.pg.schema}"."{table.name}"'
            + (f" WHERE {table.row_filter}" if table.row_filter else ""))
        t_vals = fetch_one(
            tgt, "SELECT " + ", ".join(f"SUM(CAST([{c}] AS decimal(38,6)))"
                                       for c in numeric)
            + f" FROM [{self.conns.ms.schema}].[{table.name}]")
        drift = []
        for col, s, t in zip(numeric, s_vals, t_vals):
            if s is None and t is None:
                continue
            if s is None or t is None or abs(decimal.Decimal(str(s)) -
                                             decimal.Decimal(str(t))) > decimal.Decimal("0.000001"):
                drift.append(col)
        if drift:
            return {"status": "fail",
                    "detail": "column sums differ: " + ", ".join(drift)}
        return {"detail": f"{len(numeric)} column sums matched"}

    # -- column profiling --------------------------------------------------
    # Comparing per-column aggregates rather than row hashes: the two engines
    # format values differently enough that a cross-engine hash reports
    # differences that are not there, while these catch what actually goes
    # wrong in a migration — truncation, coercion, timezone shifts, lost NULLs.
    TEXTY = ("text", "character varying", "varchar", "character", "bpchar",
             "uuid", "citext", "json", "jsonb")
    NUMERIC = ("bigint", "integer", "smallint", "numeric", "decimal",
               "double precision", "real")
    TEMPORAL = ("date", "timestamp without time zone",
                "timestamp with time zone")

    def _profile_specs(self, table: Table) -> list[tuple]:
        """(label, postgres expression, sql server expression) per column."""
        sending = set(table.migrating_columns)
        specs = []
        for c in table.columns:
            if c.name not in sending:
                continue
            pg, ms = f'"{c.name}"', f"[{c.name}]"
            specs.append((f"{c.name}: non-null count",
                          f"COUNT({pg})", f"COUNT({ms})"))
            if c.pg_type in self.TEXTY:
                # Length totals are the truncation and encoding alarm: a uuid
                # written through the wrong codec loses characters silently.
                source_text = f"{pg}::text"
                target_text = f"CAST({ms} AS nvarchar(max))"
                if c.pg_type == "uuid":
                    source_text = f"REPLACE({source_text}, '-', '')"
                    target_text = f"REPLACE({target_text}, N'-', N'')"
                target_len = f"(LEN({target_text} + N'#') - 1)"
                specs.append((f"{c.name}: longest value",
                              f"COALESCE(MAX(LENGTH({source_text})), 0)",
                              f"COALESCE(MAX({target_len}), 0)"))
                specs.append((f"{c.name}: total characters",
                              f"COALESCE(SUM(LENGTH({source_text})), 0)",
                              f"COALESCE(SUM(CAST({target_len} AS bigint)), 0)"))
            elif c.pg_type in ("boolean", "bool"):
                specs.append((f"{c.name}: true count",
                              f"COUNT(*) FILTER (WHERE {pg})",
                              f"COALESCE(SUM(CASE WHEN {ms} = 1 THEN 1 ELSE 0 END), 0)"))
            elif c.pg_type in self.NUMERIC:
                specs.append((f"{c.name}: sum",
                              f"COALESCE(SUM({pg}), 0)",
                              f"COALESCE(SUM(CAST({ms} AS decimal(38,6))), 0)"))
                specs.append((f"{c.name}: minimum", f"MIN({pg})", f"MIN({ms})"))
                specs.append((f"{c.name}: maximum", f"MAX({pg})", f"MAX({ms})"))
            elif c.pg_type in self.TEMPORAL:
                if c.pg_type == "timestamp with time zone":
                    pg = f"({pg} AT TIME ZONE 'UTC')"
                # Compared as text to the second: the two engines disagree on
                # sub-second precision far more often than on the actual value.
                specs.append((f"{c.name}: earliest",
                              f"TO_CHAR(MIN({pg}), 'YYYY-MM-DD HH24:MI:SS')",
                              f"CONVERT(varchar(19), MIN({ms}), 120)"))
                specs.append((f"{c.name}: latest",
                              f"TO_CHAR(MAX({pg}), 'YYYY-MM-DD HH24:MI:SS')",
                              f"CONVERT(varchar(19), MAX({ms}), 120)"))
            if c.is_pk:
                specs.append((f"{c.name}: distinct keys",
                              f"COUNT(DISTINCT {pg})", f"COUNT(DISTINCT {ms})"))
        return specs

    def _profile(self, table: Table, src, tgt) -> dict:
        """Every column compared on both sides, in two queries."""
        specs = self._profile_specs(table)
        if not specs:
            return {}
        where = f" WHERE {table.row_filter}" if table.row_filter else ""
        s_vals = fetch_one(
            src, "SELECT " + ", ".join(s[1] for s in specs)
            + f' FROM "{self.conns.pg.schema}"."{table.name}"' + where)
        t_vals = fetch_one(
            tgt, "SELECT " + ", ".join(s[2] for s in specs)
            + f" FROM [{self.conns.ms.schema}].[{table.name}]")
        drift = []
        for (label, _p, _m), s_val, t_val in zip(specs, s_vals, t_vals):
            if _same(s_val, t_val):
                continue
            drift.append({"check": label, "source": _show(s_val),
                          "target": _show(t_val)})
        if drift:
            return {"status": "fail", "profile": drift,
                    "detail": f"{len(drift)} column check(s) differ: "
                              + ", ".join(d["check"] for d in drift[:3])
                              + (" …" if len(drift) > 3 else "")}
        return {"profile": [], "detail": f"{len(specs)} column checks matched"}

    def _sample(self, table: Table, src, tgt, size: int = 200) -> dict:
        """Read the same rows from both sides by key and compare them field by
        field. Aggregates can agree while individual rows are wrong; this is
        the check that reads the data itself."""
        pk = [c.name for c in table.columns
              if c.is_pk and c.name in set(table.migrating_columns)]
        if not pk:
            return {"status": "fail", "sampled": 0,
                    "detail": "Row sampling requires a migrated primary key; choose profile verification or supply a stable key."}
        cols = table.migrating_columns
        where = f" WHERE {table.row_filter}" if table.row_filter else ""
        s_rows = fetch(src, "SELECT " + ", ".join(f'"{c}"' for c in cols)
            + f' FROM "{self.conns.pg.schema}"."{table.name}"{where} '
            + "ORDER BY " + ", ".join(f'"{c}"' for c in pk) + f" LIMIT {int(size)}")
        mismatches = []
        for s_row in s_rows:
            key = tuple(adapt(s_row[cols.index(c)]) for c in pk)
            target_rows = fetch(tgt, "SELECT " + ", ".join(f"[{c}]" for c in cols)
                + f" FROM [{self.conns.ms.schema}].[{table.name}] WHERE "
                + " AND ".join(f"[{c}] = ?" for c in pk), key)
            if len(target_rows) != 1:
                mismatches.append({"key": str(key), "column": "matching key",
                                   "source": "1 row", "target": f"{len(target_rows)} rows"})
                continue
            for name, original, target in zip(cols, s_row, target_rows[0]):
                left, right = adapt(original), adapt(target)
                # Preserve case, whitespace, binary content, and full text length.
                if isinstance(original, uuid.UUID):
                    try:
                        equal = original == uuid.UUID(str(target))
                    except (ValueError, TypeError, AttributeError):
                        equal = False
                else:
                    equal = left == right
                if not equal:
                    mismatches.append({"key": str(key), "column": name,
                                       "source": _show(left), "target": _show(right)})
                    break
        if mismatches:
            return {"status": "fail", "sampled": len(s_rows),
                    "sample_mismatches": mismatches[:5],
                    "detail": f"{len(mismatches)} sampled rows differ"}
        return {"sampled": len(s_rows), "detail": f"{len(s_rows)} sampled rows matched by primary key"}

    def _untrusted_keys(self, tgt) -> list[str]:
        return [r[0] for r in fetch(tgt, """
            SELECT OBJECT_NAME(parent_object_id) + '.' + name
            FROM sys.foreign_keys
            WHERE (is_not_trusted = 1 OR is_disabled = 1)
              AND OBJECT_SCHEMA_NAME(parent_object_id) = ?
        """, (self.conns.ms.schema,))]

    def _identity_seeds(self, tgt) -> list[str]:
        seeds = fetch(tgt, """
            SELECT t.name, IDENT_CURRENT(QUOTENAME(s.name) + '.' + QUOTENAME(t.name))
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE OBJECTPROPERTY(t.object_id, 'TableHasIdentity') = 1 AND s.name = ?
        """, (self.conns.ms.schema,))
        names = {t.name for t in self.tables}
        problems = []
        for name, current in seeds:
            if name not in names:
                continue
            top = fetch_one(tgt, f"""
                SELECT MAX(CAST([{self._identity_col(tgt, name)}] AS bigint))
                FROM [{self.conns.ms.schema}].[{name}]""")[0]
            if top is not None and current is not None and int(current) < int(top):
                problems.append(f"{name}: identity seed {int(current)} is below "
                                f"max key {int(top)}")
        return problems

    def _identity_col(self, tgt, table: str) -> str:
        row = fetch_one(tgt, """
            SELECT c.name FROM sys.columns c
            JOIN sys.tables t ON t.object_id = c.object_id
            WHERE t.name = ? AND c.is_identity = 1 AND SCHEMA_NAME(t.schema_id) = ?
        """, (table, self.conns.ms.schema))
        return row[0] if row else "id"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
# Every migration and every verification is a job with an id, so a run can be
# found again months later: what was moved, what was deliberately left behind,
# what the checks said, and the whole activity log as it happened.
JOB_LOG_LIMIT = 5000            # ponytail: newest lines win; raise if audits need more


@dataclass
class Job:
    id: str
    activity: str                       # "migrate" | "verify"
    started: str
    finished: str = ""
    status: str = "running"             # running | passed | failed | stopped | abandoned
    source: dict = field(default_factory=dict)
    target: dict = field(default_factory=dict)
    mode: str = ""
    tables: list = field(default_factory=list)
    preflight: list = field(default_factory=list)
    transfer: dict = field(default_factory=dict)
    verification: dict | None = None
    log: list = field(default_factory=list)

    @property
    def label(self) -> str:
        pair = f"{self.source.get('database', '?')} → {self.target.get('database', '?')}"
        return f"{self.started[:19]}  {self.activity}  {pair}"

    def note(self, level: str, message: str, stamp: str = ""):
        self.log.append({"at": stamp or datetime.datetime.now().isoformat(
            timespec="seconds"), "level": level, "message": message})
        if len(self.log) > JOB_LOG_LIMIT:
            del self.log[:len(self.log) - JOB_LOG_LIMIT]


def new_job_id() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


class JobStore:
    """Jobs on disk, newest first. One file, written atomically."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.path = os.path.join(output_dir, "jobs.json")

    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, job: Job):
        os.makedirs(self.output_dir, exist_ok=True)
        data = self._read()
        data[job.id] = asdict(job)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        return sorted(self._read().values(),
                      key=lambda j: j.get("started", ""), reverse=True)

    def get(self, job_id: str) -> dict | None:
        return self._read().get(job_id)

    def delete(self, job_id: str):
        data = self._read()
        if data.pop(job_id, None) is None:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, self.path)


def job_text_report(job: dict) -> str:
    """A job as a plain-text record: readable without this app, and complete
    enough to answer "what happened and why do the two databases differ"."""
    out = []
    line = "=" * 78

    def head(title):
        out.extend(["", line, title.upper(), line])

    out.append(line)
    out.append(f"pgbridge {job.get('activity', '?')} job {job.get('id', '?')}")
    out.append(line)
    out.append(f"Started   : {job.get('started', '?')}")
    out.append(f"Finished  : {job.get('finished') or 'did not finish'}")
    out.append(f"Status    : {job.get('status', '?')}")
    src, tgt = job.get("source", {}), job.get("target", {})
    out.append(f"Source    : {src.get('database', '?')} on {src.get('host', '?')} "
               f"(schema {src.get('schema', '?')})")
    out.append(f"Target    : {tgt.get('database', '?')} on {tgt.get('server', '?')} "
               f"(schema {tgt.get('schema', '?')})")
    if job.get("mode"):
        out.append(f"Mode      : {job['mode']}")

    tables = job.get("tables") or []
    if tables:
        head("tables")
        out.append(f"{'table':<40}{'plan':<10}{'source rows':>14}  notes")
        for t in tables:
            notes = []
            if t.get("row_filter"):
                notes.append(f"filter {t['row_filter']}")
            if t.get("excluded_columns"):
                notes.append("without " + ", ".join(t["excluded_columns"]))
            if not t.get("selected", True):
                notes.append("not selected")
            out.append(f"{t.get('name', '?'):<40}{t.get('plan', 'auto'):<10}"
                       f"{t.get('source_rows', 0):>14,}  " + "; ".join(notes))

    partial = [t for t in tables
               if t.get("plan") not in ("auto", None) or t.get("row_filter")
               or t.get("excluded_columns") or not t.get("selected", True)]
    if partial:
        head("not migrated in full — read this first")
        for t in partial:
            out.append(f"  {t.get('name')}: plan '{t.get('plan', 'auto')}'")
            if t.get("row_filter"):
                out.append(f"      rows kept only where: {t['row_filter']}")
            if t.get("excluded_columns"):
                out.append("      columns not sent: "
                           + ", ".join(t["excluded_columns"]))
            for why in t.get("why", []):
                out.append(f"      why: {why}")

    issues = job.get("preflight") or []
    if issues:
        head("preflight findings")
        for issue in issues:
            cols = ", ".join(issue.get("columns") or []) or "—"
            out.append(f"[{issue.get('level', '?').upper():<4}] "
                       f"{issue.get('table', '?')}.{cols} — "
                       f"{issue.get('check', '?')}")
            out.append(f"       {issue.get('detail', '')}")
            for key, value in (issue.get("facts") or {}).items():
                out.append(f"         {key:<28} {value}")
            if issue.get("remedy"):
                for ln in str(issue["remedy"]).splitlines():
                    out.append(f"       | {ln}")
            out.append("")

    transfer = job.get("transfer") or {}
    if transfer:
        head("transfer")
        out.append(f"Rows moved   : {transfer.get('rows', 0):,}")
        out.append(f"Tables       : {transfer.get('tables_ok', 0)}"
                   f"/{transfer.get('tables_total', 0)}")
        out.append(f"Elapsed      : {transfer.get('seconds', 0)}s")
        out.append(f"Cancelled    : {transfer.get('cancelled', False)}")
        detail = transfer.get("detail") or {}
        if detail:
            out.append("")
            out.append(f"{'table':<40}{'status':<10}{'rows':>14}  notes")
            for name, res in sorted(detail.items()):
                notes = []
                if res.get("rows_excluded"):
                    notes.append(f"{res['rows_excluded']:,} rows excluded")
                if res.get("columns_excluded"):
                    notes.append("columns: " + ", ".join(res["columns_excluded"]))
                if res.get("error"):
                    notes.append(f"error: {res['error']}")
                out.append(f"{name:<40}{res.get('status', '?'):<10}"
                           f"{res.get('rows', 0):>14,}  " + "; ".join(notes))

    verify = job.get("verification")
    if verify:
        head("verification")
        out.append(f"Result           : "
                   f"{'PASSED' if verify.get('passed') else 'FAILED'}")
        out.append(f"Level            : {verify.get('level', '?')} — "
                   f"{verify.get('level_description', '')}")
        out.append(f"Tables checked   : {verify.get('tables_checked', 0)}")
        out.append(f"Tables failed    : {verify.get('tables_failed', 0)}")
        out.append(f"Rows verified    : {verify.get('rows_verified', 0):,}")
        out.append(f"Rows sampled     : {verify.get('rows_sampled', 0):,}")
        out.append("")
        out.append(f"{'table':<40}{'result':<9}{'source':>13}{'target':>13}")
        for row in verify.get("tables", []):
            out.append(f"{row.get('table', '?'):<40}{row.get('status', '?'):<9}"
                       f"{row.get('source', 0):>13,}{row.get('target', 0):>13,}")
            if row.get("detail"):
                out.append(f"    {row['detail']}")
            for drift in row.get("profile", []):
                out.append(f"      {drift['check']:<40} "
                           f"source={drift['source']}  target={drift['target']}")
            for miss in row.get("sample_mismatches", []):
                out.append(f"      row {miss['key']} column {miss['column']}: "
                           f"source={miss['source']}  target={miss['target']}")
        for name, items in (("untrusted foreign keys", "untrusted_foreign_keys"),
                            ("identity problems", "identity_problems")):
            if verify.get(items):
                out.append("")
                out.append(f"{name}:")
                out.extend(f"  {v}" for v in verify[items])

    log = job.get("log") or []
    if log:
        head(f"activity log ({len(log)} lines)")
        for entry in log:
            out.append(f"{entry.get('at', '')[:19]}  "
                       f"{entry.get('level', ''):<5}  {entry.get('message', '')}")

    out.append("")
    return "\n".join(out)


def write_report(path: str, pg: PgConfig, ms: MsConfig, options: Options,
                 issues: list[Issue], transfer: dict, verify: dict | None,
                 tables: list[Table] | None = None) -> str:
    """The record of one run: what was found, what was decided, what moved.

    The per-table plans matter as much as the counts. A row that did not arrive
    because someone deliberately filtered it is a different fact from one that
    went missing, and only this file distinguishes them afterwards.
    """
    tables = tables or []
    partial = [
        {"table": t.name, "plan": t.plan,
         "row_filter": t.row_filter, "excluded_columns": list(t.exclude_columns),
         "why": list(t.exclusion_notes),
         "source_rows": t.source_rows}
        for t in tables
        if t.plan != "auto" or t.row_filter or t.exclude_columns or not t.selected
    ]
    payload = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": {"host": pg.host, "database": pg.dbname, "schema": pg.schema},
        "target": {"server": ms.server, "database": ms.database, "schema": ms.schema},
        "options": asdict(options),
        "plan": {
            "mode": options.mode,
            "tables_selected": sum(1 for t in tables if t.selected),
            "tables_total": len(tables),
            # Everything not migrating in full, and why. Read this first when
            # the two databases do not match.
            "not_migrating_in_full": partial,
        },
        "preflight": [asdict(i) for i in issues],
        "transfer": transfer,
        "verification": verify,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return path


# ---------------------------------------------------------------------------
# Multi-Database Batch Migrator
# ---------------------------------------------------------------------------
@dataclass
class BatchDatabaseItem:
    source_db: str
    target_db: str
    status: str = "pending"  # pending | running | done | failed | stopped
    rows_moved: int = 0
    tables_done: int = 0
    tables_total: int = 0
    error: str = ""
    started: str = ""
    finished: str = ""


class BatchMigrator:
    """Coordinates batch migration across multiple on-premise PostgreSQL and
    SQL Server database pairs."""

    def __init__(self, conns: Connections, pairs: list[tuple[str, str]],
                 options: Options, continue_on_error: bool = True):
        self.conns = conns
        self.pairs = pairs
        self.options = options
        self.continue_on_error = continue_on_error
        self.items = [BatchDatabaseItem(src, tgt) for src, tgt in pairs]
        self.cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.current_transport: Transport | None = None
        self._lock = threading.Lock()

    def emit(self, kind: str, **payload):
        self.events.put({"kind": kind, **payload})

    def log(self, level: str, message: str):
        self.emit("log", level=level, message=message)

    def stop(self):
        self.cancel.set()
        if self.current_transport:
            self.current_transport.stop()
        self.log("warn", "Batch migration cancelled by user.")

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, name="pgbridge-batch", daemon=True)
        t.start()
        return t

    def _run(self):
        self.emit("batch_start", total=len(self.items))
        total_rows = 0

        for i, item in enumerate(self.items):
            if self.cancel.is_set():
                item.status = "stopped"
                continue

            item.status = "running"
            item.started = datetime.datetime.now().isoformat(timespec="seconds")
            self.emit("batch_db_start", index=i, source=item.source_db, target=item.target_db)
            self.log("info", f"[{i+1}/{len(self.items)}] Starting {item.source_db} -> {item.target_db}...")

            try:
                db_conns = Connections(
                    PgConfig(host=self.conns.pg.host, port=self.conns.pg.port,
                             user=self.conns.pg.user, password=self.conns.pg.password,
                             sslmode=self.conns.pg.sslmode, dbname=item.source_db,
                             schema=self.conns.pg.schema),
                    MsConfig(server=self.conns.ms.server, user=self.conns.ms.user,
                             password=self.conns.ms.password, auth=self.conns.ms.auth,
                             driver=self.conns.ms.driver, encrypt=self.conns.ms.encrypt,
                             trust_cert=self.conns.ms.trust_cert, database=item.target_db,
                             schema=self.conns.ms.schema)
                )

                if not self.options.dry_run:
                    db_conns.ensure_target_database(item.target_db)
                elif not db_conns.target_database_exists(item.target_db):
                    raise ValueError("Dry run requires an existing target database; no database was created.")
                intro = Introspector(db_conns)
                tables = intro.discover()
                for t in tables:
                    if t.name in PROTECTED_TABLES:
                        t.selected = False

                item.tables_total = len([t for t in tables if t.selected])

                db_opts = copy.deepcopy(self.options)
                db_opts.validate()
                findings = Preflight(db_conns, tables, db_opts).run()
                blocking = [issue for issue in findings if issue.level == "stop"]
                for issue in findings:
                    self.log("warn" if issue.level == "warn" else "info",
                             f"{item.source_db}: {issue.table}: {issue.detail}")
                if blocking:
                    raise ValueError("Preflight blocked: " + "; ".join(
                        f"{issue.table}: {issue.detail}" for issue in blocking))

                transport = Transport(db_conns, tables, db_opts)
                with self._lock:
                    self.current_transport = transport

                worker = transport.start()
                outcome = None
                while worker.is_alive() or not transport.events.empty():
                    try:
                        evt = transport.events.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if evt.get("kind") == "finished":
                        outcome = evt
                        item.rows_moved = evt.get("rows", 0)
                        item.tables_done = evt.get("tables_ok", 0)
                    elif evt.get("kind") == "log":
                        self.log(evt.get("level", "info"), f"[{item.source_db}] {evt.get('message')}")
                worker.join()
                self.current_transport = None
                total_rows += item.rows_moved
                if self.cancel.is_set():
                    item.status = "stopped"
                elif (not outcome or outcome.get("error") or outcome.get("cancelled")
                      or not item.tables_total or item.tables_done != item.tables_total):
                    raise RuntimeError((outcome or {}).get("error") or "Transfer incomplete; inspect table failures.")
                elif db_opts.dry_run:
                    item.status = "dry_run"
                else:
                    if db_opts.with_data:
                        report = Verifier(db_conns, tables, db_opts).run(deep=False)
                        if not report["passed"]:
                            raise RuntimeError("Post-transfer verification failed.")
                    item.status = "done"
                item.finished = datetime.datetime.now().isoformat(timespec="seconds")
                self.emit("batch_db_done", index=i, item=asdict(item))
                self.log("ok" if item.status == "done" else "warn",
                         f"[{item.source_db} -> {item.target_db}] {item.status}: {item.rows_moved:,} rows processed.")

            except Exception as exc:
                self.current_transport = None
                item.status = "failed"
                item.error = str(exc)
                item.finished = datetime.datetime.now().isoformat(timespec="seconds")
                self.log("error", f"[{item.source_db} -> {item.target_db}] Failed: {exc}")
                self.emit("batch_db_done", index=i, item=asdict(item))
                if not self.continue_on_error:
                    self.log("warn", "Stopping batch migration due to failure policy.")
                    self.cancel.set()

        self.emit("batch_finished", total_rows=total_rows,
                  items=[asdict(item) for item in self.items])

