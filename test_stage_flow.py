#!/usr/bin/env python3
"""Stage-flow check: databases are chosen where they are used, not at connect.

Builds the real window against a display and drives the pickers. Needs Tk and a
display; it is skipped on a headless box.

    python test_stage_flow.py
"""

import ast
import decimal
import time
import sys

try:
    import tkinter
    tkinter.Tk().destroy()
except Exception as exc:                                  # noqa: BLE001
    print(f"skipped: no usable display ({exc})")
    raise SystemExit(0)

from pgbridge import app as A

warnings = []
A.messagebox.showwarning = lambda *a, **k: warnings.append(a)
A.messagebox.showinfo = lambda *a, **k: warnings.append(a)

# Mapping a top-level costs one slow round-trip to the X server, so the suite
# maps exactly one and reuses it wherever a test needs real geometry.
MAPPED = A.App()
MAPPED.update()

def mapped(fresh_activity=None):
    """The shared visible window, reset to a clean run."""
    MAPPED.transport = None
    MAPPED.finish_job("abandoned")     # or it closes into the next group's store
    MAPPED.reset_run(keep_connection=False)
    if fresh_activity:
        MAPPED.set_activity(fresh_activity)
    return MAPPED

app = MAPPED
connect, tables, verify = app.stages[0], app.stages[1], app.stages[4]

# 1. Connect no longer offers a database anywhere.
assert "dbname" not in connect.pg_fields, connect.pg_fields.keys()
assert "database" not in connect.ms_fields, connect.ms_fields.keys()

# 2. Connect commits without one, and keeps whatever the pickers chose.
connect.pg_fields["host"].set("pg01")
connect.ms_fields["server"].set("sql01")
assert connect._commit()
assert app.pg.dbname == "" and app.ms.database == ""

# 3. Tables refuses to read a schema until a SOURCE is picked.
tables.refresh()
assert warnings and "Pick a database" in warnings[-1][0], warnings
assert app.tables == []

# 3b. A blank target means "same name as the source", not an error.
tables.picker.src.set_values(["shop"]); tables.picker.src.set("shop")
tables.picker.tgt.set("")
assert tables.picker.require()
assert app.ms.database == "shop", app.ms.database

# 4. Picking a pair on Tables flows into the shared config.
tables.picker.src.set_values(["shop", "audit"]); tables.picker.src.set("shop")
tables.picker.tgt.set_values(["shop_ms"]);       tables.picker.tgt.set("shop_ms")
tables.picker._commit()
assert (app.pg.dbname, app.ms.database) == ("shop", "shop_ms")

# 5. Re-committing Connect preserves the pair rather than blanking it.
assert connect._commit()
assert (app.pg.dbname, app.ms.database) == ("shop", "shop_ms")

# 6. Verify shows the same pair, and pointing it elsewhere drops stale schema.
app.tables = ["fake-table"]; app.loaded_pair = ("shop", "shop_ms")
verify.on_show()
assert verify.picker.src.get() == "shop", verify.picker.src.get()
verify.picker.src.set_values(["shop", "audit"]); verify.picker.src.set("audit")
verify.picker._commit()
assert app.pg.dbname == "audit"
assert app.tables == [] and app.loaded_pair is None, "stale schema kept"

# 7. Profiles still carry the pair even though no field holds it.
connect.apply_profile({"pg": {"host": "pg02", "dbname": "billing"},
                       "ms": {"server": "sql02", "database": "billing_ms"}})
assert connect.pg_fields["host"].get() == "pg02"
assert (app.pg.dbname, app.ms.database) == ("billing", "billing_ms")


# 8. Switching pairs also drops a report that belonged to the old pair.
app = mapped()
app.transfer_summary = {"rows": 10}
app.verify_report = {"passed": True}
app.set_databases("other", "other_ms")
assert app.transfer_summary == {} and app.verify_report is None

# 9. Migrate mode drives preflight, the transport and the table list.
from pgbridge.engine import (Column, Connections, Issue, MsConfig, Options, PgConfig,
                             Preflight, SchemaBuilder, Table, describe_ms_column,
                             describe_pg_column, generate_ddl, is_serial)

missing = Table(name="orders", target_exists=False, columns=[
    Column(name="id", pg_type="bigint", nullable=False, is_pk=True,
           default="nextval('orders_id_seq'::regclass)"),
    Column(name="ref", pg_type="character varying", nullable=False, is_pk=False,
           max_length=64),
    Column(name="total", pg_type="numeric", nullable=True, is_pk=False,
           precision=12, scale=2),
])

_conns = Connections(PgConfig(dbname="src"), MsConfig(database="dst", server="sql01"))
data_only = Preflight(_conns, [missing], Options(mode="data"))
issue = data_only._table_exists(missing)[0]
assert issue.level == "stop", issue

with_schema = Preflight(_conns, [missing], Options(mode="both"))
issue = with_schema._table_exists(missing)[0]
assert issue.level == "note", issue          # no longer blocks: it gets created

# The finding has to carry the facts behind it, not just a sentence.
assert issue.facts["Primary key"] == "id", issue.facts
assert issue.facts["Identity on create"] == "id", issue.facts
assert issue.facts["Source columns"] == "3", issue.facts
assert "dst on sql01" in issue.facts["Target database"], issue.facts

ddl = generate_ddl("dbo", missing)
assert "[id] bigint IDENTITY(1,1) NOT NULL" in ddl, ddl
assert "[ref] nvarchar(64) NOT NULL" in ddl, ddl
assert "[total] decimal(12,2) NULL" in ddl, ddl
assert "CONSTRAINT [PK_orders] PRIMARY KEY ([id])" in ddl, ddl
assert is_serial(missing.columns[0]) and not is_serial(missing.columns[1])

assert Options(mode="schema").with_schema and not Options(mode="schema").with_data
assert Options(mode="data").with_data and not Options(mode="data").with_schema

# A dry run reports the DDL without touching the server.
built = SchemaBuilder(None, [missing], Options(mode="schema", dry_run=True))
assert built.missing() == [missing] and built.create_missing() == []

# 10. The table list follows the mode rather than crying "not in target".
app = mapped()
app.tables = [missing]
app.options.mode = "schema"
app.stages[1]._render()
STATE = [c[0] for c in A.TablesStage.COLUMNS].index("state")
PLAN = [c[0] for c in A.TablesStage.COLUMNS].index("plan")
row = app.stages[1].tree.item("orders")["values"]
assert row[STATE] == "will be created", row
assert row[PLAN] == "create only", row          # schema mode: no rows move
assert app.stages[1].stat_labels["rows"].cget("text") == "none — schema only"
app.options.mode = "data"
app.stages[1]._render()
assert app.stages[1].tree.item("orders")["values"][STATE] == "not in target"

# 11. The start screen routes each activity through its own stages.
import json, os, tempfile
from pgbridge.engine import Transport, read_checkpoint

app = mapped()
app.update()
assert app.chooser.winfo_ismapped(), "the chooser must be the first screen"
assert not app.body.winfo_ismapped(), "stages must wait for an activity"

