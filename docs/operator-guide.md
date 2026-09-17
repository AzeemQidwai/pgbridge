> Historical workflow reference. See [the current README](../README.md) for updated recovery, security defaults, framework support, and release limitations.

# pgbridge

A desktop console for moving data from PostgreSQL to Microsoft SQL Server, built
for Django-backed applications where the schema already exists on the target.

## Install

Runs on Ubuntu and Windows from the same source tree — no per-OS branch, no
build step. Two things it needs are OS packages rather than Python ones:
Tkinter, and the SQL Server ODBC driver.

**Ubuntu**

```bash
sudo apt install python3-tk unixodbc
# msodbcsql18 comes from Microsoft's apt repo, not Ubuntu's:
curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
  | sudo tee /etc/apt/trusted.gpg.d/microsoft.asc > /dev/null
echo "deb [arch=amd64] https://packages.microsoft.com/ubuntu/$(lsb_release -rs)/prod \
  $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt update && sudo ACCEPT_EULA=Y apt install msodbcsql18

pip install -r requirements.txt
python3 run.py
```

**Windows**

```powershell
# Tkinter ships with the python.org installer. Install the ODBC driver first:
#   "ODBC Driver 18 for SQL Server" (MSI, from Microsoft)
pip install -r requirements.txt
python run.py
```

`run.py` checks all three on startup and prints the install line for whichever
is missing, so a wrong box says so before you type a password rather than at
connect time.

Two behaviours differ by platform, by necessity:

- **Windows authentication** (`Authentication: windows` on the target panel)
  passes `Trusted_Connection=yes`. On Windows it just works; on Ubuntu it needs
  a Kerberos ticket for the SQL Server realm. Use SQL authentication there
  unless the domain is already set up.
- **Saved profile permissions.** `~/.pgbridge/profiles.json` is locked to your
  account with `chmod 0600` on Ubuntu and with `icacls /inheritance:r` on
  Windows, where mode bits mean nothing.

## Three activities

The app opens on a choice, because these are different jobs:

| Activity | Stages | Writes? |
|---|---|---|
| **Migrate** | Connect → Tables → Preflight → Transfer → Verify → Cutover | Yes |
| **Verify** | Connect → Verify | No |
| **Cutover** | Connect → Cutover | The project, not the databases |

*Verify* exists on its own so a pair migrated last week — or by someone else —
can be checked without walking through the migration screens. *Switch activity*
in the status strip goes back to the choice; it refuses while a transfer is
running.

Choosing an activity starts a fresh run: the tables, findings, transfer results
and verdict from the previous one are cleared from both the state and the
screens, and a new job is opened. The server connections are kept, since
switching activity is not disconnecting.

A migration is not finished until it is checked, so a completed transfer moves
to the Verify stage and runs verification itself. A transfer you stopped does
not: it says so and leaves the verdict to you.

**6 · Cutover** — The step nobody writes down: the application is still talking
to PostgreSQL. This rewrites a Django project's `DATABASES` to point at the
database that was just migrated.

First choose **which database the application should use**. Arriving from a
migration it is already filled in — the one that was just migrated — and says
so; arriving at *Cutover* on its own, nothing knows it, so it is picked from the
server (*List databases*) or typed. Nothing can be patched until it is set: an
empty `NAME` in a settings file is worse than no patch at all.

Then choose the project's `settings.py` and where the credentials should live:

| Option | What happens |
|---|---|
| **In a .env file, read with python-decouple** (default) | `DATABASES` uses `config("DB_NAME")` and friends; the values go in `.env`; `from decouple import config` is added to the imports if missing |
| **Written directly into settings.py** | the values are written inline, via `repr()`, so passwords containing quotes and backslashes survive |

Point it at the project's virtualenv and it reports the Python version and
whether **python-decouple** and **mssql-django** are importable *there* — not in
whatever interpreter is running pgbridge — and offers to `pip install` the
missing ones into it. `mssql-django` is checked either way: a patched
settings.py that cannot load is worse than none.

