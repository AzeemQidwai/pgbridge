"""Secret-free, reviewable application configuration handoffs.

These adapters generate documentation; they never execute application code or
claim a deployment has been validated. Django's patcher lives in cutover.py.
"""
from __future__ import annotations

FRAMEWORKS = {
    "SQLAlchemy / FastAPI / Flask": ("sqlalchemy pyodbc", "https://docs.sqlalchemy.org/en/20/dialects/mssql.html", '''from os import environ
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

# DB_HOST is a hostname; DB_PORT is separate (default 1433).
url = URL.create(
    "mssql+pyodbc",
    username=environ["DB_USER"], password=environ["DB_PASSWORD"],
    host=environ["DB_HOST"], port=int(environ.get("DB_PORT", "1433")),
    database=environ["DB_NAME"],
    query={"driver": "ODBC Driver 18 for SQL Server",
           "Encrypt": "yes", "TrustServerCertificate": "no"},
)
engine = create_engine(url, pool_pre_ping=True)
# Flask-SQLAlchemy: assign url to SQLALCHEMY_DATABASE_URI before init_app.
# FastAPI: scope sessions to requests and close them after use.
'''),
    "ASP.NET Core / EF Core": ("Microsoft.EntityFrameworkCore.SqlServer", "https://learn.microsoft.com/en-us/ef/core/providers/sql-server/", '''// Program.cs: replace AppDbContext with your application's DbContext.
// Inject ConnectionStrings__MigrationTarget through your secret store.
// Build that value with SqlConnectionStringBuilder, with Encrypt=true
// and TrustServerCertificate=false; never concatenate user input.
builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseSqlServer(
        builder.Configuration.GetConnectionString("MigrationTarget")
        ?? throw new InvalidOperationException("Missing MigrationTarget")));
// SQL Server migrations must be generated and reviewed for this provider.
'''),
    "Spring Boot / JDBC": ("com.microsoft.sqlserver:mssql-jdbc", "https://docs.spring.io/spring-boot/reference/data/sql.html", '''# application.properties
# Inject DB_JDBC_URL through deployment configuration, for example:
# jdbc:sqlserver://sql01:1433;databaseName=app;encrypt=true;trustServerCertificate=false
spring.datasource.url=${DB_JDBC_URL}
spring.datasource.username=${DB_USER}
spring.datasource.password=${DB_PASSWORD}
spring.datasource.driver-class-name=com.microsoft.sqlserver.jdbc.SQLServerDriver
# If using JPA, validate the reviewed target schema; do not auto-recreate it.
spring.jpa.hibernate.ddl-auto=validate
'''),
    "Node.js / Express": ("mssql", "https://github.com/tediousjs/node-mssql", '''const sql = require('mssql');
const required = (key) => {
  if (!process.env[key]) throw new Error(`Missing ${key}`);
  return process.env[key];
};
// DB_HOST is a hostname, not an ODBC server,port string.
const pool = new sql.ConnectionPool({
  server: required('DB_HOST'),
  port: Number(process.env.DB_PORT || 1433),
  database: required('DB_NAME'),
  user: required('DB_USER'), password: required('DB_PASSWORD'),
  options: { encrypt: true, trustServerCertificate: false },
  pool: { max: 10, min: 0, idleTimeoutMillis: 30000 }
});
// Await pool.connect() during startup; reuse it and close at shutdown.
module.exports = pool;
'''),
}


def render_handoff(framework: str, ms) -> str:
    if framework not in FRAMEWORKS:
        raise ValueError(f"Unknown framework: {framework}")
    dependency, reference, snippet = FRAMEWORKS[framework]
    # repr keeps endpoint metadata on one line; no credentials enter the export.
    return f'''# SQL Server application handoff

Framework: {framework}
Target server: {ms.server!r}
Target database: {ms.database!r}
Status: CONFIGURATION TEMPLATE — application validation pending

## Dependencies and authentication

Dependency: {dependency}
Select a release compatible with your application's runtime and lock it there.
These templates use SQL authentication and validated TLS certificates.
Supply credentials with the deployment secret store. For integrated authentication,
review the provider's platform-specific setup before adapting this template.
Endpoint metadata above is informational: set your deployment variables explicitly.
Do not copy an ODBC server string into a hostname field; configure the port separately.

## Configuration to integrate

```
{snippet.rstrip()}
```

## Release gates

1. Rehearse on a restored copy; review SQL Server schema, types, constraints,
   indexes, ORM migrations, raw SQL, and transaction behavior.
2. Back up application configuration and both databases; test the restore procedure.
3. Stop source writes and background workers; complete the final transfer.
   pgbridge does not capture changes made after a table was read.
4. Verify the final data and explicitly approve every planned exclusion.
5. Inject secrets and configuration; use the application's own runtime to confirm
   SELECT DB_NAME() matches the intended target, then test critical read/write paths.
6. Restart application pools/workers, switch traffic, and monitor errors and latency.
7. On failure before target writes, restore the old configuration. After target
   writes, reconcile those writes before rollback to avoid losing new data.

Exporting this document does not patch files, deploy an app, or complete cutover.
Provider reference: {reference}
'''