app.set_activity("verify")
app.update()
assert app.body.winfo_ismapped() and not app.chooser.winfo_ismapped()
assert app.stages[1].winfo_manager() == "", "Tables is not part of verifying"
app.change_activity()
app.update()
assert app.chooser.winfo_ismapped(), "Switch activity must come back here"
app.set_activity("verify")
assert app.activity == "verify"
assert app.rail.visible == [0, 4], app.rail.visible
assert app.stages[0]._next_stage() == 4          # Connect -> Verify, no transfer
app.set_activity("migrate")
assert app.rail.visible == [0, 1, 2, 3, 4, 5], app.rail.visible
assert app.stages[0]._next_stage() == 1

# 12. Every log line reaches a file, which is what survives a kill.
out = tempfile.mkdtemp()
app.options.output_dir = out
app.say("checkpoint of the log itself", "warn")
written = os.listdir(out)
assert written and written[0].startswith("migrate_"), written
assert "checkpoint of the log itself" in open(os.path.join(out, written[0]),
                                              encoding="utf-8").read()

# 13. A migration verifies itself; a stopped or schema-only run does not.
ran = []
app.stages[4].run = lambda: ran.append(True)
app.transport = None
app.stages[3]._expected = 1
app.stages[3]._moved = {"a": 1}
base = {"tables_ok": 1, "tables_total": 1, "rows": 1, "elapsed": 1.0}
app.options.mode = "both"
app.stages[3]._finished({**base, "cancelled": False})
assert ran == [True], "clean transfer must verify itself"
app.stages[3]._finished({**base, "cancelled": True})
assert ran == [True], "a stopped transfer must not claim verification"
app.options.mode = "schema"
app.stages[3]._finished({**base, "cancelled": False})
assert ran == [True], "schema-only has no rows to verify"

# 14. Killed mid-table: the checkpoint says so, and the resume clears and redoes
#     that table while skipping the ones that finished.
out = tempfile.mkdtemp()
conns = Connections(PgConfig(dbname="src"), MsConfig(database="dst"))
json.dump({"updated": "2026-09-07T10:00:00", "pair": ["src", "dst"],
           "mode": "both",
           "tables": {"done_one": {"status": "done", "rows": 100},
                      "half_one": {"status": "running", "rows": 40}}},
          open(os.path.join(out, "checkpoint.json"), "w", encoding="utf-8"))

saved = read_checkpoint(out, ["src", "dst"])
assert saved["done"] == ["done_one"] and saved["unfinished"] == ["half_one"]
assert read_checkpoint(out, ["other", "dst"]) is None, "must not cross pairs"

tables = [Table(name="done_one", target_exists=True),
          Table(name="half_one", target_exists=True)]
t = Transport(conns, tables,
              Options(mode="schema", resume=True, dry_run=False, output_dir=out))
# Current recovery requires the exact plan context; old checkpoints are refused.
checkpoint = json.load(open(os.path.join(out, "checkpoint.json"), encoding="utf-8"))
checkpoint["context"] = t.resume_context
checkpoint["mode"] = "schema"
with open(os.path.join(out, "checkpoint.json"), "w", encoding="utf-8") as stream:
    json.dump(checkpoint, stream)
t._run()
assert t.force_clear == {"half_one"}, t.force_clear
assert set(t.results) == {"done_one", "half_one"}, t.results
assert t.results["done_one"]["rows"] == 100  # skipped results survive recovery

# 15. The pump must survive the transfer clearing itself mid-drain.
#     (_finished sets app.transport = None while _pump is still reading its
#     queue; re-reading the attribute each iteration crashed on the next one.)
import queue as _queue

class _FakeTransport:
    def __init__(self):
        self.events = _queue.Queue()
        self.results = {"a": {"status": "done", "rows": 1}}
        self.cancel = type("E", (), {"is_set": lambda self: False})()

app = mapped()
app.set_activity("migrate")
app.stages[3]._expected = 1
app.stages[3]._moved = {"a": 1}
app.stages[3]._table_count = 1
app.stages[4].run = lambda: None
app.options.mode = "schema"                    # keeps auto-verify out of the way
fake = _FakeTransport()
app.transport = fake
for event in ({"kind": "log", "level": "info", "message": "moving"},
              {"kind": "finished", "tables_ok": 1, "tables_total": 1,
               "rows": 1, "elapsed": 1.0, "cancelled": False},
              {"kind": "log", "level": "info", "message": "trailing event"}):
    fake.events.put(event)
app._pump()                                    # crashed here before the fix
assert app.transport is None

# 16. Layout: nothing that carries an action may be squeezed out of the window,
#     and no button may be narrower than its own label.
from pgbridge.widgets import Button as _Btn

def _walk(w, out):
    out.append(w)
    for c in w.winfo_children():
        _walk(c, out)

def _clipped(root):
    bad = []
    widgets = []
    _walk(root, widgets)
    for w in widgets:
        if isinstance(w, _Btn) and w.winfo_ismapped():
            need = w._measure(w.text) + w._pad * 2
            if w.winfo_width() < need - 1:
                bad.append(f"button {w.text!r} {w.winfo_width()}px < {need}px")
        elif (w.winfo_ismapped() and w.winfo_class() == "TLabel"
              and w.winfo_width() < w.winfo_reqwidth() - 2):
            bad.append(f"label {str(w.cget('text'))[:40]!r} clipped")
    return bad

app = mapped()
app.update()
assert not _clipped(app), _clipped(app)
app.set_activity("migrate")
for i in range(5):
    app.unlock(i); app._goto(i); app.update()
    assert not _clipped(app), (i, _clipped(app))
    # the stage's own action row has to be on screen, not below the fold
    foot = app.stages[i].winfo_children()[0].body.winfo_children()
    assert any(c.winfo_ismapped() for c in foot), f"stage {i} lost its footer"

app._toggle_log()
app.update()
assert app.log_wrap.winfo_ismapped(), "the log drawer must actually appear"
assert app.log_wrap.winfo_height() > 20, app.log_wrap.winfo_height()
app._goto(3)                       # the tallest stage: options, list, footer
app.geometry("1120x720")           # the declared minimum size
app.update()
assert app.log_wrap.winfo_ismapped(), "drawer squeezed out at minimum size"
assert app.stages[3].next_btn.winfo_ismapped(), "action row lost at minimum size"
assert not _clipped(app), _clipped(app)

# 17. A finding must say which column, how both sides declare it, and the counts.
#     "email holds 52 NULLs" alone is not actionable.
assert describe_pg_column(Column(name="email", pg_type="character varying",
                                 nullable=True, is_pk=False, max_length=254)) \
    == "character varying(254), NULL"
assert describe_pg_column(missing.columns[0]) \
    == "bigint, NOT NULL, primary key, default nextval('orders_id_seq'::regclass)"
assert describe_ms_column({"type": "nvarchar", "max_length": 508, "precision": 0,
                           "scale": 0, "nullable": False, "identity": False,
                           "default": None}) == "nvarchar(254), NOT NULL"
assert describe_ms_column({"type": "decimal", "max_length": 9, "precision": 12,
                           "scale": 2, "nullable": True, "identity": False,
                           "default": "((0))"}) \
    == "decimal(12,2), NULL, default ((0))"

class _Cur:
    """Enough of a cursor to drive one check without a server."""
    def __init__(self, rows): self.rows = rows
    def execute(self, *a): return self
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass

class _Conn:
    def __init__(self, *batches): self.batches = list(batches)
    def cursor(self): return _Cur(self.batches.pop(0))

email = Column(name="email", pg_type="character varying", nullable=True,
               is_pk=False, max_length=254)
customers = Table(name="customer", source_rows=124_003, target_exists=True,
                  columns=[email])