Everything is previewed before anything is written: which lines will be
commented out, the block that replaces them, and the `.env` keys — with the
password masked. Then:

- `settings.py` is copied to `settings.py.pgbridge-<timestamp>.bak` first.
- The old `DATABASES` is **commented out, not deleted**, so rolling back is a
  matter of reading the file and the credentials that used to work stay visible
  when the new ones do not.
- The result is parsed before it is saved; if it would not import, the original
  is restored and nothing changes.
- An existing `.env` keeps every key the project already had — matching `DB_*`
  keys are updated in place, the rest are appended — and the file is locked to
  the owner, since it now holds a database password.

The block is located with `ast`, not a regex: `DATABASES` is routinely spread
over twenty lines with nested dicts and comments, and a pattern that gets it
wrong corrupts somebody's settings file. A file with no top-level `DATABASES`
is refused untouched, with a note that split settings packages usually define
it in `settings/base.py`.

## Jobs

Every migration and every verification is a **job** with an id, written to
`pgbridge_output/jobs.json` as it happens — not at the end, so a job killed
mid-transfer is still there with its log up to the moment it stopped. A job
records the database pair, the mode, every table with its plan and exclusions,
every preflight finding with its facts, the transfer results per table, the
verification report, and the whole activity log.

**Job history** (from the opening screen, or *Jobs* in the status strip) lists
them newest first with their result and row counts. Selecting one renders its
full text report in place; *Save text report* writes that to a `.txt` file. The
report is designed to be read without this app — it opens with what ran and
against which databases, then a **NOT MIGRATED IN FULL** section listing every
table that did not migrate completely and why, then preflight findings with
their facts, per-table transfer results with exclusion counts, the verification
breakdown including every failing column check, and the activity log.

Jobs end as `passed`, `failed`, `stopped` (a cancelled transfer, or the window
closed mid-run) or `abandoned` (the activity was switched away from).

## The activity log

Every line in the log drawer is also appended, as it happens, to
`pgbridge_output/<activity>_<date>.log`. That file is what survives the process
being killed, so the record of a transfer does not depend on the window still
being open. It carries the stage transitions, the database pair, each table's
row count and rate as it finishes, every verification mismatch by name, and the
resume decisions below.

## Crash recovery

Progress is checkpointed to `pgbridge_output/checkpoint.json` after every table
and after every batch within a table, written to a temp file and `fsync`ed
before being renamed over the old one — so a kill at any instant leaves a
readable checkpoint, never a half-written one.

The checkpoint records the database pair it belongs to, and a table is marked
`running` before its first row moves. On the next run:

- Tables marked `done` are skipped.
- Tables marked `running`, `failed` or `stopped` were interrupted part-way, so
  they are **cleared and reloaded** whether or not *Clear target tables* is
  ticked — reloading onto a partial load would otherwise double the rows.
- A checkpoint from a different pair is ignored rather than applied to the
  wrong database.

You are told about it twice: once at launch, and again on the Transfer stage,
which ticks *Resume from last checkpoint* for you and spells out what will be
skipped and what will be redone.

Recovery note: recovery is per table, not per batch. A table interrupted at 9 of 10
million rows redoes all 10 million. Row-level resume needs a stable sort key and
`OFFSET` reads on the source; worth adding only if single tables get big enough
that reloading one is the expensive part.

## The five stages

**1 · Connect** — Both activities start here. Servers and credentials only. *Test both connections* logs
into each server (`postgres` and `master`) and reports the versions and how many
databases the login can see. No database is chosen here: a database is picked at
the stage that uses it, so one set of credentials serves every migration on that
pair of servers. Connection profiles save to `~/.pgbridge/profiles.json`, locked
to your own account on both platforms; passwords are excluded unless you tick the
box.

**2 · Tables** — Sortable on every column, filterable by state (*All / Selected
/ Missing on target / Target not empty / Empty and ready*) and by name, with a
live tally underneath: tables selected, rows to move, tables to create, targets
that are not empty, and the largest table in the run — the one that decides how
long this takes. Clicking the tick header selects or clears everything currently
listed, so a filter plus one click is a bulk action. The **Will do** column
states each table's outcome in words (`all data`, `create + all data`,
`clean data (−rows, −2 cols)`, `schema only`, `skipped`), so a per-table plan
chosen at preflight is visible back here.