pf = Preflight(_conns, [customers], Options())
pf._tgt_cols = {"customer": {"email": {
    "type": "nvarchar", "max_length": 508, "precision": 0, "scale": 0,
    "nullable": True, "identity": False, "default": None}}}
pf._ms_index_for = lambda *a: "UQ_customer_email (unique)"
src = _Conn([("email", "customer_email_key")], [(52,)])   # index scan, null count
found = pf._nullable_unique(customers, src, None)[0]

assert found.level == "stop"
assert "52 NULLs" in found.detail and "51 rows would be rejected" in found.detail
f = found.facts
assert f["Column"] == "customer.email", f
assert f["Source column"] == "character varying(254), NULL", f
assert f["Target column"] == "nvarchar(254), NULL", f
assert f["Source unique index"] == "customer_email_key (unique, not primary)", f
assert f["NULLs in source"] == "52 of 124,003 rows (0.0%)", f
assert f["Rows that would fail"].startswith("51"), f
assert f["Target index"] == "UQ_customer_email (unique)", f
assert "WHERE [email] IS NOT NULL" in found.remedy, found.remedy
assert found.columns == ["email"], found.columns

# and the other checks name their columns too
unsupported = Preflight(_conns, [], Options())._unsupported_types(
    Table(name="t", columns=[Column(name="tags", pg_type="ARRAY",
                                    nullable=True, is_pk=False)]))[0]
assert unsupported.columns == ["tags"], unsupported.columns
notempty = Preflight(_conns, [], Options(clear_target=False))._target_not_empty(
    Table(name="t", target_rows=5, target_exists=True,
          columns=[Column(name="id", pg_type="bigint", nullable=False,
                          is_pk=True)]))[0]
assert notempty.columns == ["id"], notempty.columns

# 18. The detail pane renders all of it, aligned, with the remedy last.
app = mapped()
app.set_activity("migrate")
app.issues = [found]
stage = app.stages[2]
stage._loaded([found])
stage.tree.selection_set("0")
stage._show_detail()
shown = stage.detail.get("1.0", "end")
for line in ("customer.email", "character varying(254), NULL", "nvarchar(254), NULL",
             "52 of 124,003 rows", "How to fix", "IS NOT NULL"):
    assert line in shown, (line, shown)
assert shown.index("Source column") < shown.index("How to fix"), "facts come first"
assert "STOP" in stage.detail_head.cget("text")

# 19. The findings list itself must name the column, and the worst finding must
#     open with its detail already showing — an empty pane reads as "no detail".
app = mapped()
app.set_activity("migrate")
stage = app.stages[2]

many = Issue("stop", "orders", "Column mismatch", "five columns missing.",
             "Apply the migrations.", {"Columns missing on the target": "5"},
             ["a", "b", "c", "d", "e"])
note = Issue("note", "orders", "Target holds rows", "1,200 rows deleted first.",
             "", {"Rows in target now": "1,200"}, ["id"])
warn = Issue("warn", "—", "Collation", "case-insensitive.", "Rebuild.", {})

assert found.column_label == "email"
assert many.column_label == "a, b, c +2 more", many.column_label
assert warn.column_label == "—"

stage._loaded([note, warn, found])          # deliberately worst-last
assert stage.tree.item("0")["values"][2] == "id", stage.tree.item("0")["values"]
assert stage.tree.item("2")["values"][2] == "email"
# selection landed on the STOP, not on row 0
assert stage.tree.selection() == ("2",), stage.tree.selection()
assert "customer.email" in stage.detail_head.cget("text"), \
    stage.detail_head.cget("text")
assert "character varying(254)" in stage.detail.get("1.0", "end")

stage._loaded([])
assert "No findings" in stage.detail_head.cget("text")
assert "Every check passed" in stage.detail.get("1.0", "end")

# 20. No check may leave a cursor holding unread rows: SQL Server without MARS
#     then fails the next command with "Connection is busy with results for
#     another command", which is what killed a real preflight run.
from pgbridge.engine import fetch, fetch_one

class _BusyError(Exception):
    pass

class _StrictCursor:
    """Refuses to run while a sibling cursor still has rows pending."""
    def __init__(self, conn, rows):
        self.conn, self.rows, self.open = conn, rows, False
    def execute(self, sql, params=()):
        if self.conn.active is not None and self.conn.active is not self:
            raise _BusyError("Connection is busy with results for another command")
        self.conn.active = self
        self.open = True
        return self
    def fetchall(self):
        self.conn.active = None
        return self.rows
    def fetchone(self):
        # Mirrors pyodbc: one row taken, the result set stays active.
        return self.rows[0] if self.rows else None
    def close(self):
        if self.conn.active is self:
            self.conn.active = None

class _StrictConn:
    def __init__(self, rows): self.rows, self.active = rows, None
    def cursor(self): return _StrictCursor(self, self.rows)

# the helper always frees the connection, even when only one row is wanted
conn = _StrictConn([(1,), (2,)])
assert fetch(conn, "SELECT 1") == [(1,), (2,)]
assert conn.active is None, "fetch left the connection busy"
assert fetch_one(conn, "SELECT 1") == (1,)
assert conn.active is None, "fetch_one left the connection busy"
fetch(conn, "SELECT 1")
fetch(conn, "SELECT 1")            # would raise _BusyError if the first hung on

# and the checks that broke in the field now run clean back to back
strict = _StrictConn([])
pf2 = Preflight(_conns, [customers], Options())
pf2._tgt_cols = {}
pf2._target_columns(customers, strict)
pf2._columns_align(customers, strict)     # this pair raised in production
pf2._ms_index_for(customers, "email", strict)
pf2._index_key_size(customers, strict)
pf2._database_settings(strict)
assert strict.active is None

# 21. Per-table plans: a blocked table takes its own route without holding back
#     the rest, and each plan must move exactly what it claims.
app = mapped()
app.set_activity("migrate")
app.unlock(2); app._goto(2)
app.tables = [Table(name="good_one", source_rows=10, target_exists=True),
              Table(name="bad_rows", source_rows=100, target_exists=True,
                    columns=[Column(name="email", pg_type="character varying",
                                    nullable=True, is_pk=False, max_length=254)]),
              Table(name="bad_cols", source_rows=50, target_exists=True)]
app.options.mode = "both"
stage = app.stages[2]
stage._preflight_selection = {t.name for t in app.tables}
stage._preflight_mode = "both"

rows_issue = Issue("stop", "bad_rows", "Nullable unique column",
                   "email holds 52 NULLs.", "Filter the index.", {}, ["email"],
                   {"plans": ["clean", "schema", "skip"],
                    "row_filter": '"email" IS NOT NULL',
                    "note": "leaves 52 row(s) behind; 48 move"})
cols_issue = Issue("stop", "bad_cols", "Column mismatch",
                   "reversal_ref missing on target.", "Migrate.", {},
                   ["reversal_ref"],
                   {"plans": ["clean", "skip"],
                    "exclude_columns": ["reversal_ref"],
                    "note": "drops reversal_ref"})
stage._loaded([rows_issue, cols_issue])

# both blocked tables are unresolved, so the run is still blocked
assert not stage.next_btn._enabled
assert stage.unresolved == ["bad_cols", "bad_rows"], stage.unresolved
assert "2 blocked" in stage.next_btn.text, stage.next_btn.text

# "move what passes" for the row-level block: a filter, no column drops
stage._apply_plan("bad_rows", "clean")
bad_rows = app.tables[1]
assert bad_rows.row_filter == '"email" IS NOT NULL'
assert bad_rows.exclude_columns == []
assert bad_rows.does_data(app.options) and not bad_rows.does_schema(app.options)
assert not stage.next_btn._enabled, "the other table is still blocked"

# "move what passes" for the column-level block: a column drop, no filter
stage._apply_plan("bad_cols", "clean")
bad_cols = app.tables[2]
assert bad_cols.exclude_columns == ["reversal_ref"]
assert bad_cols.row_filter == ""
assert stage.next_btn._enabled, "every blocked table now has a plan"
assert "Continue with 3 tables" in stage.next_btn.text, stage.next_btn.text

# schema-only and skip act on that table alone
stage._apply_plan("bad_rows", "schema")
assert bad_rows.does_schema(app.options) and not bad_rows.does_data(app.options)
assert bad_rows.row_filter == "", "changing plan must clear the old filter"
stage._apply_plan("bad_rows", "skip")
assert not bad_rows.selected
assert not bad_rows.does_schema(app.options) \
    and not bad_rows.does_data(app.options)
# the healthy table was never touched by any of it
assert app.tables[0].plan == "auto" and app.tables[0].selected
assert app.tables[0].does_data(app.options)

# a plan only offers what its findings actually allow
assert stage._fix_for("bad_cols")["plans"] == ["clean", "skip"], \
    stage._fix_for("bad_cols")["plans"]
stage.tree.selection_set("1")             # the bad_cols finding
stage._render_plans()
assert not stage.plan_buttons["schema"]._enabled, \
    "schema cannot fix a column the target lacks"
assert stage.plan_buttons["clean"]._enabled

# re-running the checks forgets every plan
stage._preflight_selection = {t.name for t in app.tables}
for t in app.tables:
    t.plan, t.row_filter, t.exclude_columns = "auto", "", []
assert all(t.plan == "auto" for t in app.tables)

# 22. A finding with no table cannot be routed around at all.
stage._loaded([Issue("stop", "—", "Selection", "No tables selected.",
                     "Choose a table.")])
assert not stage.next_btn._enabled, "a global stop must stay blocking"
assert not stage.routes.winfo_ismapped(), "no table to plan for"

# with nothing blocking, the plan controls stay out of the way
stage._loaded([Issue("note", "good_one", "Target holds rows", "5 rows.", "")])
app.update()
assert not stage.routes.winfo_ismapped()
assert stage.next_btn._enabled

# 23. The plan has to reach the SQL. A filter that stays in the UI is worse
#     than no filter at all: it claims rows were excluded when they were not.
from pgbridge.engine import Transport as _T, Verifier, write_report

captured = {"selects": [], "counts": []}

class _RecCur:
    def __init__(self, rows, log):
        self.rows, self.log, self.drained = rows, log, False
        self.itersize = 0
        self.fast_executemany = False
    def execute(self, sql, params=()):
        self.log.append(" ".join(sql.split()))
        return self
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchmany(self, n):
        if self.drained:
            return []
        self.drained = True
        return []                    # dry run: the read is what is under test
    def close(self): pass

class _RecConn:
    def __init__(self, rows, log): self.rows, self.log = rows, log
    def cursor(self, name=None): return _RecCur(self.rows, self.log)
    def commit(self): pass
    def close(self): pass

filtered = Table(name="customer", source_rows=100, target_exists=True,
                 columns=[Column(name="id", pg_type="bigint", nullable=False,
                                 is_pk=True),
                          Column(name="email", pg_type="text", nullable=True,
                                 is_pk=False),
                          Column(name="tags", pg_type="ARRAY", nullable=True,
                                 is_pk=False)])
filtered.plan = "clean"
filtered.row_filter = '"email" IS NOT NULL'
filtered.exclude_columns = ["tags"]

assert filtered.migrating_columns == ["id", "email"], filtered.migrating_columns
assert filtered.does_data(Options()) and not filtered.does_schema(Options())
assert "clean data" in filtered.plan_summary(Options())

# the source read carries both the filter and the column exclusion
log = []
t = _T(Connections(PgConfig(dbname="s"), MsConfig(database="d")), [filtered],
       Options(dry_run=True, output_dir=tempfile.mkdtemp()))
t.conns.source = lambda *a, **k: _RecConn([(48,)], log)
t.conns.target = lambda *a, **k: _RecConn([], log)
t._copy_table(filtered)
selects = [q for q in log if q.startswith("SELECT")]
assert any('WHERE "email" IS NOT NULL' in q for q in selects), selects
data_read = [q for q in selects if '"id"' in q]
assert data_read and '"tags"' not in data_read[0], data_read
assert t.results["customer"]["rows_excluded"] == 52, t.results["customer"]
assert t.results["customer"]["columns_excluded"] == ["tags"]

# 24. Verification must measure a filtered table against what it was asked to
#     move, or every clean plan reports as a failure.
v = Verifier(Connections(PgConfig(dbname="s"), MsConfig(database="d")),
             [filtered], Options())
counts = []
class _Pair:
    """Every count comes back 48: source-with-filter and target agree."""
    def __init__(self, value): self.value = value
    def cursor(self, name=None): return _RecCur([(self.value,)], counts)
    def close(self): pass

entry = v._counts(filtered, _Pair(48), _Pair(48))
assert entry["status"] == "ok", entry
assert "excluded by" in entry["detail"], entry["detail"]

# a table that deliberately moved nothing is "skip", never "fail"
schema_only = Table(name="t2", source_rows=9, target_exists=True, target_rows=0)
schema_only.plan = "schema"
v2 = Verifier(Connections(PgConfig(), MsConfig()), [schema_only], Options())
v2._untrusted_keys = lambda tgt: []
v2._identity_seeds = lambda tgt: []
v2.conns.source = lambda *a, **k: _Pair(0)
v2.conns.target = lambda *a, **k: _Pair(0)
report = v2.run()
assert report["tables"][0]["status"] == "skip", report["tables"][0]
assert "no rows were expected" in report["tables"][0]["detail"]
assert not report["passed"] and report["incomplete"], report

# 25. The exported report records what was left behind, or nobody can audit it.
out = tempfile.mkdtemp() + "/r.json"
write_report(out, PgConfig(dbname="s"), MsConfig(database="d"), Options(),
             [rows_issue], {"rows": 48}, None, [filtered, schema_only])
saved = json.load(open(out, encoding="utf-8"))
partial = {p["table"]: p for p in saved["plan"]["not_migrating_in_full"]}
assert set(partial) == {"customer", "t2"}, partial
assert partial["customer"]["row_filter"] == '"email" IS NOT NULL'
assert partial["customer"]["excluded_columns"] == ["tags"]
assert partial["t2"]["plan"] == "schema"
assert saved["preflight"][0]["fix"]["row_filter"] == '"email" IS NOT NULL'
assert saved["preflight"][0]["columns"] == ["email"]

# 26. "Cannot insert duplicate key" must be a preflight finding, not a transfer
#     crash. Two causes: a case-insensitive target collation, and a target key
#     narrower than the source's.
from pgbridge.engine import _explain