Pick the source database here — *List databases* fills the
dropdowns from the live servers — then *Load schema*.

**The target database is optional.** Leave it blank and the target takes the
source's name; if no such database exists on the server, you are asked once and
it is created. Type a name to migrate into a database that does not exist yet, or
pick an existing one from the dropdown to migrate into that instead.

*Migrate* chooses what actually moves:

| Mode | What runs |
|---|---|
| `schema + data` | Missing target tables are created, then rows are copied |
| `schema only` | Tables are created, no rows are read |
| `data only` | Rows are copied into tables that must already exist |

The mode is set here rather than at the transfer, because everything downstream
depends on it: a missing target table blocks a `data only` run at preflight, and
is just a note when the same run would create it.

Exact row counts on both sides, plus whether each target table exists, is empty,
or will be created. Changing either database discards the schema and any report
already loaded, rather than showing you one pair's numbers under another pair's
name. `django_migrations` is deselected
automatically — it belongs to the target and must not receive source rows. Click
the marker in the first column, or press space, to toggle.

**3 · Preflight** — Every check that can be answered before a row moves:

| Check | Why it matters |
|---|---|
| Missing target table | Blocks a `data only` run; a note when the run creates it |
| Column mismatch | Non-nullable target column the source can't fill |
| Unsupported type | Arrays, hstore, tsvector, ranges have no SQL Server equivalent |
| Target holds rows | Primary keys would collide |
| Nullable unique column | Postgres allows many NULLs, SQL Server allows one |
| Index key size | SQL Server caps unique index keys at 900 bytes |
| Duplicate target key | Rows distinct in Postgres that collide on the target's primary key |
| Key column not sent | A plan excludes a column the target keys on |
| Snapshot isolation | Lock-based reads will block writers where MVCC didn't |
| Collation | Case-insensitive target changes uniqueness and lookups |

Warnings require ticking the acknowledgement box. A blocking finding stops the
run — but one broken table should not hold back forty good ones, and tables
break for different reasons, so the decision is **per table**. Select a blocked
finding and its table gets four options:

| Plan | What that table does |
|---|---|
| **Fix it first** (default) | nothing; the run stays blocked on it |
| **Move what passes** | migrates, minus the rows or columns that would fail |
| **Schema only** | the table is created, no rows are read |
| **Skip table** | left alone entirely |

Only the plans that would genuinely clear that table's findings are offered.
*Move what passes* is derived from the findings themselves:

| Finding | What "move what passes" does |
|---|---|
| Nullable unique column | adds `WHERE "col" IS NOT NULL`, leaving the NULL rows behind |
| Source column missing on target | sends the columns both sides share, drops the rest |
| Unsupported type | drops those columns, moves everything else |
| Target requires a column the source lacks | *not offered* — no filter helps; skip is the only route |
| Missing target table | *not offered* — schema only, which creates it |

Tables that passed are never touched by any of this. The run unblocks once
every blocked table has a plan, and continuing confirms — by name, with the
exact filter — everything being left behind. Re-running the checks clears every
plan, because re-running re-asks the question.

A finding with no table (the "—" rows, such as having no tables selected) cannot
be planned around at all, and keeps the run blocked.

The findings list names the **table and the column** on every row, so you can
see which column is at fault without opening anything. Running the checks lands
on the most serious finding with its detail already open, and each blocking or
warning finding is written to the activity log with its column name.

The pane below shows everything behind that one line:
the column, how each side declares it, the type mapping between them, and the
counts that were actually measured. A nullable unique column reads