class _KeyCur:
    def __init__(self, script, log): self.script, self.log = script, log
    def execute(self, sql, params=()):
        self.flat = " ".join(sql.split())
        self.log.append(self.flat)
        return self
    def fetchall(self):
        for pattern, rows in self.script:
            if pattern in self.flat:
                return rows
        return []
    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None
    def close(self): pass

class _KeyConn:
    def __init__(self, script, log): self.script, self.log = script, log
    def cursor(self, name=None): return _KeyCur(self.script, self.log)
    def close(self): pass

keyed = Table(name="core_importjob", source_rows=1000,
              target_exists=True,
              columns=[Column(name="id", pg_type="uuid", nullable=False,
                              is_pk=True),
                       Column(name="name", pg_type="text", nullable=True,
                              is_pk=False)])
qlog = []
tgt_conn = _KeyConn([("is_primary_key = 1",
                      [("PK_core_importjob", "id")])], qlog)
src_conn = _KeyConn([("HAVING COUNT(*) > 1 ORDER BY", [("0feb8d", 2), ("a1c2", 3)]),
                     ("COALESCE(SUM(n - 1)", [(3,)])], qlog)

pf3 = Preflight(_conns, [keyed], Options())
pf3.collation = "SQL_Latin1_General_CP1_CI_AS"
found_key = pf3._key_collisions(keyed, src_conn, tgt_conn)[0]

assert found_key.level == "stop"
assert found_key.check == "Duplicate target key"
assert "3 row(s) collide" in found_key.detail, found_key.detail
assert "PK_core_importjob" in found_key.detail
assert found_key.columns == ["id"], found_key.columns
# a uuid key is compared case-insensitively, the way the target will compare it
assert any('lower("id"::text)' in q for q in qlog), qlog
f = found_key.facts
assert f["Rows that would be rejected"] == "3", f
assert "CI_AS" in f["Target collation"], f
assert "  0feb8d" in f and f["  0feb8d"] == "2 source rows", f
# schema-only or skip are the honest options; no filter makes a key unique
assert found_key.fix["plans"] == ["schema", "skip"]

# a case-SENSITIVE target with a matching key width has nothing to report
pf4 = Preflight(_conns, [keyed], Options())
pf4.collation = "SQL_Latin1_General_CP1_CS_AS"
assert pf4._key_collisions(keyed, src_conn, tgt_conn) == []

# 27. A plan must never drop a column the target keys on.
keyed.exclude_columns = ["id"]
pf5 = Preflight(_conns, [keyed], Options())
pf5.collation = "SQL_Latin1_General_CP1_CS_AS"
dropped = pf5._key_collisions(keyed, src_conn, tgt_conn)[0]
assert dropped.check == "Key column not sent", dropped.check
assert dropped.fix["plans"] == ["skip"], dropped.fix
keyed.exclude_columns = []

# 28. If it does reach the transfer, the driver text is translated, not echoed.
raw = Exception("('23000', \"[23000] [Microsoft][ODBC Driver 18 for SQL Server]"
                "Violation of PRIMARY KEY constraint 'PK_core_importjob'. "
                "Cannot insert duplicate key. (2627) (SQLParamData)\")")
explained = _explain(raw, "core_importjob")
assert "duplicate primary key" in str(explained), str(explained)
assert "Preflight reports this before any row moves" in str(explained)
assert _explain(Exception("something else"), "t") is not explained

# 29. The uuid mangling that made distinct keys collide: SQL Server's wide types
#     are UTF-16LE, so the utf-8 override turned "0FEB8D..." into "䘰䉅䐸".
import inspect
from pgbridge.engine import Connections as _C
code = [ln.split("#")[0] for ln in inspect.getsource(_C.target).splitlines()]
assert not any("setdecoding(" in ln or "setencoding(" in ln for ln in code), \
    "the utf-8 override is back — it mangles every wide column"
assert "0FEB8D".encode("ascii").decode("utf-16le") == "䘰䉅䐸"

# 30. Verification has to be strong enough to catch what a row count cannot:
#     truncation, coercion, lost NULLs, mangled keys.
from pgbridge.engine import JobStore, Job, job_text_report, new_job_id, _same

profiled = Table(name="customer", source_rows=100, target_exists=True,
                 columns=[Column(name="id", pg_type="uuid", nullable=False,
                                 is_pk=True),
                          Column(name="email", pg_type="text", nullable=True,
                                 is_pk=False),
                          Column(name="balance", pg_type="numeric",
                                 nullable=True, is_pk=False, precision=12,
                                 scale=2),
                          Column(name="joined", pg_type="timestamp with time zone",
                                 nullable=True, is_pk=False),
                          Column(name="active", pg_type="boolean",
                                 nullable=False, is_pk=False)])
v3 = Verifier(Connections(PgConfig(dbname="s"), MsConfig(database="d")),
              [profiled], Options())
labels = [spec[0] for spec in v3._profile_specs(profiled)]
# a uuid key: length totals catch the encoding truncation, distinct catches collisions
assert "id: longest value" in labels, labels
assert "id: total characters" in labels
assert "id: distinct keys" in labels
assert "email: non-null count" in labels
assert "balance: sum" in labels and "balance: minimum" in labels
assert "joined: earliest" in labels and "joined: latest" in labels
assert "active: true count" in labels
# only the key column gets a distinct count; it is the expensive one
assert sum(1 for l in labels if l.endswith("distinct keys")) == 1

# excluded columns are not profiled: they were never meant to arrive
profiled.exclude_columns = ["email"]
assert not any(spec[0].startswith("email:")
               for spec in v3._profile_specs(profiled))
profiled.exclude_columns = []

# the comparison is cross-engine: Decimal 5 == 5, padded char == unpadded
assert _same(decimal.Decimal("5.00"), 5)
assert _same("abc  ", "abc")
assert not _same("abc", "abd")
assert not _same(None, 0)

# a truncated uuid column is caught even though the row counts agree
class _Two:
    def __init__(self, vals, log): self.vals, self.log = vals, log
    def cursor(self, name=None): return _RecCur([self.vals], self.log)
    def close(self): pass

plog = []
#                 non-null, longest, total chars, distinct
src_profile = (100, 32, 3200, 100)
tgt_profile = (100, 16, 1600, 98)          # utf-16 truncation: half the length
one_col = Table(name="j", source_rows=100, target_exists=True,
                columns=[Column(name="id", pg_type="uuid", nullable=False,
                                is_pk=True)])
drift = v3._profile(one_col, _Two(src_profile, plog), _Two(tgt_profile, plog))
assert drift["status"] == "fail", drift
found_checks = {d["check"] for d in drift["profile"]}
assert "id: longest value" in found_checks, found_checks
assert "id: distinct keys" in found_checks
assert "3 column check(s) differ" in drift["detail"], drift["detail"]

# identical profiles pass and say how much was checked
same = v3._profile(one_col, _Two(src_profile, plog), _Two(src_profile, plog))
assert same.get("status") != "fail", same
assert "4 column checks matched" in same["detail"], same

# 31. Levels: each does strictly more than the one before.
assert list(Verifier.LEVELS) == ["counts", "profile", "full"]
calls = []
v4 = Verifier(Connections(PgConfig(), MsConfig()), [one_col], Options())
v4._counts = lambda t, s, g: (calls.append("counts"),
                              {"table": t.name, "source": 1, "target": 1,
                               "status": "ok", "detail": ""})[1]