```
STOP · shop_customer · Nullable unique column
email holds 52 NULLs; SQL Server allows one NULL in a unique index, so 51 rows
would be rejected.

What was found
Column                 shop_customer.email
Source column          character varying(254), NULL
Target column          nvarchar(254), NULL
Type mapping           character varying → nvarchar({n})
Source unique index    shop_customer_email_key (unique, not primary)
NULLs in source        52 of 124,003 rows (0.0%)
SQL Server limit       one NULL per unique index
Rows that would fail   51 — the first NULL inserts, every later one violates it
Target index           UQ_shop_customer_email (unique)

How to fix
Either make the target index filtered:
  CREATE UNIQUE INDEX [UX_shop_customer_email]
    ON [dbo].[shop_customer] ([email])
    WHERE [email] IS NOT NULL;
(drop the existing unique index or constraint on that column first), or clean
the source so at most one NULL remains:
  SELECT * FROM "public"."shop_customer" WHERE "email" IS NULL;
```

Every check carries its own facts in the same shape: a column mismatch lists
each missing column with its source definition and the target type it needs, an
oversized index key breaks the 900 bytes down per column, a non-empty target
shows both row counts and the primary key they would collide on, and a
collation or snapshot finding names the database and what changes after
cutover.

**4 · Transfer** — Missing tables are created first when the mode includes
schema. Foreign keys then come down for the load and go back up with
`WITH CHECK CHECK CONSTRAINT`, so they are re-validated rather than left
untrusted. `IDENTITY_INSERT` preserves primary keys; identity columns are
reseeded afterwards. Progress is checkpointed to `pgbridge_output/checkpoint.json`
after every table, so *Resume from last checkpoint* skips completed work.

Failed batches retry with exponential backoff on deadlock and timeout codes.
Set *If a batch fails* to `quarantine` and the batch is retried row by row, with
rejects written to `pgbridge_output/quarantine_<table>.csv` alongside the error —
one bad row then costs one row instead of the whole table.

**5 · Verify** — Runs automatically at the end of a migration, and is the whole
of the *Verify* activity. Three levels, each doing strictly more than the last:

| Level | What it compares |
|---|---|
| **Row counts only** (default) | `COUNT(*)` on both sides. The one that finishes on a large database while somebody is watching. |
| **Row counts and column profile** | per column: non-null count; longest value and total characters for text; sum, minimum and maximum for numerics; earliest and latest for dates; true count for booleans; distinct count for keys |
| **Full** | the above, then up to 200 rows read back from both sides by key and compared field by field |

The column profile is what catches the failures a row count cannot see. Total
characters and longest value catch truncation and encoding damage — a `char(32)`
UUID arriving at 16 characters shows up here even though the row count is
perfect. Distinct-key counts catch collisions. Sums and extremes catch silent
type coercion, and date extremes catch timezone shifts.

Comparison is by aggregate rather than by cross-engine row hash, deliberately:
the two engines format values differently enough that a hash reports differences
that are not really there. Values are compared numerically where both sides
parse as numbers, and otherwise as text with trailing blanks ignored, since
SQL Server's `CHAR` pads and Postgres does not.

Each stage only runs where the one before it agreed — profiling a table whose
counts already differ just repeats the same news — and a table planned as
`schema` or `skip` verifies as **skipped**, never failed. Every failing check
appears under its table in the results and by name in the activity log. Carries its own database pickers, so a pair nobody
migrated in this session can be checked: point it at any two databases on the connected
servers and it reads their schemas before comparing. Row counts on both sides,
optional numeric column sums (which catch silent type coercion a count cannot
see), a sweep for foreign keys left disabled or untrusted, and a check that no
identity seed sits below its table's maximum key. Export writes a JSON report
plus the activity log.

## What schema migration does and does not create

`schema only` and `schema + data` generate `CREATE TABLE` from the source shape:
columns with the type mapping below, the primary key, and `IDENTITY(1,1)` for a
key backed by a Postgres sequence — so the application still gets keys after
cutover, and `IDENTITY_INSERT` still preserves the ones you migrate.

It does **not** create foreign keys, secondary indexes, defaults, check
constraints, or triggers. For a Django target, `manage.py migrate` remains the
schema source of truth and produces all of those; this path exists for the
"empty target, get me moving" case — a rehearsal, a scratch database, a
non-Django source. Preflight says so on every table it is about to create.

## Type conversions handled

| PostgreSQL | SQL Server | Note |
|---|---|---|
| `uuid` | `char(32)` | Dashes stripped — `mssql-django` stores UUIDField this way |
| `jsonb` / `json` | `nvarchar(max)` | Serialized on write |
| `bytea` | `varbinary(max)` | `memoryview` → `bytes` |
| `interval` | `bigint` | Microseconds, matching Django's DurationField |
| `timestamptz` | `datetime2` | Converted to naive UTC |
| `boolean` | `bit` | |

## Do you need anything besides Python?

**Required, and not a Python package:** the ODBC driver above. Get it approved
early — in a regulated organization it is usually the slowest item in the whole migration.

**Worth having:**

- **`bcp` and `sqlcmd`** (Microsoft command-line utilities). For a wide, flat
  table of tens of millions of rows, `COPY ... TO STDOUT` out of Postgres and
  `bcp ... in -E` into SQL Server is roughly an order of magnitude faster than
  any Python path. You lose the type adaptation above, so it only suits tables
  with no UUID, JSON, or interval columns.
- **PyInstaller**, if you want to hand colleagues a single `.exe` rather than
  asking them to set up Python.
- **SSMA for PostgreSQL** (free, Microsoft). Not for the migration itself —
  keep Django migrations as the schema source of truth — but useful as a second
  opinion when validating a converted schema.

**Not needed, despite appearances:**

- No Postgres client libraries. `psycopg2-binary` bundles `libpq`.
- No `customtkinter` or Qt. Every control here that carries the interface is
  drawn on a canvas, so the look does not depend on an extra UI toolkit. If you
  later want native window chrome and better HiDPI scaling on Windows, PySide6
  is the upgrade path — but the engine module has no UI imports, so it ports
  without change.
- No Django import. The cutover patches settings.py as text guided by `ast`;
  it never imports the project, which would need its dependencies installed and
  its environment configured.
- No ETL platform. If this becomes a recurring sync rather than a one-time
  cutover, that calculus changes and Azure Data Factory or SSIS earns its keep.

## Tests

```bash
python test_stage_flow.py       # about a minute
```

It maps exactly one window and reuses it: mapping a Tk top-level costs a slow
round-trip to the X server, and paying that per test group took the suite from
one minute to eleven.

Drives the real window and the engine's recovery logic: that each activity
shows only its own stages, that Connect commits without a database, that a blank
target falls back to the source's name, that switching pairs discards the
previous pair's schema and reports, that the migrate mode turns a missing target
table from a blocker into a note, that the generated DDL carries the primary key
and identity, that every log line reaches its file, that a clean transfer
verifies itself while a stopped one does not, that a checkpoint left by a kill
resumes the interrupted table and skips the finished ones, that a preflight
finding carries its column, both column definitions, the index and the counts
through to the rendered detail pane, that skipping blocked tables deselects
exactly those and leaves the mode alone while the schema-only route does the
reverse, that a per-table plan reaches the actual SQL — the filter in the
`WHERE`, the excluded column absent from the `SELECT` — that verification
measures a filtered table against what it was asked to move rather than calling
it a failure, that the exported report records every exclusion, that a duplicate
target key is a preflight finding rather than a transfer crash and that a plan
cannot drop a key column, that the UTF-16LE fix stays in place, that the column profile
catches a truncated UUID column when the row counts agree, that each
verification level does strictly more than the one before, that a job round-trips
through `jobs.json` and renders a text report carrying its exclusions and failing
checks, that switching activity clears every stage's screen rather than showing
the previous run's, that every status badge clears WCAG AA contrast against its
own background, that patching settings.py keeps the old block commented and
leaves importable Python whose `DATABASES` says what was meant, that the .env
route keeps the password out of settings.py and preserves the project's
existing keys, that a file without `DATABASES` is refused untouched, that cutover
carries the migrated database over but refuses to patch without one, that the
job history keeps both its list and its report usable at the smallest window the
app allows with the log drawer open, that the opening screen offers a scrollbar
when its cards overflow and hides it when they do not, that each activity card
names its own activity, and that no check leaves a cursor holding unread rows — the fake connection enforces SQL Server's
one-active-result-set rule. Skips itself
with no display.