v4._profile = lambda t, s, g: (calls.append("profile"), {"profile": []})[1]
v4._sample = lambda t, s, g, n: (calls.append("sample"), {"sampled": n})[1]
v4._untrusted_keys = lambda g: []
v4._identity_seeds = lambda g: []
v4.conns.source = lambda *a, **k: _Two((0,), [])
v4.conns.target = lambda *a, **k: _Two((0,), [])
for level, expected in (("counts", ["counts"]),
                        ("profile", ["counts", "profile"]),
                        ("full", ["counts", "profile", "sample"])):
    calls.clear()
    report = v4.run(level=level)
    assert calls == expected, (level, calls)
    assert report["level"] == level
# a table whose counts already differ is not profiled further
v4._counts = lambda t, s, g: {"table": t.name, "source": 1, "target": 2,
                              "status": "fail", "detail": "differs"}
calls.clear()
v4.run(level="full")
assert calls == [], calls

# 32. A job records the whole run and survives the process.
out = tempfile.mkdtemp()
store = JobStore(out)
assert store.list() == []
job = Job(id=new_job_id(), activity="migrate", started="2026-09-08T09:00:00",
          source={"database": "shop", "host": "pg01", "schema": "public"},
          target={"database": "shop_ms", "server": "sql01", "schema": "dbo"},
          mode="both")
job.note("warn", "customer: leaving 52 row(s) behind")
job.tables = [{"name": "customer", "selected": True, "plan": "clean",
               "source_rows": 100, "row_filter": '"email" IS NOT NULL',
               "excluded_columns": ["tags"], "why": ["leaves 52 rows behind"]}]
job.transfer = {"rows": 48, "tables_ok": 1, "tables_total": 1, "seconds": 2.0,
                "cancelled": False,
                "detail": {"customer": {"status": "done", "rows": 48,
                                        "rows_excluded": 52}}}
job.verification = {"passed": False, "level": "full", "rows_verified": 48,
                    "rows_sampled": 48, "tables_checked": 1, "tables_failed": 1,
                    "tables": [{"table": "customer", "status": "fail",
                                "source": 48, "target": 40, "detail": "8 short",
                                "profile": [{"check": "id: longest value",
                                             "source": "32", "target": "16"}],
                                "sample_mismatches": [
                                    {"key": "abc", "column": "id",
                                     "source": "0feb8d", "target": "䘰䉅"}]}],
                    "untrusted_foreign_keys": [], "identity_problems": []}
job.status, job.finished = "failed", "2026-09-08T09:05:00"
store.save(job)

reread = JobStore(out)              # a fresh store, as a later session would
listed = reread.list()
assert len(listed) == 1 and listed[0]["id"] == job.id
assert reread.get(job.id)["mode"] == "both"
assert listed[0]["log"][0]["message"].startswith("customer: leaving")

# newest first, whatever the order they were written
older = Job(id=new_job_id(), activity="verify", started="2026-09-07T08:00:00")
store.save(older)
assert [j["started"] for j in reread.list()] == ["2026-09-08T09:00:00",
                                                 "2026-09-07T08:00:00"]

# 33. The text report has to answer "what happened" without the app.
text = job_text_report(reread.get(job.id))
for needed in ("pgbridge migrate job", "shop on pg01", "shop_ms on sql01",
               "NOT MIGRATED IN FULL", '"email" IS NOT NULL', "without tags",
               "leaves 52 rows behind", "TRANSFER", "52 rows excluded",
               "VERIFICATION", "FAILED", "id: longest value",
               "row abc column id", "ACTIVITY LOG",
               "customer: leaving 52 row(s) behind"):
    assert needed in text, needed
assert text.count("=" * 78) >= 4

# 34. Switching activity must not hand the new run the old one's screens.
#     Reported from the field: finishing a migration, then switching to
#     verification, showed the migration's tables, findings and verdict.
app = mapped()
app.options.output_dir = tempfile.mkdtemp()
app.jobs = JobStore(app.options.output_dir)
app.set_activity("migrate")
app.pg.dbname, app.ms.database = "shop", "shop_ms"
app.versions = {"source": "PostgreSQL 15", "target": "SQL Server 2019"}
app.tables = [Table(name="orders", source_rows=10, target_exists=True)]
app.issues = [Issue("stop", "orders", "Column mismatch", "x missing.", "fix", {},
                    ["x"])]
app.transfer_summary = {"rows": 10, "tables_ok": 1, "tables_total": 1}
app.verify_report = {"passed": True, "tables": [{"table": "orders", "source": 10, "target": 10, "status": "ok", "detail": ""}], "tables_checked": 1, "rows_verified": 10,
                     "untrusted_foreign_keys": [], "identity_problems": []}
app.loaded_pair = ("shop", "shop_ms")
app.stages[1]._render()
app.stages[2]._loaded(app.issues)
app.stages[4]._loaded(app.verify_report)
assert app.stages[1].tree.get_children(), "precondition: tables are on screen"
assert app.stages[2].tree.get_children()
assert app.stages[4].verdict.text != "not run"

app.set_activity("verify")
assert app.tables == [] and app.issues == [], "run state must be cleared"
assert app.verify_report is None and app.transfer_summary == {}
assert app.loaded_pair is None
assert not app.stages[1].tree.get_children(), "the tables list is still showing"
assert not app.stages[2].tree.get_children(), "old findings are still showing"
assert not app.stages[4].tree.get_children(), "old verification is still showing"
assert app.stages[4].verdict.text == "not run", app.stages[4].verdict.text
assert app.stages[2].detail_head.cget("text") == "Select a finding for the detail"
assert app.stages[3].resume_note.cget("text") == ""
# the servers are kept: switching activity is not disconnecting
assert app.versions["source"] == "PostgreSQL 15"

# 35. Each activity opens its own job, and the previous one is closed honestly.
first = app.job
assert first is not None and first.activity == "verify"
app.set_activity("migrate")
assert app.job is not first and app.job.activity == "migrate"
closed = {j["id"]: j for j in app.jobs.list()}
assert closed[first.id]["status"] == "abandoned", closed[first.id]["status"]
assert closed[app.job.id]["status"] == "running"

# every log line lands on the open job as it is said
app.say("a thing happened", "warn")
assert app.job.log[-1]["message"] == "a thing happened"
assert app.job.log[-1]["level"] == "warn"

# 36. The job history screen lists them, newest first, and renders a report.
app.open_jobs()
app.update()
assert app.jobs_screen.winfo_ismapped(), "the jobs screen must be on screen"
assert not app.body.winfo_ismapped() and not app.chooser.winfo_ismapped()
# three activities were started, so three jobs exist
assert len(app.jobs_screen.rows) == 3, [j["activity"] for j in app.jobs_screen.rows]
assert {j["status"] for j in app.jobs_screen.rows} == {"passed", "abandoned",
                                                       "running"}
assert app.jobs_screen.tree.selection() == ("0",), "the newest opens by default"
shown = app.jobs_screen.detail.get("1.0", "end")
assert "pgbridge migrate job" in shown, shown[:200]
starts = [j["started"] for j in app.jobs_screen.rows]
assert starts == sorted(starts, reverse=True), starts

app.close_jobs()
app.update()
assert app.body.winfo_ismapped(), "Back must return to the run"
assert not app.jobs_screen.winfo_ismapped()

# 37. The preflight badges have to be readable. Text and badge were mixed from
#     the same colour and landed within 1.3:1 of each other, which reads blank.
from pgbridge.widgets import _mix, _shade
from pgbridge.theme import C as _C

def _luminance(hex_colour):
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
           for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)

for tone in ("stop", "warn", "muted", "ok", "source", "target"):
    colour = _C[tone]
    badge = _mix(colour, _C["bg"], 0.86)
    label = _shade(colour, 0.18)
    assert _contrast(label, badge) >= 4.5, \
        f"{tone} badge is {_contrast(label, badge):.2f}:1, below AA"
    # and the badge must not simply be the background either
    assert badge != _C["bg"]

# 38. Verification defaults to the level that finishes on a large database.
assert next(iter(A.VerifyStage.LEVELS.values())) == "counts", \
    list(A.VerifyStage.LEVELS)
app = mapped("verify")
assert app.stages[4].LEVELS[app.stages[4].level.get()] == "counts"

# 39. Patching settings.py: the old block is kept, commented, and the file must
#     still parse. Nothing here may be guessed — it writes to someone's project.
from pgbridge import cutover
from pgbridge.engine import MsConfig as _Ms

project = tempfile.mkdtemp()
settings = os.path.join(project, "settings.py")
ORIGINAL = (
    "import os\n"
    "from pathlib import Path\n"
    "\n"
    "BASE_DIR = Path(__file__).resolve().parent.parent\n"
    "SECRET_KEY = 'keep'\n"
    "\n"
    "DATABASES = {\n"
    "    'default': {\n"
    "        'ENGINE': 'django.db.backends.postgresql',\n"
    "        'NAME': 'shop_app',\n"
    "        'USER': 'postgres',\n"
    "        'HOST': 'localhost',\n"
    "    }\n"
    "}\n"
    "\n"
    "STATIC_URL = 'static/'\n")
open(settings, "w", encoding="utf-8").write(ORIGINAL)

target = _Ms(server="sql01", database="shop_app", user="svc", password="pw",
             driver="ODBC Driver 18 for SQL Server")
assert cutover.find_databases(ORIGINAL) == (7, 14)

result = cutover.patch_settings(settings, target, use_env=False)
patched = open(settings, encoding="utf-8").read()
live = [ln for ln in patched.splitlines() if not ln.lstrip().startswith("#")]
assert not any("postgresql" in ln for ln in live), "old engine is still live"
assert "# DATABASES = {" in patched, "old block must be kept for rollback"
assert "#         'ENGINE': 'django.db.backends.postgresql'," in patched
assert "STATIC_URL = 'static/'" in patched, "the rest of the file must survive"
assert "SECRET_KEY = 'keep'" in patched
assert os.path.exists(result["backup"]), "the original must be backed up"
assert open(result["backup"], encoding="utf-8").read() == ORIGINAL

# it has to be importable Python, and say what we meant
scope: dict = {"__file__": settings}      # settings modules reference it
exec(compile(patched, settings, "exec"), scope)
assert scope["DATABASES"]["default"]["ENGINE"] == "mssql"
assert scope["DATABASES"]["default"]["NAME"] == "shop_app"
assert scope["DATABASES"]["default"]["HOST"] == "sql01"
assert scope["DATABASES"]["default"]["OPTIONS"]["driver"].startswith("ODBC Driver")
assert scope["STATIC_URL"] == "static/"

# 40. The .env route: decouple import added, credentials out of the file, and
#     every key the project already had is preserved.
env_settings = os.path.join(project, "env_settings.py")
open(env_settings, "w", encoding="utf-8").write(ORIGINAL)
cutover.patch_settings(env_settings, target, use_env=True)
env_patched = open(env_settings, encoding="utf-8").read()
assert "from decouple import config" in env_patched
assert 'config("DB_NAME")' in env_patched
assert "'pw'" not in env_patched, "the password must not be in settings.py"
ast.parse(env_patched)
# the import goes with the other imports, before it is used
import_at = next(i for i, ln in enumerate(env_patched.splitlines())
                 if "from decouple import config" in ln)
uses_at = next(i for i, ln in enumerate(env_patched.splitlines())
               if 'config("DB_NAME")' in ln)
assert import_at < uses_at, "the import must precede its use"

env_file = os.path.join(project, ".env")
open(env_file, "w", encoding="utf-8").write(
    "SECRET_KEY=already-here\nDB_NAME=old_postgres_name\nDEBUG=True\n")
report = cutover.update_env_file(env_file, cutover.env_values(target))
body = open(env_file, encoding="utf-8").read()
assert "SECRET_KEY=already-here" in body and "DEBUG=True" in body
assert "DB_NAME=shop_app" in body and "old_postgres_name" not in body
assert "DB_HOST=sql01" in body and "DB_PASSWORD=pw" in body
assert report["updated"] == ["DB_NAME"] and "DB_HOST" in report["added"]
assert not report["created"]
if os.name != "nt":
    assert oct(os.stat(env_file).st_mode)[-3:] == "600", "the .env holds a password"

# windows auth writes no credentials at all
win = _Ms(server="sql01", database="shop_app", auth="windows")
values = cutover.env_values(win)
assert values["DB_USER"] == "" and values["DB_PASSWORD"] == ""
assert values["DB_TRUSTED_CONNECTION"] == "yes"

# 41. It refuses rather than damages: no DATABASES, or a file that would break.
broken = os.path.join(project, "no_db.py")
open(broken, "w", encoding="utf-8").write("SECRET_KEY = 'x'\n")
try:
    cutover.patch_settings(broken, target, use_env=False)
    raise AssertionError("a file with no DATABASES must be refused")
except ValueError as exc:
    assert "No top-level DATABASES" in str(exc)
assert open(broken, encoding="utf-8").read() == "SECRET_KEY = 'x'\n", \
    "the refused file must be untouched"

# 42. Finding the project's interpreter, on either platform layout.
fake = tempfile.mkdtemp()
os.makedirs(os.path.join(fake, "bin"))
posix_py = os.path.join(fake, "bin", "python")
open(posix_py, "w").close()
assert cutover.venv_python(fake) == posix_py
win_env = tempfile.mkdtemp()
os.makedirs(os.path.join(win_env, "Scripts"))
win_py = os.path.join(win_env, "Scripts", "python.exe")
open(win_py, "w").close()
assert cutover.venv_python(win_env) == win_py
assert cutover.venv_python(tempfile.mkdtemp()) is None
assert cutover.venv_python("") is None

# the check runs in that interpreter, not this one
here = cutover.check_packages(sys.executable, ("json", "nonexistent-package-xyz"))
assert here["json"] is True and here["nonexistent-package-xyz"] is False

# 43. The cutover stage previews before it writes, and will not arm without a file.
app = mapped("cutover")
stage = app.stages[5]
app.ms = target
stage.settings_path.set("")
stage._refresh()
assert not stage.apply_btn._enabled, "nothing selected must not be applyable"
assert not stage.test_btn._enabled, "test button must be disabled with no file"
fresh = os.path.join(project, "preview.py")
open(fresh, "w", encoding="utf-8").write(ORIGINAL)
stage.settings_path.set(fresh)
stage._refresh()
assert stage.apply_btn._enabled
assert stage.test_btn._enabled
shown = stage.preview.get("1.0", "end")
assert "Will comment out lines 7–14" in shown, shown[:200]
assert "django.db.backends.postgresql" in shown, "show what is being replaced"
assert open(fresh, encoding="utf-8").read() == ORIGINAL, "preview must not write"