It also walks the live widget tree on every screen, at full size and at the
1120x720 minimum with the log drawer open, and fails if any button is narrower
than its own label, any label is clipped, or a stage's action row is pushed off
the window.

### Duplicate keys are found before the load, not during it

`Violation of PRIMARY KEY constraint … Cannot insert duplicate key` is the worst
kind of failure: it fires part-way through a transfer, after other tables have
already loaded, and leaves the run half-done. Its cause is always visible up
front, so preflight looks for it:

- **A case- or accent-insensitive target collation.** Postgres compares text
  byte for byte; a `_CI_`/`_AI_` collation does not, so `'Ali'` and `'ali'` are
  two source rows and one target key.
- **A target primary key narrower than the source's**, where rows the source
  kept apart share a single target key.

The check groups the source by the *target's* key columns, compared the way the
target will compare them, and reports the exact count of rows that would be
rejected along with the worst offending values. Because no filter can make a key
unique, the only honest plans for such a table are *Schema only* or *Skip*.

If a rejection does reach the transfer, the driver's text is translated rather
than echoed: the message names what the target actually objected to and points
back at the preflight check that sees it first.

> **A note on `char(32)` UUIDs.** `Connections.target()` deliberately sets no
> `setencoding`/`setdecoding` overrides. SQL Server's wide types are UTF-16LE
> and pyodbc already defaults to that; forcing `utf-8` on `SQL_WCHAR` sends a
> 32-character UUID as 32 bytes that the server reads as 16 wide characters and
> truncates — `0FEB8D…` arrives as `䘰䉅䐸`, and UUIDs sharing a prefix collide on
> the primary key. The test suite asserts those overrides stay gone.

## What the run leaves behind, and where that is recorded

A per-table plan means some rows and columns deliberately do not arrive. That is
a different fact from data going missing, so it is recorded in three places:

- **The activity log**, as the transfer runs: how many rows a filter excluded,
  which columns were not sent, and which tables moved schema only.
- **Verification**, which measures a filtered table against the rows it was
  *asked* to move — otherwise every clean plan would report as a failure — and
  notes the exclusions in its detail column. A table planned as `schema` or
  `skip` verifies as **skipped**, never failed.
- **The exported JSON report**, under `plan.not_migrating_in_full`: every table
  that did not migrate in full, its plan, the exact row filter, the excluded
  columns, and why. Read that section first when the two databases do not match.

## A note on cursors

SQL Server without MARS allows one active result set per connection, and
Preflight and Verifier share a single connection across every check. A cursor
left holding unread rows therefore breaks the *next* check with `Connection is
busy with results for another command`. Every read in those two classes goes
through `fetch()` / `fetch_one()`, which take all rows and close the cursor, so
that cannot depend on each caller remembering to drain. `fetch_one` exists
because `cursor.fetchone()` leaves the result set open — the failure it causes
surfaces one check later, nowhere near the cause.

## Using the engine without the GUI

`pgbridge/engine.py` imports no Tk. It runs headless for CI or a scheduled
cutover rehearsal:

```python
from pgbridge.engine import (Connections, Introspector, Options,
                             PgConfig, MsConfig, Preflight, Transport, Verifier)

conns = Connections(PgConfig(host="pg01", dbname="shop", user="svc",
                             password="..."),
                    MsConfig(server="sql01", database="shop", user="svc",
                             password="..."))
tables = Introspector(conns).discover()
options = Options(chunk_size=50_000, on_row_error="quarantine")

issues = Preflight(conns, tables, options).run()
if any(i.level == "stop" for i in issues):
    raise SystemExit([i.detail for i in issues if i.level == "stop"])

transport = Transport(conns, tables, options)
transport.start().join()
print(Verifier(conns, tables, options).run(deep=True))
```