# default route is .env: credentials go there, never into settings.py, and the
# password is masked even in the preview
stage.env_path.set(os.path.join(project, ".env"))
stage._refresh()
env_preview = stage.preview.get("1.0", "end")
assert 'config("DB_NAME")' in env_preview, env_preview[:400]
assert "DB_PASSWORD=********" in env_preview, "the password must be masked"
assert "DB_PASSWORD=pw" not in env_preview

# switching to the hardcoded route previews the literal block instead
hard_label = [k for k, v in stage.WHERE.items() if v is False][0]
stage.where.set(hard_label)
stage._refresh()
hard_preview = stage.preview.get("1.0", "end")
assert '"ENGINE": "mssql"' in hard_preview, hard_preview[:400]
assert "config(" not in hard_preview
assert open(fresh, encoding="utf-8").read() == ORIGINAL, "preview must not write"
# a file with no DATABASES disarms the button
stage.settings_path.set(broken)
stage._refresh()
assert not stage.apply_btn._enabled
assert not stage.test_btn._enabled

# 44. The job report pane must not collapse on a short window: the list took the
#     whole PanedWindow and the report — the point of the screen — vanished.
app = mapped()
app.options.output_dir = tempfile.mkdtemp()
app.jobs = JobStore(app.options.output_dir)
for n in range(6):
    j = Job(id=new_job_id() + str(n), activity="migrate",
            started=f"2026-09-0{n + 1}T09:00:00", status="passed",
            source={"database": "shop_app"}, target={"database": "shop_app"})
    app.jobs.save(j)
# Sizes the window manager will actually allow: the app enforces 1120x720.
app.open_jobs()
if not app._log_open:
    app._toggle_log()                    # the drawer is what makes it tight
for size in ("1280x840", "1200x760", "1120x720"):
    app.geometry(size)
    for _ in range(3):
        app.update()
        time.sleep(0.1)
    app.jobs_screen._sash_placed = False
    app.jobs_screen.place_sash()
    app.jobs_screen.clamp_sash()
    app.update()
    js = app.jobs_screen
    assert js.detail.winfo_ismapped(), f"{size}: the report pane vanished"
    # Neither pane may be starved down to a scrollbar, however little room
    # there is — the log drawer being open is the case that bites.
    fair = max(35, min(80, js.split.winfo_height() // 3))
    assert js.detail.winfo_height() >= fair, \
        f"{size}: report pane is {js.detail.winfo_height()}px of " \
        f"{js.split.winfo_height()}"
    assert js.tree.winfo_height() >= fair, f"{size}: the job list collapsed"
    # and the way out is always reachable
    for widget in (js.count,):
        assert widget.winfo_ismapped(), f"{size}: {widget} is hidden"
    top, bottom = app.winfo_rooty(), app.winfo_rooty() + app.winfo_height()
    for btn in (js.detail, js.tree):
        assert btn.winfo_rooty() >= top - 1, f"{size}: pushed above the window"
app.geometry("1280x840")
if app._log_open:
    app._toggle_log()
app.close_jobs()

# 45. Cutover needs to know which database. From a migration it is carried over;
#     arriving straight there it must be chosen, and an empty one cannot patch.
app = mapped("cutover")
stage = app.stages[5]
app.ms = _Ms(server="sql01", database="", user="svc", password="pw")
stage.reset()
stage.on_show()
assert stage.database.get() == "", "nothing to carry over"
assert "choose the database" in stage.db_note.cget("text").lower()

# a settings file alone is not enough while the database is empty
straight = os.path.join(project, "straight.py")
open(straight, "w", encoding="utf-8").write(ORIGINAL)
stage.settings_path.set(straight)
stage._refresh()
assert not stage.apply_btn._enabled, "must not patch with an empty NAME"
assert "No database chosen" in stage.preview.get("1.0", "end")

# choosing one arms it, and reaches the config that gets written
stage.database.set_values(["shop_app", "other_db"])
stage.database.set("shop_app")
stage._set_database()
assert app.ms.database == "shop_app"
assert stage.apply_btn._enabled
assert 'config("DB_NAME")' in stage.preview.get("1.0", "end")
assert cutover.env_values(app.ms)["DB_NAME"] == "shop_app"

# arriving from a migration, the migrated database is already there
app = mapped("migrate")
app.ms = _Ms(server="sql01", database="shop_app")
app.transfer_summary = {"rows": 10, "tables_ok": 1, "tables_total": 1}
stage = app.stages[5]
stage.reset()
stage.on_show()
assert stage.database.get() == "shop_app", stage.database.get()
assert "carried over" in stage.db_note.cget("text")

# and the hardcoded route writes that name, not an empty one
hard = os.path.join(project, "carried.py")
open(hard, "w", encoding="utf-8").write(ORIGINAL)
cutover.patch_settings(hard, app.ms, use_env=False)
scope2: dict = {"__file__": hard}
exec(compile(open(hard, encoding="utf-8").read(), hard, "exec"), scope2)
assert scope2["DATABASES"]["default"]["NAME"] == "shop_app"

# 47. Every activity card must be reachable on a window the app allows. Four
#     panels do not fit 1120x720, and the last of them — Job history — was
#     simply cut off with no way to scroll to it.
from pgbridge.widgets import ScrollArea

app = mapped()
app.change_activity()                    # back to the chooser
app.update()
scroller = next(w for w in app.chooser.winfo_children()[0].body.winfo_children()
                if isinstance(w, ScrollArea))

app.geometry("1120x720")                 # the app's own minimum
for _ in range(4):
    app.update()
    time.sleep(0.1)
assert scroller._bar_shown, "the chooser must offer a scrollbar when it overflows"
assert scroller.bar.winfo_ismapped()

# scrolling reaches the last card
scroller.canvas.yview_moveto(1.0)
app.update()
history = scroller.body.winfo_children()[-1]
top = scroller.canvas.winfo_rooty()
bottom = top + scroller.canvas.winfo_height()
assert history.winfo_rooty() + history.winfo_height() <= bottom + 4, \
    "the last card is still below the fold after scrolling to the end"

# a tall window puts it away again, and resets the view
app.geometry("1305x1000")
for _ in range(4):
    app.update()
    time.sleep(0.1)
assert not scroller._bar_shown, "the bar must hide when everything fits"
assert not scroller.bar.winfo_ismapped()
assert scroller.canvas.yview()[0] == 0.0, "hiding the bar must scroll back to top"

# the wheel does nothing while there is nothing to scroll
before = scroller.canvas.yview()[0]
scroller._scroll(type("E", (), {"num": 5, "delta": -120})())
assert scroller.canvas.yview()[0] == before

app.geometry("1280x840")
app.update()

# 48. Each card's button must name its own activity — the label was a two-way
#     conditional, so Cutover's button said "Start verification".
labels = []
def _collect(widget):
    for child in widget.winfo_children():
        if isinstance(child, _Btn):
            labels.append(child.text)
        _collect(child)
_collect(app.chooser)
assert "Start migration" in labels, labels
assert "Start verification" in labels, labels
assert "Start cutover" in labels, labels
assert "Open job history" in labels, labels
assert labels.count("Start verification") == 1, labels
assert set(A.START_LABEL) == set(A.ACTIVITIES), "every activity needs a label"

print("stage flow ok")

MAPPED.destroy()
