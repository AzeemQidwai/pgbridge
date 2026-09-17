"""Application window and the five stages of a migration."""

from __future__ import annotations

import datetime
import copy
import json
import os
import queue
import re
import threading
import tkinter as tk
import traceback
from dataclasses import asdict, replace
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import cutover, theme
from .engine import (PLANS, Connections, Introspector, Job, JobStore, MsConfig,
                     Options, PgConfig, Preflight, Table, Transport, Verifier,
                     BatchMigrator, BatchDatabaseItem,
                     job_text_report, new_job_id, read_checkpoint, write_report,
                     estimate_transfer_size, format_bytes)
from .theme import C, SP
from .widgets import (BridgeHeader, Button, Field, LogView, Meter, Pill,
                      ScrollArea, StageRail, section)

PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".pgbridge")
PROFILE_PATH = os.path.join(PROFILE_DIR, "profiles.json")


def _restrict(path: str):
    """Make a saved profile readable only by its owner, on either platform.

    chmod does nothing useful on Windows, where permissions are ACLs, so hand
    that case to icacls: drop inherited entries, grant the current user alone.
    """
    try:
        if os.name == "nt":
            import subprocess
            subprocess.run(["icacls", path, "/inheritance:r",
                            "/grant:r", f"{os.environ['USERNAME']}:F"],
                           check=False, capture_output=True,
                           creationflags=0x08000000)   # CREATE_NO_WINDOW
        else:
            os.chmod(path, 0o600)
    except (OSError, KeyError):
        pass


STAGES = [
    ("Connect", "Point at both servers"),
    ("Tables", "Choose what moves"),
    ("Preflight", "Find what would break"),
    ("Transfer", "Move the rows"),
    ("Verify", "Prove it arrived"),
    ("Cutover", "Point the app at it"),
]

START_LABEL = {"migrate": "Start migration", "verify": "Start verification",
               "cutover": "Start cutover"}

# Verification is a first-class activity, not just the tail of a migration:
# it stands alone for a pair somebody else migrated, or migrated last week.
ACTIVITIES = {
    "migrate": ("Migrate", "Move a database from PostgreSQL to SQL Server, "
                           "verify it landed, then repoint the application "
                           "at it.", [0, 1, 2, 3, 4, 5]),
    "verify":  ("Verify", "Compare a pair that is already migrated. Reads "
                          "both sides, writes nothing.", [0, 4]),
    "cutover": ("Cutover", "Prepare an application for a database that is "
                           "already migrated. Touches the project, not the "
                           "databases.", [0, 5]),
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("pgbridge — PostgreSQL to SQL Server Enterprise Migration")
        self.geometry("1280x840")
        self.minsize(1120, 720)
        self.fonts = theme.apply(self)

        # shared state
        self.pg = PgConfig()
        self.ms = MsConfig()
        self.options = Options()
        self.conns: Connections | None = None
        self.tables: list[Table] = []
        self.issues: list = []
        self.transfer_summary: dict = {}
        self.verify_report: dict | None = None
        self.transport: Transport | None = None
        self.versions = {"source": "", "target": ""}
        self.activity = "migrate"
        self._pump_job = None
        self.jobs = JobStore(self.options.output_dir)
        self.job: Job | None = None
        self.loaded_pair: tuple[str, str] | None = None
        self._busy = False
        self._preflight_signature = None

        self._build()
        self._load_profile()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_job = self.after(60, self._pump)

    # ------------------------------------------------------------------ chrome
    def _build(self):
        self.header = BridgeHeader(self, self.fonts)
        self.header.pack(fill="x", padx=SP["lg"], pady=(SP["md"], 0))

        self.chooser = self._build_chooser()

        body = ttk.Frame(self)
        self.body = body

        rail_wrap = ttk.Frame(body, style="Rail.TFrame", width=206)
        rail_wrap.pack(side="left", fill="y")
        rail_wrap.pack_propagate(False)
        self.rail = StageRail(rail_wrap, STAGES, self._goto, self.fonts)
        self.rail.pack(fill="both", expand=True, pady=SP["sm"])

        self.stack = ttk.Frame(body)
        self.stack.pack(side="left", fill="both", expand=True, padx=(SP["lg"], 0))

        self.stages = [
            ConnectStage(self.stack, self),
            TablesStage(self.stack, self),
            PreflightStage(self.stack, self),
            TransferStage(self.stack, self),
            VerifyStage(self.stack, self),
            CutoverStage(self.stack, self),
        ]

        # status strip + log drawer
        strip = ttk.Frame(self)
        strip.pack(side="bottom", fill="x", padx=SP["lg"], pady=(SP["sm"], SP["sm"]))
        self.log_toggle = Button(strip, "Activity log", self._toggle_log,
                                 variant="quiet", font=self.fonts["small"],
                                 height=26, width=104)
        self.log_toggle.pack(side="right")
        self.switch_btn = Button(strip, "Switch activity", self.change_activity,
                                 variant="quiet", font=self.fonts["small"],
                                 height=26, width=112)
        self.switch_btn.pack(side="right", padx=(0, SP["sm"]))
        Button(strip, "Jobs", self.open_jobs, variant="quiet",
               font=self.fonts["small"], height=26,
               width=64).pack(side="right", padx=(0, SP["sm"]))
        Button(strip, "Batch", self.open_batch, variant="quiet",
               font=self.fonts["small"], height=26,
               width=64).pack(side="right", padx=(0, SP["sm"]))
        Button(strip, "Test Connection", self.test_connections_front, variant="quiet",
               font=self.fonts["small"], height=26,
               width=120).pack(side="right", padx=(0, SP["sm"]))
        self.activity_label = ttk.Label(strip, text="", style="Data.TLabel")
        self.activity_label.pack(side="left", padx=(0, SP["sm"]))
        self.status = ttk.Label(strip, text="Ready.", style="Muted.TLabel", width=24)
        self.status.pack(side="left")
        self.busy_meter = Meter(strip, height=4)
        self.busy_meter.pack(side="left", fill="x", expand=True,
                             padx=SP["md"], pady=(7, 0))

        self.log_wrap = ttk.Frame(self, style="Panel.TFrame")
        self.log = LogView(self.log_wrap, self.fonts, height=6)
        self.log.pack(fill="both", expand=True)
        self._log_open = False

        self.jobs_screen = JobsScreen(self, self)
        self.batch_screen = BatchScreen(self, self)

        self.chooser.pack(fill="both", expand=True,
                          padx=SP["lg"], pady=(SP["sm"], 0))
        self.switch_btn.set_enabled(False)
        self.say("Choose an activity to begin.", "info")
        self._announce_checkpoint()

    # ------------------------------------------------------------------ screens UI
    def open_jobs(self):
        """The history is readable at any time — including mid-run, since a job
        is written as it happens rather than at the end."""
        self.sync_job()
        for frame in (self.chooser, self.body, self.batch_screen):
            frame.pack_forget()
        self.jobs_screen.pack(fill="both", expand=True,
                              padx=SP["lg"], pady=(SP["sm"], 0))
        self._repack_log()
        self.jobs_screen.reload()

    def close_jobs(self):
        self.jobs_screen.pack_forget()
        target = self.body if self.activity_label.cget("text") else self.chooser
        target.pack(fill="both", expand=True, padx=SP["lg"], pady=(SP["sm"], 0))
        self._repack_log()

    def open_batch(self):
        self.sync_job()
        for frame in (self.chooser, self.body, self.jobs_screen):
            frame.pack_forget()
        self.batch_screen.pack(fill="both", expand=True,
                               padx=SP["lg"], pady=(SP["sm"], 0))
        self._repack_log()
        self.batch_screen.reload()

    def close_batch(self):
        self.batch_screen.pack_forget()
        target = self.body if self.activity_label.cget("text") else self.chooser
        target.pack(fill="both", expand=True, padx=SP["lg"], pady=(SP["sm"], 0))
        self._repack_log()

    def test_connections_front(self):
        """Choose credentials before running a workspace connection test."""
        if self.operation_active():
            messagebox.showwarning("Operation running", "Wait before changing connection settings.")
            return
        window = tk.Toplevel(self)
        window.title("Test connections · Choose credentials")
        window.configure(background=C["bg"])
        window.geometry("1120x800")
        window.minsize(1000, 740)
        window.transient(self)
        editor = WorkspaceConnections(window, self)
        editor.pack(fill="both", expand=True, padx=SP["lg"], pady=SP["lg"])
        window.grab_set()
        return editor

    def _test_connections_configured(self):
        """Test reachability and authentication of both PostgreSQL and SQL Server
        directly from the front screen or status bar without starting a migration."""
        if not self.stages[0]._commit():
            return

        def work():
            return (self.conns.probe_source(), self.conns.probe_target())

        def done(payload):
            src, tgt = payload
            self.versions["source"] = src["version"]
            self.versions["target"] = tgt["version"]
            self.refresh_endpoints()
            self.stages[0].next_btn.set_enabled(True)
            self.stages[0].notes.configure(
                text=f"{len(src['databases'])} databases visible on "
                     f"{self.pg.host}, {len(tgt['databases'])} on "
                     f"{self.ms.server}. Pick the pair on the next stage; "
                     "snapshot isolation and collation are reported by "
                     "preflight, once a target database exists to check.")
            self.rail.set_state(0, "done")
            msg = (f"Both on-prem servers reachable. PostgreSQL ({len(src['databases'])} DBs): {src['version'][:45]} | "
                   f"SQL Server ({len(tgt['databases'])} DBs): {tgt['version'][:45]}.")
            self.say(msg, "ok")
            if hasattr(self, "front_conn_status"):
                self.front_conn_status.configure(
                    text=f"✓ Connected: PostgreSQL ({self.pg.host}:{self.pg.port}, {len(src['databases'])} DBs) & "
                         f"SQL Server ({self.ms.server}, {len(tgt['databases'])} DBs)",
                    foreground=C["ok"]
                )
            messagebox.showinfo(
                "Connection Test Succeeded",
                f"✓ PostgreSQL on {self.pg.host}:{self.pg.port}\n"
                f"  Version: {src['version'][:70]}\n"
                f"  Databases found: {len(src['databases'])}\n\n"
                f"✓ SQL Server on {self.ms.server}\n"
                f"  Version: {tgt['version'][:70]}\n"
                f"  Databases found: {len(tgt['databases'])}\n\n"
                "Both on-premise servers are reachable and authenticated."
            )

        def failed(exc):
            self.say(f"Connection test failed: {exc}", "stop")
            if hasattr(self, "front_conn_status"):
                self.front_conn_status.configure(
                    text=f"✕ Connection failed: {exc}",
                    foreground=C["stop"]
                )
            messagebox.showerror(
                "Connection Test Failed",
                f"Could not connect to one or both servers:\n\n{exc}\n\n"
                "Click 'Configure Servers' to check host, port, user, or password credentials."
            )

        self.run_async(work, on_done=done, on_error=failed, message="Testing on-premise server connections")

    def open_server_settings(self):
        """Open Connect stage directly to configure servers without starting a transfer run."""
        self.set_activity("migrate")
        self._goto(0)
        self.say("Server Settings: review or edit on-premise connection parameters.", "info")

    def _announce_checkpoint(self):
        """An interrupted run should be the first thing you hear about, not
        something you find out by reloading rows you already moved."""
        path = os.path.join(self.options.output_dir, "checkpoint.json")
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        tables = saved.get("tables") or {}
        unfinished = [k for k, v in tables.items() if v.get("status") != "done"]
        if not tables:
            return
        pair = " \u2192 ".join(saved.get("pair") or ["?", "?"])
        self.say(f"Unfinished transfer found in {path}: {pair}, "
                 f"{len(tables) - len(unfinished)} tables done"
                 + (f", {len(unfinished)} interrupted" if unfinished else "")
                 + f", last written {saved.get('updated', '?')[:19]}. "
                 "Choose Migrate and reconnect to that pair to resume it.",
                 "warn")

    def _build_chooser(self) -> ttk.Frame:
        """First screen: migration and verification are different jobs and take
        different routes through the app."""
        frame = ttk.Frame(self)
        block = section(frame, "Migration workspace", self.fonts,
                        "PostgreSQL to SQL Server. Plan the move, validate the result, "
                        "and prepare your application for cutover.")
        block.pack(fill="both", expand=True)
        scroller = ScrollArea(block.body)
        scroller.pack(fill="both", expand=True)
        cards = scroller.body

        hero = ttk.Frame(cards, style="Panel.TFrame", padding=(24, 20))
        hero.pack(fill="x", pady=(0, 18))
        ttk.Label(hero, text="P G B R I D G E   /   MIGRATION CONTROL", style="PanelMuted.TLabel",
                  font=self.fonts["badge"]).pack(anchor="w")
        ttk.Label(hero, text="A deliberate path to your next database.",
                  background=C["panel"], foreground=C["text"],
                  font=self.fonts["display"]).pack(anchor="w", pady=(8, 6))
        ttk.Label(hero, text="01  Plan & assess     /     02  Transfer & reconcile     /     03  Prepare cutover",
                  style="PanelMuted.TLabel").pack(anchor="w")

        workflows = ttk.Frame(cards)
        workflows.pack(fill="x", pady=(0, 18))
        descriptions = {
            "migrate": "Choose tables, resolve compatibility findings, and transfer data with a recorded execution plan.",
            "verify": "Compare source and target independently. Inspect mismatches and export the evidence.",
            "cutover": "Patch Django settings or prepare a configuration handoff for another application framework.",
        }
        for n, (key, (title, blurb, stages)) in enumerate(ACTIVITIES.items()):
            workflows.columnconfigure(n, weight=1, uniform="workflow")
            card = ttk.Frame(workflows, style="Panel.TFrame", padding=20)
            card.grid(row=0, column=n, sticky="nsew", padx=(0 if n == 0 else 7, 0 if n == 2 else 7))
            tone = C["source"] if key == "migrate" else C["target_light"]
            ttk.Label(card, text=f"0{n + 1}  /  {('DATA MOVEMENT', 'QUALITY ASSURANCE', 'APPLICATION HANDOFF')[n]}",
                      background=C["panel"], foreground=tone,
                      font=self.fonts["badge"]).pack(anchor="w")
            ttk.Label(card, text=title, background=C["panel"], foreground=C["text"],
                      font=self.fonts["display"]).pack(anchor="w", pady=(14, 10))
            description = ttk.Label(card, text=descriptions[key], background=C["panel"],
                                    foreground=C["muted"], font=self.fonts["body"],
                                    wraplength=260, justify="left")
            description.pack(fill="x", anchor="w")
            card.bind("<Configure>", lambda e, label=description: label.configure(wraplength=max(120, e.width - 40)))
            Button(card, START_LABEL[key], lambda k=key: self.set_activity(k),
                   variant="primary" if n == 0 else "ghost", font=self.fonts["body"],
                   background=C["panel"]).pack(side="bottom", anchor="w", pady=(22, 0))

        # Tools & Server Health Card
        history = ttk.Frame(cards, style="Panel.TFrame", padding=(SP["lg"], SP["md"]))
        history.pack(fill="x", pady=(0, SP["sm"]))

        hh = ttk.Frame(history, style="Panel.TFrame")
        hh.pack(fill="x", pady=(0, 4))
        ttk.Label(hh, text="Workspace tools", background=C["panel"],
                  foreground=C["text"], font=self.fonts["title"]).pack(side="left")
        Pill(hh, "CONNECTIVITY", tone="muted", font=self.fonts["badge"],
             background=C["panel"]).pack(side="right")

        ttk.Label(history, text="Test PostgreSQL and SQL Server reachability, configure credentials, or orchestrate multi-database queues.",
                  background=C["panel"], foreground=C["muted"],
                  font=self.fonts["body"], wraplength=760,
                  justify="left").pack(anchor="w", pady=(2, SP["md"]))

        h_row = ttk.Frame(history, style="Panel.TFrame")
        h_row.pack(anchor="w")
        Button(h_row, "Test Both Connections", self.test_connections_front, variant="primary",
                font=self.fonts["body"], background=C["panel"]).pack(side="left")
        Button(h_row, "Configure Servers", self.open_server_settings, variant="ghost",
               icon="⚙", font=self.fonts["body"], background=C["panel"]).pack(side="left", padx=(SP["md"], 0))
        Button(h_row, "Batch Migration", self.open_batch, variant="ghost",
               icon="▤", font=self.fonts["body"], background=C["panel"]).pack(side="left", padx=(SP["md"], 0))
        Button(h_row, "Open job history", self.open_jobs, variant="quiet",
                font=self.fonts["body"], background=C["panel"]).pack(side="left", padx=(SP["md"], 0))

        self.front_conn_status = ttk.Label(
            history, text="Click 'Test Both Connections' to verify PostgreSQL and SQL Server reachability.",
            background=C["panel"], foreground=C["muted"], font=self.fonts["small"]
        )
        self.front_conn_status.pack(anchor="w", pady=(SP["sm"], 0))
        return frame

    def set_activity(self, key: str):
        if self.operation_active():
            messagebox.showwarning("Operation running", "Finish or stop the current operation first.")
            return
        title, _blurb, stages = ACTIVITIES[key]
        # A new activity is a new run. Without this the verification screen
        # still holds the migration's tables, findings and verdict, and reads
        # as though it had already been run.
        self.finish_job("abandoned")
        self.reset_run(keep_connection=True)
        self.activity = key
        self.start_job(key)
        self.chooser.pack_forget()
        self.body.pack(fill="both", expand=True, padx=SP["lg"], pady=(SP["sm"], 0))
        self.switch_btn.set_enabled(True)
        self.rail.show(stages)
        self._repack_log()
        self.activity_label.configure(text=f"{title}  ·")
        self.say(f"{title} selected. Stages: "
                 + " \u2192 ".join(STAGES[i][0] for i in stages), "step")
        self._goto(0)

    def reset_run(self, keep_connection: bool = True):
        """Clear everything that belongs to one run, leaving the servers alone.

        Stage widgets hold their own copies of the last run, so each is asked
        to clear itself rather than being trusted to notice.
        """
        self._preflight_signature = None
        self.tables = []
        self.issues = []
        self.transfer_summary = {}
        self.verify_report = None
        self.loaded_pair = None
        self.pg.dbname = ""
        self.ms.database = ""
        if self.conns is not None:
            self.conns.pg.dbname = ""
            self.conns.ms.database = ""
        self.options.mode = "both"
        if not keep_connection:
            self.versions = {"source": "", "target": ""}
            self.conns = None
        for stage in self.stages:
            if hasattr(stage, "reset"):
                stage.reset()
        for index in (1, 4):
            self.stages[index].picker.sync()
        for index in range(1, len(STAGES)):
            self.rail.set_state(index, "idle")
        self.refresh_endpoints()

    def change_activity(self):
        if self.operation_active():
            messagebox.showinfo("Transfer running",
                                "Stop the transfer before switching activity.")
            return
        self.finish_job("abandoned")
        self.reset_run(keep_connection=True)
        self.body.pack_forget()
        self.chooser.pack(fill="both", expand=True,
                          padx=SP["lg"], pady=(SP["sm"], 0))
        self.activity_label.configure(text="")
        self.switch_btn.set_enabled(False)
        self.say("Pick an activity.", "info")

    def _toggle_log(self):
        if self._log_open:
            self.log_wrap.pack_forget()
        else:
            # The content frame is packed with expand=True and claims every
            # spare pixel, so the drawer has to take its place in the packing
            # order ahead of it, not after.
            content = next((f for f in (self.body, self.jobs_screen, self.batch_screen, self.chooser)
                            if f.winfo_manager()), self.chooser)
            self.log_wrap.pack(side="bottom", fill="x", padx=SP["lg"],
                               pady=(SP["sm"], 0), before=content)
        self._log_open = not self._log_open
        self.log_toggle.set_text("Hide log" if self._log_open else "Activity log")

    def _repack_log(self):
        if not hasattr(self, "jobs_screen"):
            return
        """Swapping the content frame invalidates the drawer's place in the
        packing order, so put it back in front of whatever shows now."""
        if self._log_open:
            self._log_open = False
            self._toggle_log()

    def _goto(self, index: int):
        if self.operation_active():
            return
        for i, stage in enumerate(self.stages):
            stage.pack_forget() if i != index else stage.pack(
                fill="both", expand=True)
        self.rail.select(index)
        self.stages[index].on_show()

    def unlock(self, index: int):
        self.rail.unlock(index)

    def refresh_endpoints(self):
        """Header follows the current server + database, wherever it was set."""
        self.header.set_endpoint(
            "source", f"{self.pg.dbname or 'no database'} on {self.pg.host}",
            self.versions["source"][:40], bool(self.versions["source"]))
        self.header.set_endpoint(
            "target", f"{self.ms.database or 'no database'} on {self.ms.server}",
            self.versions["target"][:40], bool(self.versions["target"]))

    def operation_active(self):
        batch = getattr(getattr(self, "batch_screen", None), "migrator", None)
        return self._busy or self.transport is not None or (batch is not None and
            any(item.status == "running" for item in batch.items))

    def plan_signature(self):
        return (self.pg.host, self.pg.port, self.pg.dbname, self.pg.schema,
                self.ms.server, self.ms.database, self.ms.schema, self.options.mode,
                tuple((t.name, t.selected, t.plan, tuple(t.migrating_columns), t.row_filter)
                      for t in self.tables))

    def invalidate_plan(self):
        self._preflight_signature = None
        self.issues = []
        self.transfer_summary = {}
        self.verify_report = None
        for i in (2, 3, 4):
            self.stages[i].reset()
        self.stages[2]._completed = False

    def set_databases(self, source: str, target: str):
        """Point both configs at a database pair. Schemas already read for a
        different pair are dropped rather than silently reused."""
        if (source, target) != (self.pg.dbname, self.ms.database):
            if self.operation_active():
                messagebox.showwarning("Operation running", "Wait for the current operation before changing databases.")
                return
            self.invalidate_plan()
            self.pg.dbname, self.ms.database = source, target
            self.tables = []
            self.loaded_pair = None
            # Results belong to the pair they came from; exporting them beside
            # another pair's would be a lie.
            self.transfer_summary = {}
            self.verify_report = None
        self.refresh_endpoints()

    # ------------------------------------------------------------------ helpers
    def say(self, message: str, level: str = "info"):
        """One line to the status strip, the log drawer, and the log file.

        The file matters: it is the only part of the record that survives the
        process being killed mid-transfer.
        """
        summary = message if len(message) <= 24 else message[:21] + "…"
        self.status.configure(text=summary)
        now = datetime.datetime.now()
        self.log.write(message, level, now.strftime("%H:%M:%S"))
        if self.job is not None:
            self.job.note(level, message, now.isoformat(timespec="seconds"))
        try:
            os.makedirs(self.options.output_dir, exist_ok=True)
            with open(self._log_path(now), "a", encoding="utf-8") as fh:
                fh.write(f"{now:%Y-%m-%d %H:%M:%S}  {level:<5}  {message}\n")
        except OSError:
            pass                     # never let logging break the run

    def _log_path(self, when: datetime.datetime) -> str:
        return os.path.join(self.options.output_dir,
                            f"{self.activity}_{when:%Y%m%d}.log")

    def busy(self, on: bool, message: str = ""):
        self._busy = on
        if on:
            self.busy_meter.start()
            if message:
                self.status.configure(text=message)
            self.configure(cursor="watch")
        else:
            self.busy_meter.stop()
            self.busy_meter.set(0)
            self.configure(cursor="")

    def run_async(self, work, on_done=None, on_error=None, message: str = "Working"):
        """Run blocking database work off the UI thread."""
        if self.operation_active():
            messagebox.showinfo("Busy", "Another operation is still running.")
            return
        self.busy(True, message)
        result: dict = {}

        def wrapper():
            try:
                result["value"] = work()
            except Exception as exc:                       # noqa: BLE001
                result["error"] = exc
                result["trace"] = traceback.format_exc()
            self.after(0, finish)

        def finish():
            self.busy(False)
            if "error" in result:
                self.say(str(result["error"]), "error")
                self.log.write(result.get("trace", ""), "error")
                if on_error:
                    on_error(result["error"])
                else:
                    messagebox.showerror("Failed", str(result["error"]))
            elif on_done:
                on_done(result.get("value"))

        threading.Thread(target=wrapper, daemon=True).start()

    # ------------------------------------------------------------------ jobs
    def start_job(self, activity: str):
        """Open a new job. Everything said or decided from here belongs to it."""
        self.finish_job("abandoned")
        self.jobs = JobStore(self.options.output_dir)
        self.job = Job(id=new_job_id(), activity=activity,
                       started=datetime.datetime.now().isoformat(timespec="seconds"))
        self.sync_job()

    def sync_job(self):
        """Copy the current run state onto the open job and write it out."""
        if self.job is None:
            return
        self.job.source = {"host": self.pg.host, "database": self.pg.dbname,
                           "schema": self.pg.schema}
        self.job.target = {"server": self.ms.server, "database": self.ms.database,
                           "schema": self.ms.schema}
        self.job.mode = self.options.mode
        self.job.tables = [
            {"name": t.name, "selected": t.selected, "plan": t.plan,
             "source_rows": t.source_rows, "target_rows": t.target_rows,
             "row_filter": t.row_filter,
             "excluded_columns": list(t.exclude_columns),
             "why": list(t.exclusion_notes)}
            for t in self.tables]
        self.job.preflight = [asdict(i) for i in self.issues]
        self.job.transfer = self.transfer_summary
        self.job.verification = self.verify_report
        try:
            self.jobs.save(self.job)
        except OSError as exc:
            self.log.write(f"Could not write the job file: {exc}", "warn")

    def finish_job(self, status: str):
        if self.job is None:
            return
        if self.job.status == "running":
            self.job.finished = datetime.datetime.now().isoformat(timespec="seconds")
            self.job.status = status
        self.sync_job()
        self.job = None

    # ------------------------------------------------------------------ profiles
    def _load_profile(self):
        if not os.path.exists(PROFILE_PATH):
            return
        try:
            with open(PROFILE_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if "profiles" in data and isinstance(data["profiles"], dict):
            active = data.get("active_profile", "Default")
            prof_data = data["profiles"].get(active) or next(iter(data["profiles"].values()), {})
            self.stages[0].apply_profile(prof_data)
            if hasattr(self.stages[0], "set_profile_names"):
                self.stages[0].set_profile_names(list(data["profiles"].keys()), active)
        else:
            self.stages[0].apply_profile(data)
            if hasattr(self.stages[0], "set_profile_names"):
                self.stages[0].set_profile_names(["Default"], "Default")
        self.say("Loaded saved connection profile.", "info")

    def save_profile(self, include_secrets: bool, profile_name: str = "Default"):
        os.makedirs(PROFILE_DIR, exist_ok=True)
        pg, ms = asdict(self.pg), asdict(self.ms)
        if not include_secrets:
            pg["password"] = ms["password"] = ""

        all_data = {}
        if os.path.exists(PROFILE_PATH):
            try:
                with open(PROFILE_PATH, encoding="utf-8") as fh:
                    all_data = json.load(fh)
            except Exception:
                all_data = {}

        profiles = all_data.get("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}

        current_payload = {"pg": pg, "ms": ms, "options": asdict(self.options)}
        profiles[profile_name] = current_payload

        save_dict = {
            "active_profile": profile_name,
            "profiles": profiles,
            "pg": pg,
            "ms": ms,
            "options": asdict(self.options)
        }
        with open(PROFILE_PATH, "w", encoding="utf-8") as fh:
            json.dump(save_dict, fh, indent=2)
        _restrict(PROFILE_PATH)
        if hasattr(self.stages[0], "set_profile_names"):
            self.stages[0].set_profile_names(list(profiles.keys()), profile_name)
        self.say(f"Profile '{profile_name}' saved to {PROFILE_PATH}", "ok")

    def list_profiles(self) -> list[str]:
        if not os.path.exists(PROFILE_PATH):
            return ["Default"]
        try:
            with open(PROFILE_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            if "profiles" in data and isinstance(data["profiles"], dict):
                return list(data["profiles"].keys()) or ["Default"]
        except Exception:
            pass
        return ["Default"]

    def load_named_profile(self, profile_name: str):
        if not os.path.exists(PROFILE_PATH):
            return
        try:
            with open(PROFILE_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        profiles = data.get("profiles", {})
        if profile_name in profiles:
            self.stages[0].apply_profile(profiles[profile_name])
            data["active_profile"] = profile_name
            try:
                with open(PROFILE_PATH, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
            except Exception:
                pass
            self.say(f"Switched to profile '{profile_name}'.", "ok")

    # ------------------------------------------------------------------ pump
    def _pump(self):
        # Held locally: handling the "finished" event clears self.transport, and
        # the drain must keep reading the queue it started on.
        transport = self.transport
        if transport is not None:
            drained = 0
            try:
                while drained < 400:
                    event = transport.events.get_nowait()
                    self.stages[3].handle(event)
                    drained += 1
            except queue.Empty:
                pass
        # Drop any earlier booking first: a pump call from outside the
        # scheduler would otherwise leave one behind to fire after destroy().
        if self._pump_job is not None:
            self.after_cancel(self._pump_job)
        self._pump_job = self.after(60, self._pump)

    def destroy(self):
        # The pump reschedules itself; left running it fires against a dead
        # interpreter and spits Tcl errors over the shutdown.
        if self._pump_job is not None:
            self.after_cancel(self._pump_job)
            self._pump_job = None
        super().destroy()

    def _on_close(self):
        if getattr(self, "_closing", False):
            return
        if self.operation_active():
            if not messagebox.askyesno("Operation running",
                    "Stop and close after the active operation finishes its cleanup?"):
                return
            self._closing = True
            if self.transport:
                self.transport.stop()
            batch = getattr(self.batch_screen, "migrator", None)
            if batch:
                batch.stop()
            self.say("Waiting for active work and database cleanup before closing.", "warn")
            self.after(100, self._finish_close)
            return
        self.finish_job("abandoned")
        self.destroy()

    def _finish_close(self):
        if self.operation_active():
            self.after(100, self._finish_close)
            return
        self.finish_job("abandoned")
        self.destroy()


# ===========================================================================
class JobsScreen(ttk.Frame):
    """Every past run, and what it did.

    Sits outside the stage flow: a job is a record, not a step. Reads the same
    jobs.json the app writes as it runs, so a job left half-finished by a crash
    is here too, with its log up to the moment it stopped.
    """

    COLUMNS = [("started", "Started", 150), ("activity", "Activity", 90),
               ("pair", "Source → target", 250), ("status", "Result", 100),
               ("rows", "Rows", 110), ("tables", "Tables", 90)]

    def __init__(self, master, app: App):
        super().__init__(master)
        self.app = app
        self.rows: list[dict] = []

        block = section(self, "Job history", app.fonts,
                        "Every migration and verification this machine has run, "
                        "newest first. Select one to read what it did.")
        block.pack(fill="both", expand=True)

        foot = ttk.Frame(block.body)
        foot.pack(side="bottom", fill="x", pady=(SP["md"], 0))

        bar = ttk.Frame(block.body)
        bar.pack(fill="x", pady=(0, SP["md"]))
        Button(bar, "Refresh", self.reload, variant="quiet", icon="↻",
               font=app.fonts["small"], height=28).pack(side="left")
        self.count = ttk.Label(bar, text="", style="Muted.TLabel")
        self.count.pack(side="left", padx=(SP["md"], 0))
        Button(bar, "Save text report", self._export, variant="ghost", icon="↗",
               font=app.fonts["small"], height=28).pack(side="right")

        split = ttk.PanedWindow(block.body, orient="vertical")
        split.pack(fill="both", expand=True)

        top = ttk.Frame(split)
        # An explicit row count: a Treeview asks for as many rows as it holds,
        # and a paned window will not shrink a pane below what it asks for, so
        # a long history squeezed the report pane to nothing.
        self.tree = ttk.Treeview(top, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="browse", height=6)
        for key, title, width in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="e" if key in
                             ("rows", "tables") else "w",
                             stretch=(key == "pair"))
        sb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("passed", foreground=C["ok"])
        self.tree.tag_configure("failed", foreground=C["stop"])
        self.tree.tag_configure("running", foreground=C["warn"])
        self.tree.tag_configure("abandoned", foreground=C["faint"])
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show())
        self.split = split
        self._sash_placed = False
        # Resizing the window moves the panes but not the sash, so shrinking
        # eventually squeezes one of them to nothing. Re-clamp on every change,
        # and only when a pane has actually gone unusable, so a sash the reader
        # dragged deliberately is left where they put it.
        split.bind("<Configure>", lambda _e: self.clamp_sash())
        split.add(top, weight=1)

        bottom = ttk.Frame(split, style="Panel.TFrame", padding=SP["sm"])
        # An explicit height, and a sash placed on show: without them the list
        # takes the whole pane on a short window and the report — the reason
        # this screen exists — collapses to a scrollbar.
        self.detail = tk.Text(bottom, wrap="none", bd=0, height=8,
                              background=C["panel"], foreground=C["text"],
                              font=app.fonts["data_sm"], padx=0, pady=6,
                              highlightthickness=0, state="disabled")
        dsb = ttk.Scrollbar(bottom, orient="vertical", command=self.detail.yview)
        dhb = ttk.Scrollbar(bottom, orient="horizontal", command=self.detail.xview)
        self.detail.configure(yscrollcommand=dsb.set, xscrollcommand=dhb.set)
        dhb.pack(side="bottom", fill="x")
        self.detail.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")
        split.add(bottom, weight=2)

        Button(foot, "Back", app.close_jobs, variant="quiet",
               font=app.fonts["body"]).pack(side="left")
        self.hint = ttk.Label(foot, text="", style="Muted.TLabel")
        self.hint.pack(side="left", padx=(SP["md"], 0))

    MIN_PANE = 70

    def place_sash(self):
        """Split the screen between the list and the report, once. After that
        the sash belongs to whoever drags it."""
        if self._sash_placed:
            return
        height = self.split.winfo_height()
        if height < 160:
            self.after(60, self.place_sash)
            return
        self.split.sashpos(0, self._safe(int(height * 0.42)))
        self._sash_placed = True

    def _safe(self, position: int) -> int:
        height = self.split.winfo_height()
        return max(min(position, height - self.MIN_PANE), self.MIN_PANE)

    def clamp_sash(self):
        height = self.split.winfo_height()
        if height < 40:
            return
        try:
            current = self.split.sashpos(0)
        except tk.TclError:
            return
        if height < 2 * self.MIN_PANE:
            # Not enough room for both minimums — with the log drawer open on a
            # short window there rarely is. Halve it rather than starve one pane
            # down to a scrollbar.
            safe = height // 2
        else:
            safe = self._safe(current)
        if safe != current:
            self.split.sashpos(0, safe)

    def reload(self):
        self.after(60, self.place_sash)
        try:
            self.rows = self.app.jobs.list()
        except OSError as exc:
            self.rows = []
            self.app.say(f"Could not read the job history: {exc}", "warn")
        self.tree.delete(*self.tree.get_children())
        for i, job in enumerate(self.rows):
            src = (job.get("source") or {}).get("database") or "?"
            tgt = (job.get("target") or {}).get("database") or "?"
            transfer = job.get("transfer") or {}
            verify = job.get("verification") or {}
            rows = transfer.get("rows")
            if rows is None:
                rows = verify.get("rows_verified", 0)
            self.tree.insert(
                "", "end", iid=str(i),
                values=(job.get("started", "")[:19].replace("T", " "),
                        job.get("activity", "?"),
                        f"{src} → {tgt}",
                        job.get("status", "?"),
                        f"{rows:,}" if isinstance(rows, int) else "—",
                        f"{transfer.get('tables_ok', '')}"
                        f"{'/' if transfer.get('tables_total') else ''}"
                        f"{transfer.get('tables_total', '')}" or "—"),
                tags=(job.get("status", ""),))
        self.count.configure(
            text=f"{len(self.rows)} job(s) in {self.app.jobs.path}")
        if self.rows:
            self.tree.selection_set("0")
            self._show()
        else:
            self._render_text("No jobs recorded yet. Run a migration or a "
                              "verification and it will appear here.")

    def _selected(self) -> dict | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.rows[int(selection[0])]

    def _show(self):
        job = self._selected()
        if job is None:
            return
        self._render_text(job_text_report(job))
        self.hint.configure(text=f"job {job.get('id', '?')}")

    def _render_text(self, text: str):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.see("1.0")
        self.detail.configure(state="disabled")

    def _export(self):
        job = self._selected()
        if job is None:
            messagebox.showinfo("No job selected", "Select a job first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Text report", "*.txt")],
            initialfile=f"pgbridge_{job.get('activity', 'job')}_"
                        f"{job.get('id', 'report')}.txt")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(job_text_report(job))
        except OSError as exc:
            messagebox.showerror("Could not write the report", str(exc))
            return
        self.app.say(f"Job report written to {path}", "ok")


# ===========================================================================
class BatchScreen(ttk.Frame):
    """Multi-database migration orchestrator for on-premise servers."""

    def __init__(self, master, app: App):
        super().__init__(master)
        self.app = app
        self.available_dbs: list[str] = []
        self.selected_dbs: set[str] = set()
        self.migrator: BatchMigrator | None = None
        self._pump_id = None

        block = section(self, "Multi-Database Batch Migration", app.fonts,
                        "Migrate multiple on-premise PostgreSQL databases to Microsoft SQL Server in a coordinated queue.")
        block.pack(fill="both", expand=True)

        foot = ttk.Frame(block.body)
        foot.pack(side="bottom", fill="x", pady=(SP["md"], 0))

        bar = ttk.Frame(block.body)
        bar.pack(fill="x", pady=(0, SP["sm"]))

        Button(bar, "Scan Source Databases", self.scan_databases, variant="primary",
               icon="🔍", font=app.fonts["small"], height=28).pack(side="left")
        Button(bar, "Select All", lambda: self._toggle_all(True), variant="quiet",
               font=app.fonts["small"], height=28).pack(side="left", padx=(SP["sm"], 0))
        Button(bar, "Select None", lambda: self._toggle_all(False), variant="quiet",
               font=app.fonts["small"], height=28).pack(side="left", padx=(SP["sm"], 0))

        self.naming_rule = Field(bar, "Target database naming", app.fonts, kind="combo",
                                 style_prefix="Root", width=22,
                                 values=["Same name as source", "Prefix: MSSQL_", "Suffix: _mssql"])
        self.naming_rule.set("Same name as source")
        self.naming_rule.pack(side="left", padx=(SP["lg"], 0))

        self.continue_on_err = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Continue queue on database error", variable=self.continue_on_err).pack(
            side="left", padx=(SP["md"], 0), pady=(18, 0))

        exec_bar = ttk.Frame(block.body)
        exec_bar.pack(fill="x", pady=(0, SP["sm"]))

        self.start_btn = Button(exec_bar, "Start Batch Migration", self.start_batch,
                                variant="target", icon="▶", font=app.fonts["body"])
        self.start_btn.pack(side="left")

        self.stop_btn = Button(exec_bar, "Stop Batch", self.stop_batch, variant="danger",
                               icon="⏹", font=app.fonts["body"])
        self.stop_btn.pack(side="left", padx=(SP["sm"], 0))
        self.stop_btn.set_enabled(False)

        self.status_lbl = ttk.Label(exec_bar, text="", style="Data.TLabel")
        self.status_lbl.pack(side="right")

        self.overall_meter = Meter(block.body, height=8, tone="target")
        self.overall_meter.pack(fill="x", pady=(0, SP["sm"]))

        wrap = ttk.Frame(block.body)
        wrap.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(wrap, columns=("sel", "src_db", "tgt_db", "status", "tables", "rows", "time", "error"),
                                 show="headings", selectmode="browse")
        self.tree.heading("sel", text="✓", command=lambda: self._toggle_all())
        self.tree.heading("src_db", text="Source Database")
        self.tree.heading("tgt_db", text="Target Database")
        self.tree.heading("status", text="Queue Status")
        self.tree.heading("tables", text="Tables")
        self.tree.heading("rows", text="Rows Moved")
        self.tree.heading("time", text="Finished")
        self.tree.heading("error", text="Notes / Error")

        self.tree.column("sel", width=40, anchor="center")
        self.tree.column("src_db", width=200, anchor="w")
        self.tree.column("tgt_db", width=200, anchor="w")
        self.tree.column("status", width=120, anchor="center")
        self.tree.column("tables", width=90, anchor="e")
        self.tree.column("rows", width=110, anchor="e")
        self.tree.column("time", width=130, anchor="w")
        self.tree.column("error", width=250, anchor="w")

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree.tag_configure("done", foreground=C["ok"])
        self.tree.tag_configure("running", foreground=C["warn"])
        self.tree.tag_configure("failed", foreground=C["stop"])
        self.tree.tag_configure("pending", foreground=C["muted"])
        self.tree.bind("<Button-1>", self._click_tree)
        self.tree.bind("<space>", lambda _e: self._toggle_selected())

        Button(foot, "Back", app.close_batch, variant="quiet",
               font=app.fonts["body"]).pack(side="left")
        self.summary_lbl = ttk.Label(foot, text="", style="Muted.TLabel")
        self.summary_lbl.pack(side="left", padx=(SP["md"], 0))

    def reload(self):
        if self.app.conns and not self.available_dbs:
            self.scan_databases()

    def scan_databases(self):
        if self.app.conns is None:
            messagebox.showinfo("Not connected", "Test connections on the Connect stage first.")
            return

        def work():
            return self.app.conns.probe_source()["databases"]

        def done(dbs):
            filtered = [d for d in dbs if d not in ("template0", "template1")]
            self.available_dbs = filtered
            self.selected_dbs = set(filtered)
            self._render()
            self.app.say(f"Found {len(filtered)} PostgreSQL databases for batch migration.", "ok")

        self.app.run_async(work, on_done=done, message="Scanning source databases")

    def _target_name_for(self, src: str) -> str:
        rule = self.naming_rule.get()
        if "Prefix" in rule:
            return f"MSSQL_{src}"
        if "Suffix" in rule:
            return f"{src}_mssql"
        return src

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for d in self.available_dbs:
            sel = "✓" if d in self.selected_dbs else " "
            tgt = self._target_name_for(d)
            self.tree.insert("", "end", iid=d, values=(sel, d, tgt, "Pending", "—", "—", "—", ""),
                             tags=("pending",))
        self.summary_lbl.configure(text=f"{len(self.selected_dbs)} of {len(self.available_dbs)} databases selected for migration.")

    def _toggle_all(self, on: bool | None = None):
        if on is None:
            on = len(self.selected_dbs) < len(self.available_dbs)
        self.selected_dbs = set(self.available_dbs) if on else set()
        self._render()

    def _click_tree(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row in self.selected_dbs:
                self.selected_dbs.remove(row)
            else:
                self.selected_dbs.add(row)
            self._render()

    def _toggle_selected(self):
        sel = self.tree.selection()
        if sel:
            d = sel[0]
            if d in self.selected_dbs:
                self.selected_dbs.remove(d)
            else:
                self.selected_dbs.add(d)
            self._render()

    def start_batch(self):
        if not self.selected_dbs:
            messagebox.showwarning("No databases", "Select at least one database to migrate.")
            return
        if self.app.conns is None:
            messagebox.showinfo("Not connected", "Test connections on the Connect stage first.")
            return

        pairs = [(d, self._target_name_for(d)) for d in self.available_dbs if d in self.selected_dbs]
        if self.app.operation_active():
            messagebox.showwarning("Operation running", "Finish the current operation before starting a batch.")
            return
        opts = self.app.options
        description = (f"Start {len(pairs)} database(s), mode: {opts.mode}.\n"
                       f"Target server: {self.app.ms.server}; schema: {self.app.ms.schema}.\n"
                       + ("Dry run: no database writes.\n" if opts.dry_run else
                          "Existing selected target rows will be deleted before loading.\n" if opts.clear_target and opts.with_data else
                          "Existing target rows will not be cleared.\n")
                       + "\n" + "\n".join(f"{src} → {dst}" for src, dst in pairs))
        if not messagebox.askyesno("Confirm Batch", description):
            return

        self.start_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.overall_meter.set(0)

        self.migrator = BatchMigrator(
            self.app.conns, pairs, self.app.options,
            continue_on_error=self.continue_on_err.get()
        )
        self.migrator.start()
        self.app.say(f"Started batch migration of {len(pairs)} databases.", "info")
        self._pump_id = self.after(100, self._pump_batch)

    def stop_batch(self):
        if self.migrator:
            self.migrator.stop()
            self.stop_btn.set_enabled(False)

    def _pump_batch(self):
        if not self.migrator:
            return
        while not self.migrator.events.empty():
            evt = self.migrator.events.get_nowait()
            kind = evt.get("kind")
            if kind == "batch_start":
                self.status_lbl.configure(text=f"Batch started: {evt.get('total')} DBs")
            elif kind == "batch_db_start":
                src = evt.get("source")
                tgt = evt.get("target")
                idx = evt.get("index", 0)
                total = len(self.migrator.items)
                self.overall_meter.set(idx / max(total, 1))
                if self.tree.exists(src):
                    self.tree.item(src, values=("✓", src, tgt, "Running", "...", "...", "...", "In progress"),
                                   tags=("running",))
                self.status_lbl.configure(text=f"Migrating {src} ({idx+1}/{total})")
            elif kind == "batch_db_done":
                it = evt.get("item", {})
                src = it.get("source_db")
                tgt = it.get("target_db")
                st = it.get("status")
                tag = "done" if st == "done" else ("failed" if st == "failed" else "pending")
                if self.tree.exists(src):
                    self.tree.item(src, values=(
                        "✓", src, tgt, st.capitalize(),
                        f"{it.get('tables_done')}/{it.get('tables_total')}",
                        f"{it.get('rows_moved', 0):,}",
                        it.get("finished", "")[11:19],
                        it.get("error") or "Completed"
                    ), tags=(tag,))
            elif kind == "batch_finished":
                self.overall_meter.set(1.0)
                self.start_btn.set_enabled(True)
                self.stop_btn.set_enabled(False)
                rows = evt.get("total_rows", 0)
                self.status_lbl.configure(text=f"Batch finished: {rows:,} total rows moved.")
                self.app.say(f"Batch migration complete. {rows:,} total rows migrated.", "ok")
                self._pump_id = None
                return

        self._pump_id = self.after(100, self._pump_batch)


# ===========================================================================
class Stage(ttk.Frame):
    def __init__(self, master, app: App):
        super().__init__(master)
        self.app = app
        self.fonts = app.fonts
        self.build()

    def build(self):        # pragma: no cover
        raise NotImplementedError

    def on_show(self):
        pass

    def reset(self):
        """Drop whatever the previous run left on this screen."""


# ===========================================================================
class DatabasePicker(ttk.Frame):
    """Source and target database pickers.

    Databases are chosen where they are used, not at connect time: the schema
    view needs a pair to compare, and verification may be pointed at a pair
    nobody migrated in this session.
    """

    def __init__(self, master, app: App, action: str, on_action):
        super().__init__(master)
        self.app = app
        self.src = Field(self, "Source database", app.fonts, kind="combo", searchable=True,
                         style_prefix="Root", width=22)
        self.src.pack(side="left")
        self.tgt = Field(self, "Target database (blank = same name)", app.fonts,
                         kind="combo", searchable=True, style_prefix="Root", width=22)
        self.tgt.pack(side="left", padx=(SP["md"], 0))
        # Typeable, so a database that does not exist yet can be named.
        self.tgt.widget.configure(state="normal")
        for field in (self.src, self.tgt):
            field.widget.bind("<<ComboboxSelected>>", lambda _e: self._commit())
        holder = ttk.Frame(self)
        holder.pack(side="left", padx=(SP["md"], 0), pady=(19, 0))
        Button(holder, "List databases", self.reload, variant="quiet",
               font=app.fonts["small"], height=28).pack(side="left")
        self.action_btn = Button(holder, action, on_action, variant="ghost",
                                 font=app.fonts["small"], height=28)
        self.action_btn.pack(side="left", padx=(SP["sm"], 0))

    def _commit(self):
        self.app.set_databases(self.src.get(), self.tgt.get())

    def sync(self):
        """Show whatever the shared config currently holds."""
        for field, value in ((self.src, self.app.pg.dbname),
                             (self.tgt, self.app.ms.database)):
            if value and value not in field.widget.cget("values"):
                field.set_values([*field.widget.cget("values"), value])
            field.set(value)

    def reload(self):
        if self.app.conns is None:
            messagebox.showinfo("Not connected", "Test the connections first.")
            return
        conns = self.app.conns

        def work():
            return conns.probe_source()["databases"], conns.probe_target()["databases"]

        def done(payload):
            source, target = payload
            self.src.set_values(source)
            self.tgt.set_values(target)
            self.sync()
            self.app.say(f"{len(source)} source and {len(target)} target "
                         "databases listed.", "ok")

        self.app.run_async(work, on_done=done, message="Listing databases")

    def require(self) -> bool:
        """Source is mandatory; a blank target means "same name as the source",
        created on the server if it is not there yet."""
        self._commit()
        if not self.app.pg.dbname:
            messagebox.showwarning("Pick a database",
                                   "Choose the source database to migrate.")
            return False
        if not self.app.ms.database:
            self.app.set_databases(self.app.pg.dbname, self.app.pg.dbname)
            self.sync()
            self.app.say(f"No target database chosen; using "
                         f"{self.app.ms.database} on {self.app.ms.server}.", "info")
        return True

    def ensure_target(self, then):
        """Create the target database if the server does not have it, then run
        `then`. Creating a database is a server-level write, so it is asked for
        rather than assumed."""
        conns, name = self.app.conns, self.app.ms.database

        def check():
            return conns.target_database_exists(name)

        def done(exists):
            if exists:
                then()
                return
            if not messagebox.askyesno(
                    "Create database",
                    f"{name} does not exist on {self.app.ms.server}.\n\n"
                    f"Create it now?"):
                self.app.say(f"{name} was not created; nothing to migrate into.",
                             "warn")
                return
            self.app.run_async(
                lambda: conns.create_target_database(name),
                on_done=lambda _r: (self.app.say(f"Created database {name} on "
                                                 f"{self.app.ms.server}.", "ok"),
                                    self.sync(), then()),
                message=f"Creating {name}")

        self.app.run_async(check, on_done=done, message="Checking target database")


# ===========================================================================
class ConnectStage(Stage):
    def build(self):
        workspace = isinstance(self, WorkspaceConnections)
        block = section(self, "Connection workspace" if workspace else "Connect",
                        self.fonts,
                        "Choose a profile or enter credentials. Test and review both servers here." if workspace else
                        "Servers only. Databases are chosen at the stage that "
                        "uses them. Nothing is written until you start the "
                        "transfer.")
        block.pack(fill="both", expand=True)
        cols = block.body
        cols.columnconfigure(0, weight=1, uniform="c")
        cols.columnconfigure(1, weight=1, uniform="c")
        cols.rowconfigure(0, weight=1)

        self.pg_fields = self._panel(cols, 0, "Source", "PostgreSQL", C["source"], [
            ("host", "Host", "entry", None), ("port", "Port", "entry", None),
            ("user", "User", "entry", None), ("password", "Password", "entry", None),
            ("sslmode", "SSL mode", "combo",
             ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]),
            ("schema", "Schema", "entry", None),
        ])

        self.ms_fields = self._panel(cols, 1, "Target", "SQL Server", C["target"], [
            ("server", "Server", "entry", None),
            ("driver", "ODBC driver", "combo", self._drivers()),
            ("auth", "Authentication", "combo", ["sql", "windows"]),
            ("user", "Login", "entry", None), ("password", "Password", "entry", None),
            ("schema", "Schema", "entry", None),
            ("encrypt", "Encrypt connection", "combo", ["Yes", "No"]),
            ("trust_cert", "Trust server certificate", "combo", ["No", "Yes"]),
        ])
        if workspace:
            for panel in cols.grid_slaves(row=0):
                panel.grid_configure(row=1)
            cols.rowconfigure(0, weight=0)
            cols.rowconfigure(1, weight=1)

        self.pg_fields["host"].set("localhost")
        self.pg_fields["port"].set("5432")
        self.pg_fields["sslmode"].set("prefer")
        self.pg_fields["schema"].set("public")
        self.ms_fields["server"].set("localhost")
        self.ms_fields["auth"].set("sql")
        self.ms_fields["schema"].set("dbo")
        self.ms_fields["encrypt"].set("Yes")
        self.ms_fields["trust_cert"].set("No")
        drivers = self._drivers()
        if drivers:
            self.ms_fields["driver"].set(drivers[0])

        foot = ttk.Frame(block.body)
        foot.grid(row=3 if workspace else 1, column=0, columnspan=2, sticky="ew", pady=(SP["lg"], 0))

        prof_bar = ttk.Frame(block.body if workspace else foot)
        if workspace:
            prof_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, SP["md"]))
        else:
            prof_bar.pack(side="left", fill="x")

        ttk.Label(prof_bar, text="Profile:", font=self.fonts["small"]).pack(side="left", padx=(0, SP["xs"]))
        self.profile_var = tk.StringVar(value="Default")
        self.profile_combo = ttk.Combobox(prof_bar, textvariable=self.profile_var, state="readonly", width=14)
        self.profile_combo.pack(side="left", padx=(0, SP["xs"]))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)

        self.remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(prof_bar, text="Remember passwords",
                        variable=self.remember).pack(side="left", padx=(SP["xs"], SP["sm"]))
        Button(prof_bar, "Save", self._save_active_profile,
               variant="quiet", font=self.fonts["body"]).pack(side="left", padx=(0, SP["xs"]))
        Button(prof_bar, "Save As...", self._save_profile_as,
               variant="quiet", font=self.fonts["body"]).pack(side="left", padx=(0, SP["md"]))

        self.next_btn = Button(foot, "Continue", self._continue,
                               variant="primary", font=self.fonts["body"])
        self.next_btn.pack(side="right")
        self.next_btn.set_enabled(False)
        self.test_btn = Button(foot, "Test both connections", self._test,
                               variant="primary" if workspace else "ghost", font=self.fonts["body"])
        self.test_btn.pack(side="right", padx=(0, SP["sm"]))

        self.notes = ttk.Label(block.body, text="Self-signed SQL Server certificate: keep encryption enabled and select Trust server certificate = Yes. "
                               "This skips certificate validation.", style="Muted.TLabel",
                               wraplength=760, justify="left")
        self.notes.grid(row=4 if workspace else 2, column=0, columnspan=2, sticky="w",
                        pady=(SP["md"], 0))

    def _panel(self, parent, column, title, system, colour, spec):
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=SP["lg"])
        panel.grid(row=0, column=column, sticky="nsew",
                   padx=(0, SP["md"]) if column == 0 else (SP["md"], 0))

        head = ttk.Frame(panel, style="Panel.TFrame")
        head.pack(fill="x", pady=(0, SP["md"]))
        emblem = "🐘" if "Postgre" in system else "⊞"
        top_row = ttk.Frame(head, style="Panel.TFrame")
        top_row.pack(fill="x")
        ttk.Label(top_row, text=f"{title.upper()} SERVER", background=C["panel"], foreground=colour,
                  font=self.fonts["badge"]).pack(side="left")
        Pill(top_row, "ON-PREMISE", tone="muted", font=self.fonts["badge"],
             background=C["panel"]).pack(side="right")
        ttk.Label(head, text=f"{emblem}  {system}", background=C["panel"], foreground=C["text"],
                  font=self.fonts["title"]).pack(anchor="w", pady=(3, 0))

        fields: dict[str, Field] = {}
        grid = ttk.Frame(panel, style="Panel.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1, uniform="f")
        grid.columnconfigure(1, weight=1, uniform="f")
        for i, (key, label, kind, values) in enumerate(spec):
            field = Field(grid, label, self.fonts, kind=kind, values=values,
                          secret=(key == "password"), width=18)
            field.grid(row=i // 2, column=i % 2, sticky="ew",
                       padx=(0, SP["sm"]) if i % 2 == 0 else (SP["sm"], 0),
                       pady=(0, SP["sm"]))
            fields[key] = field
        return fields

    @staticmethod
    def _drivers() -> list[str]:
        try:
            import pyodbc
            found = [d for d in pyodbc.drivers() if "SQL Server" in d]
            return found or ["ODBC Driver 18 for SQL Server"]
        except ImportError:
            return ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]

    # -- actions -----------------------------------------------------------
    def _commit(self) -> bool:
        if self.app.operation_active():
            messagebox.showwarning("Operation running", "Wait before changing connection settings.")
            return False
        old_pg, old_ms = self.app.pg, self.app.ms
        f = self.pg_fields
        try:
            port = int(f["port"].get() or 5432)
            if not 1 <= port <= 65535:
                raise ValueError("Port out of range")
        except ValueError:
            messagebox.showerror("Port", "Port must be a number between 1 and 65535.", parent=self)
            return False
        self.app.pg = PgConfig(
            host=f["host"].get(), port=port, dbname=self.app.pg.dbname,
            user=f["user"].get(), password=f["password"].get(),
            sslmode=f["sslmode"].get() or "prefer",
            schema=f["schema"].get() or "public")
        m = self.ms_fields
        self.app.ms = MsConfig(
            server=m["server"].get(), database=self.app.ms.database,
            driver=m["driver"].get() or "ODBC Driver 18 for SQL Server",
            auth=m["auth"].get() or "sql", user=m["user"].get(),
            password=m["password"].get(), schema=m["schema"].get() or "dbo",
            encrypt=m["encrypt"].get() == "Yes",
            trust_cert=m["trust_cert"].get() == "Yes")
        if old_pg != self.app.pg or old_ms != self.app.ms:
            self.app.tables = []
            self.app.loaded_pair = None
            self.app.invalidate_plan()
            self.app.versions = {"source": "", "target": ""}
            self.next_btn.set_enabled(False)
            self.app.refresh_endpoints()
        self.app.conns = Connections(self.app.pg, self.app.ms)
        return True

    def apply_profile(self, data: dict):
        pg, ms = data.get("pg") or {}, data.get("ms") or {}
        if isinstance(data.get("options"), dict):
            try:
                saved_options = Options(**{k: v for k, v in data["options"].items()
                                           if k in Options.__dataclass_fields__})
                saved_options.validate()
            except (TypeError, ValueError):
                self.app.say("Saved transfer options are invalid; keeping current options.", "warn")
            else:
                self.app.options = saved_options
                transfer = self.app.stages[3]
                transfer.chunk.set(saved_options.chunk_size)
                transfer.workers.set(saved_options.workers)
                transfer.on_error.set(saved_options.on_row_error)
                for key, var in transfer.flags.items():
                    var.set(getattr(saved_options, key))
        for key, field in self.pg_fields.items():
            field.set(pg.get(key, getattr(PgConfig(), key)))
        for key, field in self.ms_fields.items():
            value = ms.get(key, getattr(MsConfig(), key))
            if key in ("encrypt", "trust_cert"):
                value = "Yes" if value is True or str(value).lower() in ("true", "yes", "1") else "No"
            field.set(value)
        # The database pair is remembered too; the pickers downstream show it.
        self.app.pg.dbname = pg.get("dbname") or ""
        self.app.ms.database = ms.get("database") or ""

    def _test(self):
        if not self._commit():
            return

        def work():
            return (self.app.conns.probe_source(), self.app.conns.probe_target())

        def done(payload):
            src, tgt = payload
            self.app.versions["source"] = src["version"]
            self.app.versions["target"] = tgt["version"]
            self.app.refresh_endpoints()
            self.notes.configure(
                text=f"{len(src['databases'])} databases visible on "
                     f"{self.app.pg.host}, {len(tgt['databases'])} on "
                     f"{self.app.ms.server}. Pick the pair on the next stage; "
                     "snapshot isolation and collation are reported by "
                     "preflight, once a target database exists to check.")
            self.next_btn.set_enabled(True)
            self.app.rail.set_state(0, "done")
            self.app.unlock(self._next_stage())
            self.app.say(
                f"Both servers reachable. Source {src['version'][:60]}; "
                f"target {tgt['version'][:60]}.", "ok")

        self.app.run_async(work, on_done=done, message="Testing connections")

    def _next_stage(self) -> int:
        """The first stage after Connect that this activity actually uses."""
        stages = ACTIVITIES[self.app.activity][2]
        return stages[1] if len(stages) > 1 else 0

    def set_profile_names(self, names: list[str], active: str = "Default"):
        if hasattr(self, "profile_combo"):
            self.profile_combo["values"] = names
            if active in names:
                self.profile_var.set(active)
            elif names:
                self.profile_var.set(names[0])

    def _on_profile_selected(self, _event=None):
        name = self.profile_var.get()
        if name:
            self.app.load_named_profile(name)

    def _save_active_profile(self):
        if self._commit():
            name = self.profile_var.get() or "Default"
            self.app.save_profile(self.remember.get(), profile_name=name)

    def _save_profile_as(self):
        name = simpledialog.askstring("Save Profile", "Enter a name for this connection profile:",
                                     parent=self)
        if name and name.strip():
            name = name.strip()
            if self._commit():
                self.app.save_profile(self.remember.get(), profile_name=name)

    def on_show(self):
        self.next_btn.set_text(f"Continue to {STAGES[self._next_stage()][0].lower()}")
        if hasattr(self.app, "list_profiles"):
            profiles = self.app.list_profiles()
            active = self.profile_var.get()
            self.set_profile_names(profiles, active if active in profiles else (profiles[0] if profiles else "Default"))

    def _continue(self):
        self.app._goto(self._next_stage())


# ===========================================================================
class WorkspaceConnections(ConnectStage):
    """Credential editor for workspace tests; selecting a profile is local
    until the user explicitly tests or saves it."""

    def build(self):
        super().build()
        self._testing = False
        self.next_btn.set_text("Close")
        self.next_btn.command = self._close
        self.master.protocol("WM_DELETE_WINDOW", self._close)
        self.next_btn.set_enabled(True)
        self.profile_combo.configure(width=22)
        Button(self.profile_combo.master, "Enter manually", self._manual,
               variant="ghost", font=self.fonts["body"]).pack(side="left")
        results = ttk.Frame(self.notes.master)
        results.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(SP["md"], 0))
        self.results = {}
        for index, name in enumerate(("PostgreSQL", "SQL Server")):
            results.columnconfigure(index, weight=1, uniform="results")
            label = ttk.Label(results, text=f"{name} · Not tested", style="Muted.TLabel",
                              padding=SP["md"], wraplength=455, justify="left")
            label.grid(row=0, column=index, sticky="nsew", padx=(0, SP["sm"]))
            self.results[name] = label
        self.progress = ttk.Progressbar(results, mode="indeterminate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(SP["sm"], 0))
        self._profiles = {}
        try:
            with open(PROFILE_PATH, encoding="utf-8") as stream:
                data = json.load(stream)
            if isinstance(data, dict):
                self._profiles = data.get("profiles", {"Default": data})
                if not isinstance(self._profiles, dict):
                    self._profiles = {}
        except (OSError, ValueError):
            pass
        self.set_profile_names(list(self._profiles), self.app.stages[0].profile_var.get())
        if self._profiles:
            self._on_profile_selected()
        else:
            self._manual()

    def apply_profile(self, data):
        # Workspace tests use server credentials only, never saved migration
        # options or database selections.
        for fields, config, values in (
                (self.pg_fields, PgConfig(), data.get("pg") or {}),
                (self.ms_fields, MsConfig(), data.get("ms") or {})):
            for key, field in fields.items():
                value = values.get(key, getattr(config, key))
                if key in ("encrypt", "trust_cert"):
                    value = "Yes" if str(value).lower() in ("true", "yes", "1") else "No"
                field.set(value)
        self.remember.set(False)
        for name, label in self.results.items():
            label.configure(text=f"{name} · Not tested", foreground=C["muted"])

    def _on_profile_selected(self, _event=None):
        name = self.profile_var.get()
        self.apply_profile(self._profiles.get(name, {}))
        self.notes.configure(text=f"Loaded profile: {name}. Review credentials, then test both connections. "
                             "Passwords omitted from the saved profile must be entered here.")

    def _manual(self):
        self.profile_var.set("")
        self.apply_profile({})
        self.notes.configure(text="Manual entry. Enter both server credentials, then test. "
                             "Use Save As to create a named profile; passwords are saved only if selected.")

    def _commit(self):
        if not super()._commit():
            return False
        connection_stage = self.app.stages[0]
        for source, target in ((self.pg_fields, connection_stage.pg_fields),
                               (self.ms_fields, connection_stage.ms_fields)):
            for key, field in source.items():
                target[key].set(field.get())
        connection_stage.profile_var.set(self.profile_var.get())
        self.next_btn.set_enabled(True)
        return True

    def _test(self):
        if self._testing or not self._commit():
            return
        conns = self.app.conns
        self._set_testing(True)
        for name, label in self.results.items():
            label.configure(text=f"{name} · Connecting…", foreground=C["muted"])
        self.notes.configure(text="Testing the displayed credentials. Results will appear above.")

        def work():
            outcomes = {}
            for name, probe in (("PostgreSQL", conns.probe_source), ("SQL Server", conns.probe_target)):
                try:
                    outcomes[name] = (True, probe())
                except Exception as exc:
                    detail = str(exc)
                    for secret in (conns.pg.password, conns.ms.password):
                        if secret:
                            detail = detail.replace(secret, "[redacted]")
                    outcomes[name] = (False, detail)
            return outcomes

        self.app.run_async(work, on_done=self._tested, on_error=self._test_failed,
                           message="Testing workspace connections")

    def _set_testing(self, active):
        self._testing = active
        self.test_btn.set_text("Testing…" if active else "Test both connections")
        self.test_btn.set_enabled(not active)
        self.next_btn.set_enabled(not active)
        if active:
            self.progress.start()
        else:
            self.progress.stop()
        # Freeze the tested credentials and profile actions until results arrive.
        def controls(widget):
            for child in widget.winfo_children():
                if isinstance(child, Button) and child not in (self.test_btn, self.next_btn):
                    child.set_enabled(not active)
                elif isinstance(child, (ttk.Entry, ttk.Combobox, ttk.Checkbutton)):
                    if active:
                        child._test_previous_state = str(child.cget("state"))
                        child.configure(state="disabled")
                    else:
                        child.configure(state=getattr(child, "_test_previous_state", "normal"))
                controls(child)
        controls(self)

    def _tested(self, outcomes):
        self._set_testing(False)
        passed = all(ok for ok, _ in outcomes.values())
        for name, (ok, result) in outcomes.items():
            key = "source" if name == "PostgreSQL" else "target"
            self.app.versions[key] = result["version"] if ok else ""
            detail = (f"{len(result['databases'])} databases visible\n{result['version'][:160]}"
                      if ok else str(result))
            self.results[name].configure(text=f"{name} · {'Connected' if ok else 'Failed'}\n{detail}",
                                          foreground=C["ok"] if ok else C["stop"])
        self.app.refresh_endpoints()
        self.app.stages[0].next_btn.set_enabled(passed)
        self.notes.configure(text="Both servers authenticated successfully. You can close this window or test another profile."
                             if passed else "Review the failed server’s credentials and TLS settings, then test again.")
        self.app.front_conn_status.configure(text="Both connections verified." if passed else "Connection test failed. Review workspace results.",
                                             foreground=C["ok"] if passed else C["stop"])

    def _test_failed(self, exc):
        self._tested({name: (False, "Unable to complete the connection test. Please retry.") for name in self.results})

    def _close(self):
        if not self._testing:
            self.master.destroy()

    def _save_active_profile(self):
        if not self.profile_var.get():
            self._save_profile_as()
            return
        self._save_named(self.profile_var.get())

    def _save_profile_as(self):
        name = simpledialog.askstring("Save Profile", "Name this connection profile (for example UAT):",
                                      parent=self)
        if name and name.strip():
            self._save_named(name.strip())

    def _save_named(self, name):
        if name in self._profiles and not messagebox.askyesno(
                "Replace profile", f"Replace saved credentials for '{name}'?", parent=self):
            return
        if not self._commit():
            return
        try:
            self.app.save_profile(self.remember.get(), profile_name=name)
        except OSError as exc:
            messagebox.showerror("Could not save profile", str(exc), parent=self)
            return
        payload = {"pg": asdict(self.app.pg), "ms": asdict(self.app.ms)}
        if not self.remember.get():
            payload["pg"]["password"] = payload["ms"]["password"] = ""
        self._profiles[name] = payload
        self.set_profile_names(list(self._profiles), name)
        self.notes.configure(text=f"Profile '{name}' saved. You can now test both connections.")


class TablesStage(Stage):
    MODES = {"schema + data": "both", "schema only": "schema",
             "data only": "data"}
    # Sortable, right-aligned where the eye compares magnitudes.
    COLUMNS = [("sel", "", 34, "center", False), ("table", "Table", 260, "w", True),
               ("srows", "Source rows", 110, "e", False),
               ("trows", "Target rows", 110, "e", False),
               ("cols", "Cols", 60, "e", False),
               ("state", "Target state", 150, "w", False),
               ("plan", "Will do", 190, "w", False)]
    VIEWS = {"All tables": "all", "Selected only": "selected",
             "Missing on target": "missing", "Target not empty": "dirty",
             "Empty and ready": "ready"}

    def build(self):
        block = section(self, "Tables", self.fonts,
                        "Choose the databases, then what moves. Row counts are "
                        "exact on both sides. Tables Django owns on the target "
                        "are deselected for you.")
        block.pack(fill="both", expand=True)

        foot = ttk.Frame(block.body)
        foot.pack(side="bottom", fill="x", pady=(SP["md"], 0))

        self.picker = DatabasePicker(block.body, self.app, "Load schema",
                                     self.refresh)
        self.picker.pack(fill="x", pady=(0, SP["md"]))

        # --- one control strip, not three scattered rows ---------------------
        bar = ttk.Frame(block.body)
        bar.pack(fill="x", pady=(0, SP["sm"]))

        self.mode = Field(bar, "Migrate", self.fonts, kind="combo",
                          style_prefix="Root", width=16, values=list(self.MODES))
        self.mode.set(next(k for k, v in self.MODES.items()
                           if v == self.app.options.mode))
        self.mode.widget.bind("<<ComboboxSelected>>", lambda _e: self._set_mode())
        self.mode.pack(side="left")

        self.view = Field(bar, "Show", self.fonts, kind="combo",
                          style_prefix="Root", width=18, values=list(self.VIEWS))
        self.view.set("All tables")
        self.view.widget.bind("<<ComboboxSelected>>", lambda _e: self._render())
        self.view.pack(side="left", padx=(SP["md"], 0))

        find = ttk.Frame(bar)
        find.pack(side="left", padx=(SP["md"], 0))
        ttk.Label(find, text="Find", style="Muted.TLabel").pack(anchor="w",
                                                                pady=(0, 4))
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self._render())
        ttk.Entry(find, textvariable=self.search, width=24).pack()

        acts = ttk.Frame(bar)
        acts.pack(side="right", pady=(19, 0))
        Button(acts, "Reload schema", self.refresh, variant="quiet", icon="↻",
               font=self.fonts["small"], height=28).pack(side="right")
        for label, fn in (("None", lambda: self._bulk(False)),
                          ("All shown", lambda: self._bulk(True)),
                          ("Only matched", self._only_matched)):
            Button(acts, label, fn, variant="quiet", font=self.fonts["small"],
                   height=28).pack(side="right", padx=(0, SP["sm"]))

        wrap = ttk.Frame(block.body)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="extended")
        for key, title, width, anchor, stretch in self.COLUMNS:
            self.tree.heading(
                key, text=title,
                command=(lambda k=key: self._sort_by(k)) if key != "sel" else
                self._toggle_shown)
            self.tree.column(key, width=width, anchor=anchor, stretch=stretch)
        bar_v = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar_v.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar_v.pack(side="right", fill="y")
        self.tree.tag_configure("missing", foreground=C["warn"])
        self.tree.tag_configure("dirty", foreground=C["warn"])
        self.tree.tag_configure("blocked", foreground=C["stop"])
        self.tree.tag_configure("off", foreground=C["faint"])
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", lambda _e: self._toggle_current())
        self.tree.bind("<Return>", lambda _e: self._toggle_current())
        self._sort_key, self._sort_desc = "table", False

        # --- modern KPI metric tiles ------------------------------------------
        self.stats = ttk.Frame(block.body, style="Panel.TFrame", padding=(SP["md"], SP["sm"]))
        self.stats.pack(fill="x", pady=(SP["sm"], 0))
        self.stat_labels = {}
        for key, title in (("tables", "Tables selected"), ("rows", "Rows to move"),
                           ("create", "To create on target"),
                           ("dirty", "Target not empty"),
                           ("widest", "Largest table")):
            cell = ttk.Frame(self.stats, style="Raised.TFrame", padding=(14, 8))
            cell.pack(side="left", padx=(0, SP["md"]))
            ttk.Label(cell, text=title.upper(), style="PanelMuted.TLabel",
                      font=self.fonts["badge"]).pack(anchor="w")
            value = ttk.Label(cell, text="—", background=C["raised"],
                              foreground=C["text"], font=self.fonts["kpi_num"])
            value.pack(anchor="w", pady=(2, 0))
            self.stat_labels[key] = value

        self.summary = ttk.Label(foot, text="No schema loaded.",
                                 style="Muted.TLabel")
        self.summary.pack(side="left")
        self.next_btn = Button(foot, "Continue to preflight",
                               lambda: (self.app.unlock(2), self.app._goto(2)),
                               variant="primary", font=self.fonts["body"])
        self.next_btn.pack(side="right")
        self.next_btn.set_enabled(False)

    def reset(self):
        self.tree.delete(*self.tree.get_children())
        self.search.set("")
        self.view.set("All tables")
        self.sync_mode()
        self.summary.configure(text="No schema loaded.")
        for label in self.stat_labels.values():
            label.configure(text="—")
        self.next_btn.set_enabled(False)

    def _set_mode(self):
        self.app.options.mode = self.MODES.get(self.mode.get(), "both")
        self.app.say(f"Migrating {self.mode.get()}.", "info")
        self._render()

    def sync_mode(self):
        """Show whatever the shared options now hold — preflight can change the
        mode, and this control must not keep claiming the old one."""
        for label, value in self.MODES.items():
            if value == self.app.options.mode:
                self.mode.set(label)
                break

    def on_show(self):
        self.picker.sync()
        if not self.app.tables and self.app.pg.dbname and self.app.ms.database:
            self.refresh()

    def refresh(self):
        """Pick up the pair, make sure the target database is really there, then
        read both schemas."""
        if self.app.conns is None or not self.picker.require():
            return
        self.picker.ensure_target(self._discover)

    def _discover(self):
        introspector = Introspector(self.app.conns)
        self.app.run_async(
            lambda: introspector.discover(
                progress=lambda m: self.after(0, self.app.say, m)),
            on_done=self._loaded, message="Reading schemas")

    def _loaded(self, tables: list[Table]):
        self.app.tables = tables
        self.app.loaded_pair = (self.app.pg.dbname, self.app.ms.database)
        self._render()
        self.app.sync_job()
        missing = sum(1 for t in tables if t.selected and not t.target_exists)
        self.app.say(f"{len(tables)} tables found. "
                     f"{missing} have no matching target table.",
                     "warn" if missing else "ok")
        self.app.rail.set_state(1, "done")
        self.next_btn.set_enabled(True)

    def _visible(self) -> list:
        """Whatever the Show filter, the Find box and the sort ask for."""
        raw_term = self.search.get().strip()
        term = raw_term.lower()
        view = self.VIEWS.get(self.view.get(), "all")
        pattern = None
        if raw_term:
            if raw_term.startswith("re:"):
                try:
                    pattern = re.compile(raw_term[3:].strip(), re.IGNORECASE)
                except re.error:
                    pattern = None
            elif any(ch in raw_term for ch in ("^", "$", "*", "+", "?", "[", "]", "(", ")", "|")):
                try:
                    pattern = re.compile(raw_term, re.IGNORECASE)
                except re.error:
                    pattern = None

        rows = []
        for table in self.app.tables:
            if raw_term:
                if pattern is not None:
                    if not pattern.search(table.name):
                        continue
                elif term not in table.name.lower():
                    continue
            if view == "selected" and not table.selected:
                continue
            if view == "missing" and table.target_exists:
                continue
            if view == "dirty" and not table.target_rows:
                continue
            if view == "ready" and (not table.target_exists or table.target_rows):
                continue
            rows.append(table)
        keys = {"table": lambda t: t.name.lower(),
                "srows": lambda t: t.source_rows,
                "trows": lambda t: t.target_rows,
                "cols": lambda t: len(t.columns),
                "state": lambda t: (t.target_exists, t.target_rows),
                "plan": lambda t: t.plan_summary(self.app.options)}
        rows.sort(key=keys.get(self._sort_key, keys["table"]),
                  reverse=self._sort_desc)
        return rows

    def _sort_by(self, key: str):
        self._sort_desc = not self._sort_desc if key == self._sort_key else False
        self._sort_key = key
        self._render()

    def _toggle_shown(self):
        """The tick header selects or clears everything currently listed."""
        shown = self._visible()
        target = not all(t.selected for t in shown) if shown else True
        for table in shown:
            table.selected = target
        self._render()

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        options = self.app.options
        rows = self._visible()
        for table in rows:
            if not table.target_exists:
                state = ("will be created" if table.does_schema(options)
                         else "not in target")
                tag = "" if table.does_schema(options) else "missing"
            elif table.target_rows:
                state, tag = f"holds {table.target_rows:,}", "dirty"
            else:
                state, tag = "empty, ready", ""
            if table.plan == "skip" or not table.selected:
                tag = "off"
            elif table.plan in ("clean", "schema"):
                tag = "blocked"
            self.tree.insert(
                "", "end", iid=table.name,
                values=("\u25A0" if table.selected else "\u25A1",
                        table.name, f"{table.source_rows:,}",
                        f"{table.target_rows:,}", len(table.columns), state,
                        table.plan_summary(options)),
                tags=[tag] if tag else [])

        # Column headers show the sort, so nobody has to guess the order.
        for key, title, _w, _a, _s in self.COLUMNS:
            mark = ("  \u25BC" if self._sort_desc else "  \u25B2") \
                if key == self._sort_key else ""
            if key != "sel":
                self.tree.heading(key, text=title + mark)

        chosen = [t for t in self.app.tables if t.selected and t.plan != "skip"]
        moving = [t for t in chosen if t.does_data(options)]
        rows_total = sum(t.source_rows for t in moving)
        fresh = sum(1 for t in chosen
                    if not t.target_exists and t.does_schema(options))
        dirty = sum(1 for t in chosen if t.target_rows)
        widest = max(moving, key=lambda t: t.source_rows, default=None)
        self.stat_labels["tables"].configure(
            text=f"{len(chosen)} of {len(self.app.tables)}")
        self.stat_labels["rows"].configure(
            text=f"{rows_total:,}" if moving else "none — schema only")
        self.stat_labels["create"].configure(text=f"{fresh}" if fresh else "—")
        self.stat_labels["dirty"].configure(text=f"{dirty}" if dirty else "—")
        self.stat_labels["widest"].configure(
            text=f"{widest.name} ({widest.source_rows:,})" if widest else "—")

        parts = [f"showing {len(rows)} of {len(self.app.tables)}"]
        excluded = [t for t in chosen if t.row_filter or t.exclude_columns]
        if excluded:
            parts.append(f"{len(excluded)} moving partially")
        skipped = [t for t in self.app.tables if t.plan == "skip"]
        if skipped:
            parts.append(f"{len(skipped)} skipped by preflight")
        self.summary.configure(text="  ·  ".join(parts))

    def _click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        name = self.tree.identify_row(event.y)
        if name:
            self._toggle(name)

    def _toggle_current(self):
        for name in self.tree.selection():
            self._toggle(name)

    def _toggle(self, name: str):
        for table in self.app.tables:
            if table.name == name:
                table.selected = not table.selected
                break
        self._render()

    def _bulk(self, value: bool):
        for table in self._visible():
            table.selected = value
        self._render()

    def _only_matched(self):
        for table in self.app.tables:
            table.selected = table.target_exists
        self._render()


# ===========================================================================
class PreflightStage(Stage):
    LEVEL_TONE = {"stop": "stop", "warn": "warn", "note": "muted"}

    def __init__(self, master, app):
        self._sash_after = None
        self._preflight_selection: set[str] = set()
        self._preflight_mode = "both"
        super().__init__(master, app)

    def build(self):
        block = section(self, "Preflight", self.fonts,
                        "Nothing writes until these pass. Warnings need an "
                        "explicit acknowledgement; a blocking finding stops the "
                        "run unless you leave its table behind or move schema "
                        "only.")
        block.pack(fill="both", expand=True)

        foot = ttk.Frame(block.body)
        foot.pack(side="bottom", fill="x", pady=(SP["md"], 0))
        # The routes get their own row: three radios beside a checkbox and a
        # button is how a footer runs out of width.
        route_row = ttk.Frame(block.body)
        route_row.pack(side="bottom", fill="x", pady=(SP["sm"], 0))

        bar = ttk.Frame(block.body)
        bar.pack(fill="x", pady=(0, SP["md"]))
        Button(bar, "Run checks", self.run, variant="ghost", icon="🔍",
               font=self.fonts["body"]).pack(side="left")
        self.counts = ttk.Frame(bar)
        self.counts.pack(side="left", padx=(SP["md"], 0))
        self.pills = {}
        for level in ("stop", "warn", "note"):
            pill = Pill(self.counts, f"0 {level}", self.LEVEL_TONE[level],
                        font=self.fonts["small"])
            pill.pack(side="left", padx=(0, SP["sm"]))
            self.pills[level] = pill

        self.size_report = None
        self.size_signature = None
        size_row = ttk.Frame(block.body, style="Panel.TFrame", padding=(12, 8))
        size_row.pack(fill="x", pady=(0, SP["sm"]))
        self.size_label = ttk.Label(size_row, text="Selected data: run checks to estimate size",
                                    style="Panel.TLabel", font=self.fonts["strong"])
        self.size_label.pack(side="left")
        Button(size_row, "Refresh size", self._refresh_size, variant="ghost",
               font=self.fonts["small"], height=30, background=C["panel"]).pack(side="right")
        self.size_note = ttk.Label(block.body, text="Sampled source values; excludes indexes. Actual transferred bytes may differ.",
                                  style="Muted.TLabel", wraplength=800)
        self.size_note.pack(anchor="w", pady=(0, SP["sm"]))

        self.split = split = ttk.PanedWindow(block.body, orient="vertical")
        split.pack(fill="both", expand=True)
        self._sash_placed = False

        top = ttk.Frame(split)
        self.tree = ttk.Treeview(
            top, columns=("level", "table", "column", "check", "detail"),
            show="headings", selectmode="browse")
        for key, title, width, stretch in (
                ("level", "", 68, False), ("table", "Table", 190, False),
                ("column", "Column", 170, False),
                ("check", "Finding", 180, False), ("detail", "Detail", 520, True)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=stretch, anchor="w")
        sb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("stop", foreground=C["stop"])
        self.tree.tag_configure("warn", foreground=C["warn"])
        self.tree.tag_configure("note", foreground=C["muted"])
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        split.add(top, weight=2)

        bottom = ttk.Frame(split, style="Panel.TFrame", padding=SP["md"])
        self.detail_head = ttk.Label(bottom, text="Select a finding for the detail",
                                     style="PanelMuted.TLabel")
        self.detail_head.pack(anchor="w")
        pane = ttk.Frame(bottom, style="Panel.TFrame")
        pane.pack(fill="both", expand=True)
        self.detail = tk.Text(pane, height=9, wrap="word", bd=0,
                              background=C["panel"], foreground=C["text"],
                              font=self.fonts["data_sm"], padx=0, pady=8,
                              highlightthickness=0, state="disabled")
        # A finding now runs longer than the pane: without this the remedy is
        # there but unreachable.
        detail_bar = ttk.Scrollbar(pane, orient="vertical",
                                   command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_bar.set)
        self.detail.pack(side="left", fill="both", expand=True)
        detail_bar.pack(side="right", fill="y")
        # Keys muted, values plain, headings coloured: a dense block stays
        # scannable when every finding carries a dozen lines.
        self.detail.tag_configure("key", foreground=C["muted"])
        self.detail.tag_configure("head", foreground=C["source"],
                                  font=self.fonts["strong"], spacing1=6)
        self.detail.tag_configure("lead", foreground=C["text"],
                                  font=self.fonts["body"], spacing3=4)
        split.add(bottom, weight=1)

        # A blocking finding stops the whole run, which is right by default and
        # wrong as an absolute: one broken table should not hold back forty good
        # ones. These are the two ways past it that stay honest about what moves.
        # Per table, because tables fail for different reasons: one wants its
        # schema built, another wants the offending rows left behind, a third is
        # genuinely unmigratable. A single global switch cannot say that.
        self.routes = ttk.Frame(route_row)
        self.route_label = ttk.Label(
            self.routes, text="", style="Muted.TLabel", justify="left")
        self.route_label.pack(side="left", padx=(0, SP["md"]))
        self.plan_buttons = {}
        for key, label in (("auto", "Fix it first"),
                           ("clean", "Move what passes"),
                           ("schema", "Schema only"),
                           ("skip", "Skip table")):
            btn = Button(self.routes, label,
                         lambda k=key: self._plan_selected(k),
                         variant="quiet", font=self.fonts["small"], height=28)
            btn.pack(side="left", padx=(0, SP["sm"]))
            self.plan_buttons[key] = btn
        self.plan_note = ttk.Label(route_row, text="", style="Muted.TLabel",
                                   justify="left")
        self.plan_note.pack(side="left", padx=(SP["md"], 0))
        # Kept so the older whole-run helpers still have something to read.
        self.route = tk.StringVar(value="fix")
        self.route_buttons = self.plan_buttons

        self.ack = tk.BooleanVar(value=False)
        self.ack_box = ttk.Checkbutton(
            foot, text="I have read the warnings and accept them",
            variable=self.ack, command=self._recheck)
        self.ack_box.pack(side="left")
        self.next_btn = Button(foot, "Continue to transfer", self._continue,
                               variant="primary", font=self.fonts["body"])
        self.next_btn.pack(side="right")
        self.next_btn.set_enabled(False)

    def on_show(self):
        if self.size_signature is not None and self.size_signature != self.app.plan_signature():
            self.size_label.configure(text="Selection changed · refresh size estimate")
        # Findings carry a dozen lines each, so the pane that shows them starts
        # at nearly half the stage rather than the sliver pack would give it.
        # Once only: after that the sash is the reader's to drag.
        if not self._sash_placed and self._sash_after is None:
            self._sash_after = self.after(60, self._place_sash)

    def _place_sash(self):
        if self._sash_after is not None:
            self.after_cancel(self._sash_after)
            self._sash_after = None
        height = self.split.winfo_height()
        if height < 120:
            self._sash_after = self.after(60, self._place_sash)
            return
        self.split.sashpos(0, int(height * 0.55))
        self._sash_placed = True

    def destroy(self):
        if self._sash_after is not None:
            self.after_cancel(self._sash_after)
            self._sash_after = None
        super().destroy()

    def run(self):
        if self.app.conns is None:
            return
        # The selection and mode as the Tables stage left them: every route is
        # applied to this, so switching between routes is not cumulative.
        self._preflight_selection = self._selected_names()
        self._preflight_mode = self.app.options.mode
        # Re-running the checks re-asks the question, so old answers go.
        for table in self.app.tables:
            table.plan = "auto"
            table.exclude_columns = []
            table.row_filter = ""
            table.exclusion_notes = []
        checker = Preflight(self.app.conns, self.app.tables, self.app.options)
        self.checker = checker
        signature = self.app.plan_signature()
        tables, options = copy.deepcopy(self.app.tables), copy.deepcopy(self.app.options)
        self.size_label.configure(text="Selected data: estimating…")
        def work():
            issues = checker.run(progress=lambda m: self.after(0, self.app.say, m))
            try:
                report = estimate_transfer_size(checker.conns, tables, options)
            except Exception as exc:
                report = {"estimated_bytes": None, "database_bytes": None, "errors": [str(exc)]}
            return issues, report
        def done(payload):
            issues, report = payload
            self._loaded(issues)
            self._show_size(report, signature)
        self.app.run_async(work, on_done=done, message="Running preflight checks")

    def _show_size(self, report, signature):
        self.size_report = report
        self.size_signature = signature
        if signature != self.app.plan_signature():
            self.size_label.configure(text="Selection changed · refresh size estimate")
            return
        self.size_label.configure(text=f"Selected data ≈ {format_bytes(report['estimated_bytes'])}"
                                 + (f"  ·  {report['rows']:,} rows" if report.get("estimated_bytes") is not None else ""))
        disk = format_bytes(report.get("database_bytes"))
        if report.get("errors"):
            self.size_note.configure(text="Size unavailable for some tables; review the activity log. Database on disk: " + disk)
            for error in report["errors"]:
                self.app.say("Size estimate: " + error, "warn")
        else:
            self.size_note.configure(text=f"Database on disk: {disk} (includes indexes and unselected data). "
                                     "Selected size is sampled; actual transfer bytes may differ.")
            self.app.say(f"Estimated selected data: {format_bytes(report['estimated_bytes'])}; "
                         f"{report['rows']:,} rows across {report['tables']} tables. Database on disk: {disk}.")

    def _refresh_size(self):
        if self.app.conns is None or self.app.operation_active():
            return
        tables, options = copy.deepcopy(self.app.tables), copy.deepcopy(self.app.options)
        signature = self.app.plan_signature()
        conns = self.app.conns
        self.size_label.configure(text="Selected data: estimating…")
        self.app.run_async(lambda: estimate_transfer_size(conns, tables, options),
                           on_done=lambda report: self._show_size(report, signature),
                           on_error=lambda exc: self._show_size({"estimated_bytes": None,
                               "database_bytes": None, "errors": [str(exc)]}, signature),
                           message="Estimating selected data size")

    def _loaded(self, issues):
        self._completed = True
        self.app.issues = issues
        self.tree.delete(*self.tree.get_children())
        for i, issue in enumerate(issues):
            self.tree.insert("", "end", iid=str(i),
                             values=(issue.level.upper(), issue.table,
                                     issue.column_label, issue.check,
                                     issue.detail),
                             tags=(issue.level,))
        for level in ("stop", "warn", "note"):
            n = sum(1 for i in issues if i.level == level)
            self.pills[level].set(f"{n} {level}", self.LEVEL_TONE[level])

        # Land on the most serious finding with its detail already open: an
        # empty pane under a list of one-line errors reads as "no detail".
        if issues:
            first = min(range(len(issues)),
                        key=lambda i: ("stop", "warn", "note").index(
                            issues[i].level))
            self.tree.selection_set(str(first))
            self.tree.focus(str(first))
            self.tree.see(str(first))
            self._show_detail()
        else:
            self.detail_head.configure(text="No findings")
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0", "Nothing to fix. Every check passed.")
            self.detail.configure(state="disabled")
        self._recheck()
        stops = sum(1 for i in issues if i.level == "stop")
        self.app.rail.set_state(2, "fail" if stops else "done")
        self.app.sync_job()
        self.app.say(
            f"Preflight complete: {stops} blocking, "
            f"{sum(1 for i in issues if i.level == 'warn')} warnings.",
            "error" if stops else "ok")
        # Each finding by name in the log, so the record survives the window.
        blocked, global_stops = self._blocked()
        if blocked and not global_stops:
            self.app.say(
                f"{len(blocked)} table(s) blocked: " + ", ".join(sorted(blocked))
                + ". Select each one to give it a plan — move what passes, "
                  "schema only, or skip. Tables that passed are unaffected.",
                "warn")
        elif global_stops:
            self.app.say("Blocking findings apply to the whole run, so they "
                         "cannot be skipped by leaving tables out.", "error")
        for issue in issues:
            if issue.level == "note":
                continue
            where = issue.table + (f".{issue.column_label}" if issue.columns
                                   else "")
            self.app.say(f"{issue.level.upper()} {where} — {issue.check}: "
                         f"{issue.detail}",
                         "error" if issue.level == "stop" else "warn")

    def _fix_for(self, name: str) -> dict:
        """Everything a "clean" plan would do for one table, merged across all
        of its blocking findings."""
        merged = {"exclude_columns": [], "filters": [], "notes": [], "plans": None}
        for issue in self.app.issues:
            if issue.level != "stop" or issue.table != name:
                continue
            fix = issue.fix or {}
            allowed = set(fix.get("plans") or ["skip"])
            merged["plans"] = allowed if merged["plans"] is None \
                else merged["plans"] & allowed
            for col in fix.get("exclude_columns", []):
                if col not in merged["exclude_columns"]:
                    merged["exclude_columns"].append(col)
            if fix.get("row_filter"):
                merged["filters"].append(fix["row_filter"])
            if fix.get("note"):
                merged["notes"].append(fix["note"])
        merged["plans"] = sorted(merged["plans"] or {"skip"})
        merged["row_filter"] = " AND ".join(merged["filters"])
        return merged

    def _apply_plan(self, name: str, plan: str):
        """Give one table its own route through the run."""
        table = next((t for t in self.app.tables if t.name == name), None)
        if table is None:
            return
        fix = self._fix_for(name)
        table.plan = plan
        table.selected = plan != "skip"
        table.exclude_columns = []
        table.row_filter = ""
        table.exclusion_notes = []
        if plan == "clean":
            table.exclude_columns = list(fix["exclude_columns"])
            table.row_filter = fix["row_filter"]
            table.exclusion_notes = list(fix["notes"])
        self.app.say(f"{name}: {plan} — {PLANS[plan]}"
                     + (f" ({'; '.join(fix['notes'])})"
                        if plan == "clean" and fix["notes"] else ""),
                     "warn" if plan != "auto" else "info")
        self._render_plans()
        self.app.stages[1]._render()
        self._recheck()

    def _blocked(self) -> tuple[set[str], list]:
        """Tables that cannot move, and the findings that have no table to skip.

        A finding against "—" is about the database, not a table, so skipping
        tables cannot clear it.
        """
        stops = [i for i in self.app.issues if i.level == "stop"]
        tables = {i.table for i in stops if i.table != "—"}
        global_stops = [i for i in stops if i.table == "—"]
        return tables, global_stops

    def _selected_names(self) -> set[str]:
        return {t.name for t in self.app.tables if t.selected}

    def reset(self):
        self.size_report = None
        self.size_signature = None
        self.size_label.configure(text="Selected data: run checks to estimate size")
        self.size_note.configure(text="Sampled source values; excludes indexes. Actual transferred bytes may differ.")
        self._completed = False
        self.tree.delete(*self.tree.get_children())
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.configure(state="disabled")
        self.detail_head.configure(text="Select a finding for the detail")
        self.routes.pack_forget()
        self.plan_note.configure(text="")
        for level in ("stop", "warn", "note"):
            self.pills[level].set(f"0 {level}", self.LEVEL_TONE[level])
        self.ack.set(False)
        self.next_btn.set_enabled(False)
        self._preflight_selection = set()

    def _current_table(self) -> str:
        selection = self.tree.selection()
        if not selection:
            return ""
        return self.app.issues[int(selection[0])].table

    def _plan_selected(self, plan: str):
        name = self._current_table()
        if not name or name == "—":
            return
        self._apply_plan(name, plan)

    def _render_plans(self):
        """The plan controls describe the selected finding's table only."""
        name = self._current_table()
        blocked, _ = self._blocked()
        if not name or name == "—" or name not in blocked:
            self.routes.pack_forget()
            self.plan_note.configure(text="")
            return

        self.routes.pack(side="left")
        table = next((t for t in self.app.tables if t.name == name), None)
        current = table.plan if table else "auto"
        fix = self._fix_for(name)
        allowed = set(fix["plans"]) | {"auto"}

        self.route_label.configure(text=f"{name} is blocked —")
        for key, btn in self.plan_buttons.items():
            btn.set_enabled(key in allowed)
            btn.variant = "target" if key == current else "quiet"
            btn._draw()

        if current == "clean":
            note = "; ".join(fix["notes"]) or "moves what passes"
            self.plan_note.configure(text="→ " + note)
        elif current == "schema":
            self.plan_note.configure(text="→ table created, no rows moved")
        elif current == "skip":
            self.plan_note.configure(text="→ not migrated at all")
        elif "clean" not in allowed:
            self.plan_note.configure(
                text="no filter can fix this one — schema only or skip")
        else:
            self.plan_note.configure(text="")

    def _recheck(self):
        if self.size_signature is not None and self.size_signature != self.app.plan_signature():
            self.size_label.configure(text="Selection changed · refresh size estimate")
        warns = [i for i in self.app.issues if i.level == "warn"]
        blocked, global_stops = self._blocked()
        self.ack_box.state(["!disabled"] if warns else ["disabled"])
        self._render_plans()

        # A blocked table clears once it has been given a plan that answers its
        # findings. Anything still on "auto" is still blocking.
        by_name = {t.name: t for t in self.app.tables}
        unresolved = sorted(
            name for name in blocked
            if by_name.get(name) is None or by_name[name].plan == "auto")
        moving = [t for t in self.app.tables if t.selected and t.plan != "skip"]

        cleared = not unresolved and not global_stops and bool(moving)
        self.next_btn.set_enabled(
            getattr(self, "_completed", False) and cleared and (not warns or self.ack.get()))
        if unresolved:
            self.next_btn.set_text(f"{len(unresolved)} blocked")
        elif blocked:
            self.next_btn.set_text(f"Continue with {len(moving)} tables")
        else:
            self.next_btn.set_text("Continue to transfer")
        self.unresolved = unresolved

    def _continue(self):
        """Confirm, by name, everything being deliberately left behind."""
        departures = []
        for table in self.app.tables:
            if table.plan == "skip":
                departures.append(f"  {table.name}: not migrated")
            elif table.plan == "schema":
                departures.append(f"  {table.name}: table created, no rows")
            elif table.plan == "clean":
                bits = []
                if table.row_filter:
                    bits.append(f"rows where NOT ({table.row_filter})")
                if table.exclude_columns:
                    bits.append("columns " + ", ".join(table.exclude_columns))
                departures.append(f"  {table.name}: without " + " and ".join(bits))
        if departures:
            if not messagebox.askyesno(
                    "Confirm what is left behind",
                    "These tables do not migrate in full:\n\n"
                    + "\n".join(departures)
                    + "\n\nThe two databases will not match for them until "
                      "that is dealt with separately. The activity log and the "
                      "exported report record every exclusion.\n\nContinue?"):
                return
            for line in departures:
                self.app.say("Leaving behind —" + line.strip(), "warn")
        self.app._preflight_signature = self.app.plan_signature()
        self.app.unlock(3)
        self.app._goto(3)

    def _show_detail(self, _event=None):
        """The whole finding: what was measured, on which column, how each side
        declares it, and only then what to do about it."""
        selection = self.tree.selection()
        if not selection:
            return
        issue = self.app.issues[int(selection[0])]
        where = issue.table
        if issue.columns:
            where += f".{issue.columns[0]}" if len(issue.columns) == 1 \
                     else f"  ({issue.column_label})"
        self.detail_head.configure(
            text=f"{issue.level.upper()}  ·  {where}  ·  {issue.check}")

        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", issue.detail + "\n", "lead")

        if issue.facts:
            width = max(len(k) for k in issue.facts)
            self.detail.insert("end", "\nWhat was found\n", "head")
            for key, value in issue.facts.items():
                self.detail.insert("end", key.ljust(width) + "   ", "key")
                self.detail.insert("end", f"{value}\n")

        self.detail.insert("end", "\nHow to fix\n", "head")
        self.detail.insert("end", (issue.remedy or "No action needed.") + "\n")

        if issue.check in ("Missing target table", "Will be created") \
                and hasattr(self, "checker"):
            table = next((t for t in self.app.tables if t.name == issue.table),
                         None)
            if table:
                self.detail.insert("end", "\nAssisted DDL (review before "
                                          "running)\n", "head")
                self.detail.insert("end", self.checker.generate_ddl(table) + "\n")

        self.detail.see("1.0")
        self.detail.configure(state="disabled")
        self._render_plans()


# ===========================================================================
class TransferStage(Stage):
    def build(self):
        block = section(self, "Transfer", self.fonts,
                        "Foreign keys come down for the load and are revalidated "
                        "afterwards. Progress is checkpointed after every table.")
        block.pack(fill="both", expand=True)

        foot = ttk.Frame(block.body)
        foot.pack(side="bottom", fill="x", pady=(SP["md"], 0))
        self.resume_note = ttk.Label(block.body, text="", style="Muted.TLabel",
                                     wraplength=900, justify="left")
        self.resume_note.pack(side="bottom", anchor="w", pady=(SP["sm"], 0))

        opts = ttk.Frame(block.body, style="Panel.TFrame", padding=SP["md"])
        opts.pack(fill="x", pady=(0, SP["md"]))
        left = ttk.Frame(opts, style="Panel.TFrame")
        left.pack(side="left", fill="x", expand=True)
        right = ttk.Frame(opts, style="Panel.TFrame")
        right.pack(side="left", fill="x", expand=True, padx=(SP["lg"], 0))

        self.chunk = Field(left, "Batch size (rows)", self.fonts, kind="spin",
                           width=12)
        self.chunk.set(50000)
        self.chunk.pack(anchor="w", pady=(0, SP["sm"]))
        self.workers = Field(left, "Parallel tables", self.fonts, kind="combo",
                             values=["1", "2", "3", "4"], width=12)
        self.workers.set("1")
        self.workers.pack(anchor="w", pady=(0, SP["sm"]))
        self.on_error = Field(left, "If a batch fails", self.fonts, kind="combo",
                              values=["abort", "quarantine"], width=12)
        self.on_error.set("abort")
        self.on_error.pack(anchor="w")

        self.flags = {}
        for key, label, default in (
                ("clear_target", "Clear target tables before load", True),
                ("disable_constraints", "Disable foreign keys during load", True),
                ("preserve_identity", "Preserve primary keys (IDENTITY_INSERT)", True),
                ("reseed_identity", "Reseed identity columns afterwards", True),
                ("fast_executemany", "Fast bulk insert", True),
                ("resume", "Resume from last checkpoint", False),
                ("dry_run", "Dry run — read and convert, write nothing", False)):
            var = tk.BooleanVar(value=default)
            ttk.Checkbutton(right, text=label, variable=var,
                            style="Panel.TCheckbutton").pack(anchor="w")
            self.flags[key] = var

        controls = ttk.Frame(block.body)
        controls.pack(fill="x", pady=(0, SP["md"]))
        self.start_btn = Button(controls, "Start transfer", self._start,
                                variant="target", icon="▶", font=self.fonts["body"])
        self.start_btn.pack(side="left")
        self.pause_btn = Button(controls, "Pause", self._pause, variant="ghost",
                                icon="⏸", font=self.fonts["body"])
        self.pause_btn.pack(side="left", padx=(SP["sm"], 0))
        self.pause_btn.set_enabled(False)
        self.stop_btn = Button(controls, "Stop", self._stop, variant="danger",
                               icon="⏹", font=self.fonts["body"])
        self.stop_btn.pack(side="left", padx=(SP["sm"], 0))
        self.stop_btn.set_enabled(False)
        self.throughput = ttk.Label(controls, text="", style="Data.TLabel")
        self.throughput.pack(side="right")

        self.overall = Meter(block.body, height=8, tone="target")
        self.overall.pack(fill="x", pady=(0, SP["md"]))

        wrap = ttk.Frame(block.body)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("table", "moved", "total",
                                                "pct", "rate", "state"),
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in (
                ("table", "Table", 300, "w"), ("moved", "Moved", 120, "e"),
                ("total", "Expected", 120, "e"), ("pct", "%", 70, "e"),
                ("rate", "Rows/sec", 110, "e"), ("state", "State", 160, "w")):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "table"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("done", foreground=C["ok"])
        self.tree.tag_configure("failed", foreground=C["stop"])
        self.tree.tag_configure("busy", foreground=C["text"])

        self.next_btn = Button(foot, "Continue to verification",
                               lambda: (self.app.unlock(4), self.app._goto(4)),
                               variant="primary", font=self.fonts["body"])
        self.next_btn.pack(side="right")
        self.next_btn.set_enabled(False)

        self._moved = {}
        self._expected = 0
        self._table_count = 0

    # -- lifecycle ---------------------------------------------------------
    def on_show(self):
        """A killed run leaves a checkpoint. Say so, and arm the resume, rather
        than letting someone reload rows they already moved."""
        self.flags["resume"].set(False)
        if not self.app.conns:
            return
        candidate = Transport(self.app.conns, self.app.tables, self.app.options)
        try:
            saved = candidate.load_checkpoint()
        except ValueError as exc:
            self.resume_note.configure(text=str(exc))
            return
        if not saved.get("tables"):
            self.resume_note.configure(text="Fresh transfer. Resume is optional and requires a matching checkpoint.")
            return
        matches = saved.get("context") == candidate.resume_context and not saved.get("errors")
        self.resume_note.configure(text=(
            "Matching checkpoint available. Select Resume to skip completed tables and clear/reload interrupted tables."
            if matches else
            "An older or different checkpoint exists. Resume is off. A fresh transfer follows the clearing option above."))

    def reset(self):
        self.tree.delete(*self.tree.get_children())
        self.resume_note.configure(text="")
        self.flags["resume"].set(False)
        self.overall.set(0)
        self.throughput.configure(text="")
        self._moved, self._expected, self._table_count = {}, 0, 0
        self.start_btn.set_enabled(True)
        self.pause_btn.set_enabled(False)
        self.stop_btn.set_enabled(False)
        self.next_btn.set_enabled(False)

    def _collect_options(self) -> Options:
        o = copy.deepcopy(self.app.options)
        o.chunk_size = int(self.chunk.get())
        o.workers = int(self.workers.get())
        o.on_row_error = self.on_error.get()
        for key, var in self.flags.items():
            setattr(o, key, bool(var.get()))
        o.validate()
        self.app.options = o
        return o

    def _start(self):
        if self.app.conns is None:
            return
        if self.app.operation_active():
            messagebox.showwarning("Operation running", "Wait for the current operation to finish.")
            return
        try:
            options = self._collect_options()
        except ValueError as exc:
            messagebox.showerror("Invalid transfer options", str(exc))
            return
        if self.app._preflight_signature != self.app.plan_signature():
            messagebox.showwarning("Preflight required", "The database or selection changed. Run preflight and continue from there.")
            self.app._goto(2)
            return
        candidate = Transport(self.app.conns, self.app.tables, options)
        if options.resume and not options.dry_run:
            try:
                saved = candidate.load_checkpoint()
                if not saved.get("tables") or saved.get("context") != candidate.resume_context or saved.get("errors"):
                    raise ValueError("This checkpoint does not match. Turn off Resume, review Clear target tables, and start a fresh transfer.")
            except ValueError as exc:
                messagebox.showwarning("Cannot resume", str(exc))
                return
        chosen = [t for t in self.app.tables if t.selected]
        if not options.clear_target and options.with_data and not options.resume and any(t.target_rows for t in chosen):
            messagebox.showwarning("Target contains rows", "Enable Clear target tables or choose an empty target. This tool does not merge existing rows.")
            return
        rows = sum(t.source_rows for t in chosen)
        fresh = sum(1 for t in chosen if not t.target_exists)
        lines = []
        if options.with_schema:
            lines.append(f"Create {fresh} missing table(s)"
                         if fresh else "Create no tables — all of them exist")
        if options.with_data:
            verb = "Read and convert" if options.dry_run else "Write"
            lines.append(f"{verb} {rows:,} rows across {len(chosen)} tables")
            if options.clear_target and not options.dry_run:
                lines.append("Delete existing rows in those target tables first")
            if not options.preserve_identity:
                lines.append("Generate new identity values; foreign-key references are NOT remapped")
        if not messagebox.askyesno(
                "Confirm transfer",
                f"Into {self.app.ms.database} on {self.app.ms.server}:\n\n"
                + "\n".join(f"  \u2022 {line}" for line in lines)
                + "\n\nContinue?"):
            return

        self.app.verify_report = None
        self.app.stages[4].reset()
        if self.app.job is None:
            self.app.start_job(self.app.activity)
        self.tree.delete(*self.tree.get_children())
        self._moved = {}
        self._expected = max(rows, 1)
        self._table_count = len(chosen)
        for table in chosen:
            expected = f"{table.source_rows:,}" if options.with_data else "—"
            self.tree.insert("", "end", iid=table.name,
                             values=(table.name, "0", expected,
                                     "0%", "—", "queued"))

        self.app.transport = Transport(self.app.conns, self.app.tables, options)
        self.app.transport.start()
        self.start_btn.set_enabled(False)
        self.pause_btn.set_enabled(True)
        self.stop_btn.set_enabled(True)
        self.app.header.set_flowing(True)
        self.app.rail.set_state(3, "busy")
        self.app.say(f"Transfer started ({options.mode})."
                     + (" Dry run — nothing will be written."
                        if options.dry_run else ""), "step")

    def _pause(self):
        transport = self.app.transport
        if not transport:
            return
        if transport.running.is_set():
            transport.pause()
            self.pause_btn.set_text("Resume")
            self.app.header.set_flowing(False)
        else:
            transport.resume()
            self.pause_btn.set_text("Pause")
            self.app.header.set_flowing(True)

    def _stop(self):
        transport = self.app.transport
        if transport and messagebox.askyesno(
                "Stop transfer",
                "Stop after the current batch? Completed tables are kept and "
                "recorded in the checkpoint, so you can resume later."):
            transport.stop()
            self.stop_btn.set_enabled(False)

    # -- event handling ----------------------------------------------------
    def handle(self, event: dict):
        kind = event["kind"]
        if kind == "log":
            self.app.say(event["message"], event["level"])
        elif kind == "phase":
            self.app.header.set_caption(
                {"schema": "creating tables",
                 "transfer": "moving rows",
                 "constraints": "revalidating foreign keys"}.get(event["name"], ""))
        elif kind == "table_start":
            self._set(event["table"], state="reading")
            self.app.say(f"{event['table']}: reading "
                         f"{event['total']:,} source rows.", "step")
        elif kind == "progress":
            self._progress(event)
        elif kind == "table_done":
            self._table_done(event)
        elif kind == "table_error":
            self._set(event["table"], state="failed", tag="failed")
        elif kind == "finished":
            self._finished(event)

    def _set(self, table: str, **values):
        if not self.tree.exists(table):
            return
        current = list(self.tree.item(table, "values"))
        keys = ["table", "moved", "total", "pct", "rate", "state"]
        for key, value in values.items():
            if key in keys:
                current[keys.index(key)] = value
        tag = values.get("tag")
        self.tree.item(table, values=current, tags=(tag,) if tag else ())

    def _progress(self, event):
        self._moved[event["table"]] = event["done"]
        pct = event["done"] / max(event["total"], 1)
        self._set(event["table"], moved=f"{event['done']:,}",
                  pct=f"{pct * 100:.0f}%", rate=f"{event['rate']:,.0f}",
                  state="moving", tag="busy")
        total_moved = sum(self._moved.values())
        self.overall.set(min(total_moved / self._expected, 1.0))
        self.throughput.configure(
            text=f"{total_moved:,} / {self._expected:,} rows")
        self.app.header.set_caption(f"{event['table']} · {event['done']:,} rows")

    def _table_done(self, event):
        self._moved[event["table"]] = event["rows"]
        rate = event["rows"] / max(event["seconds"], 1e-6)
        self._set(event["table"], moved=f"{event['rows']:,}", pct="100%",
                  rate=f"{rate:,.0f}", state=event["status"],
                  tag="done" if event["status"] == "done" else "failed")
        self.app.say(
            f"[{len(self._moved)}/{self._table_count}] {event['table']}: "
            f"{event['rows']:,} rows in {event['seconds']:.1f}s "
            f"({rate:,.0f} rows/sec) \u2014 {event['status']}.",
            "ok" if event["status"] == "done" else "warn")

    def _finished(self, event):
        # Transport owns immutable plan copies. Bring observed target metadata
        # back to the UI so a second run does not recreate tables just built.
        transport = self.app.transport
        observed = {t.name: t for t in getattr(transport, "tables", [])}
        for table in self.app.tables:
            if table.name in observed:
                table.target_exists = observed[table.name].target_exists
                table.target_has_identity = observed[table.name].target_has_identity
                result = transport.results.get(table.name, {})
                if (result.get("status") == "done" and not result.get("schema_only")
                        and not event.get("dry_run", self.app.options.dry_run)):
                    table.target_rows = result.get("rows", table.target_rows)
        self.app.header.set_flowing(False)
        self.app.header.set_caption("")
        self.start_btn.set_enabled(True)
        self.pause_btn.set_enabled(False)
        self.pause_btn.set_text("Pause")
        self.stop_btn.set_enabled(False)
        self.overall.set(1.0 if not event["cancelled"] else
                         sum(self._moved.values()) / self._expected,
                         tone="warn" if event["cancelled"] else "ok")
        self.app.transfer_summary = {
            "tables_ok": event["tables_ok"], "tables_total": event["tables_total"],
            "rows": event["rows"], "seconds": round(event["elapsed"], 2),
            "cancelled": event["cancelled"],
            "error": event.get("error", ""),
            "dry_run": event.get("dry_run", self.app.options.dry_run),
            "detail": dict(self.app.transport.results) if self.app.transport else {},
        }
        clean = (event["tables_total"] > 0 and event["tables_ok"] == event["tables_total"]
                 and not event["cancelled"] and not event.get("error"))
        self.app.rail.set_state(3, "done" if clean else "fail")
        self.app.say(
            f"Transfer finished: {event['rows']:,} rows, "
            f"{event['tables_ok']}/{event['tables_total']} tables, "
            f"{event['elapsed']:.1f}s.", "ok" if clean else "warn")
        self.next_btn.set_enabled(clean and not self.app.options.dry_run)
        self.app.transport = None
        self.app.sync_job()

        # A migration is not finished until it has been checked. Cancelled and
        # schema-only runs have nothing to count, so they are the exceptions.
        if event.get("error"):
            self.app.say("Transfer failed: " + event["error"], "error")
            if not getattr(self.app, "_closing", False):
                messagebox.showerror("Transfer failed", event["error"])
            self.app.finish_job("failed")
            return
        if self.app.options.dry_run:
            self.app.say("Dry run finished: no target data was written. Run a real transfer before verification.", "info")
            self.app.finish_job("dry_run" if clean else "failed")
            return
        if event["cancelled"]:
            self.app.say("Transfer was stopped, so verification is not run "
                         "automatically. Resume it, or verify by hand.", "warn")
            self.app.finish_job("stopped")
            return
        if not self.app.options.with_data:
            self.app.say("Schema-only run: no rows to verify.", "info")
            self.app.finish_job("passed" if clean else "failed")
            return
        if not clean:
            self.app.say("Transfer incomplete. Review failed or partial tables before verification.", "error")
            self.app.finish_job("failed")
            return
        self.app.unlock(4)
        self.app._goto(4)
        self.app.say("Verifying automatically now the transfer is done.", "step")
        self.app.stages[4].run()


# ===========================================================================
class VerifyStage(Stage):
    # Counts first and default: it is the one that finishes on a large database
    # while somebody is watching. The deeper levels are a deliberate choice.
    LEVELS = {
        "Row counts only — fastest": "counts",
        "Row counts and column profile": "profile",
        "Full — profile, then read rows back and compare": "full",
    }

    def build(self):
        block = section(self, "Verify", self.fonts,
                        "Row counts, then every column's nulls, extremes, totals "
                        "and text lengths, then — at full — the rows themselves "
                        "read back and compared. Plus a sweep for untrusted "
                        "foreign keys and identity seeds below their maximum "
                        "key. Any pair on these servers can be checked, "
                        "migrated here or not.")
        block.pack(fill="both", expand=True)

        foot = ttk.Frame(block.body)
        foot.pack(side="bottom", fill="x", pady=(SP["sm"], 0))
        self.next_btn = Button(foot, "Continue to cutover",
                               lambda: (self.app.unlock(5), self.app._goto(5)),
                               variant="primary", font=self.fonts["body"])
        self.next_btn.pack(side="right")
        self.next_btn.set_enabled(False)

        self.footnote = ttk.Label(block.body, text="", style="Muted.TLabel",
                                  wraplength=900, justify="left")
        self.footnote.pack(side="bottom", anchor="w", pady=(SP["md"], 0))

        self.picker = DatabasePicker(block.body, self.app, "Run verification",
                                     self.run)
        self.picker.pack(fill="x", pady=(0, SP["md"]))

        bar = ttk.Frame(block.body)
        bar.pack(fill="x", pady=(0, SP["md"]))
        self.level = Field(bar, "How hard to look", self.fonts, kind="combo",
                           style_prefix="Root", width=44,
                           values=list(self.LEVELS))
        self.level.set(next(iter(self.LEVELS)))
        self.level.pack(side="left")
        self.deep = tk.BooleanVar(value=True)      # kept for older callers
        self.verdict = Pill(bar, "not run", "muted", font=self.fonts["small"])
        self.verdict.pack(side="left", padx=(SP["md"], 0))
        Button(bar, "Export report", self._export, variant="quiet", icon="↗",
               font=self.fonts["small"], height=30).pack(side="right")

        wrap = ttk.Frame(block.body)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("table", "source", "target",
                                                "status", "detail"),
                                 show="tree headings", selectmode="browse")
        self.tree.column("#0", width=26, stretch=False)
        for key, title, width, anchor, stretch in (
                ("table", "Table", 280, "w", False),
                ("source", "Source rows", 130, "e", False),
                ("target", "Target rows", 130, "e", False),
                ("status", "Result", 100, "w", False),
                ("detail", "Detail", 460, "w", True)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground=C["ok"])
        self.tree.tag_configure("fail", foreground=C["stop"])
        self.tree.tag_configure("skip", foreground=C["muted"])


    def reset(self):
        self.tree.delete(*self.tree.get_children())
        self.footnote.configure(text="")
        self.verdict.set("not run", "muted")
        self.next_btn.set_enabled(False)

    def on_show(self):
        self.picker.sync()

    def run(self):
        if self.app.conns is None or not self.picker.require():
            return
        self.reset()
        self.app.verify_report = None
        conns, options = self.app.conns, self.app.options
        level = self.LEVELS.get(self.level.get(), "profile")
        pair = (self.app.pg.dbname, self.app.ms.database)
        known = self.app.tables if self.app.loaded_pair == pair else []

        def work():
            progress = lambda m: self.after(0, self.app.say, m)   # noqa: E731
            tables = known or Introspector(conns).discover(progress=progress)
            return tables, Verifier(conns, tables, options).run(
                level=level, progress=progress)

        def done(payload):
            tables, report = payload
            self.app.tables = tables
            self.app.loaded_pair = pair
            self._loaded(report)

        self.app.say(f"Verifying {pair[0]} \u2192 {pair[1]} — "
                     f"{Verifier.LEVELS[level]}.", "step")
        self.app.run_async(work, on_done=done, message="Verifying")

    def _loaded(self, report: dict):
        report = dict(report)
        if report.get("tables_checked", sum(r["status"] != "skip" for r in report["tables"])) == 0:
            report["passed"] = False
            report["incomplete"] = True
        self.app.verify_report = report
        self.tree.delete(*self.tree.get_children())
        for row in report["tables"]:
            parent = self.tree.insert(
                "", "end", values=(row["table"], f"{row['source']:,}",
                                   f"{row['target']:,}", row["status"],
                                   row.get("detail", "")),
                tags=(row["status"],), open=row["status"] == "fail")
            # Every failing check by name, under the table it belongs to.
            for drift in row.get("profile", []):
                self.tree.insert(parent, "end",
                                 values=("", drift["source"], drift["target"],
                                         "differs", drift["check"]),
                                 tags=("fail",))
            for miss in row.get("sample_mismatches", []):
                self.tree.insert(
                    parent, "end",
                    values=("", miss["source"], miss["target"], "row differs",
                            f"key {miss['key']}, column {miss['column']}"),
                    tags=("fail",))
        notes = []
        if report["untrusted_foreign_keys"]:
            notes.append("Untrusted or disabled foreign keys: "
                         + ", ".join(report["untrusted_foreign_keys"])
                         + ". The optimizer ignores these until they are "
                           "revalidated with WITH CHECK CHECK CONSTRAINT.")
        if report["identity_problems"]:
            notes.append("Identity seeds below their maximum key: "
                         + "; ".join(report["identity_problems"])
                         + ". The next insert will collide.")
        self.footnote.configure(text="\n\n".join(notes))
        passed = report["passed"]
        self.verdict.set("passed" if passed else "failed",
                         "ok" if passed else "stop")
        self.app.rail.set_state(4, "done" if passed else "fail")

        # Every mismatch by name in the log, so the record does not depend on
        # anyone reading the table before the window closes.
        for row in report["tables"]:
            if row["status"] not in ("ok", "skip"):
                self.app.say(f"{row['table']}: {row['status']} \u2014 source "
                             f"{row['source']:,}, target {row['target']:,}. "
                             f"{row.get('detail', '')}".strip(), "error")
                for drift in row.get("profile", []):
                    self.app.say(f"  {row['table']} {drift['check']}: "
                                 f"source {drift['source']}, "
                                 f"target {drift['target']}", "error")
                for miss in row.get("sample_mismatches", []):
                    self.app.say(f"  {row['table']} row {miss['key']} "
                                 f"column {miss['column']}: "
                                 f"source {miss['source']}, "
                                 f"target {miss['target']}", "error")
        for note in notes:
            self.app.say(note, "warn")
        self.app.say(
            f"Verification {'passed' if passed else 'failed'} for "
            f"{self.app.pg.dbname} \u2192 {self.app.ms.database} at level "
            f"'{report.get('level', '?')}': "
            f"{report.get('tables_checked', 0)} tables checked, "
            f"{report['rows_verified']:,} rows, "
            f"{report.get('rows_sampled', 0):,} rows read back, "
            f"{report.get('tables_failed', 0)} tables failed.",
            "ok" if passed else "error")
        self.app.sync_job()
        self.app.finish_job("passed" if passed else "failed")
        self.next_btn.set_enabled(self.app.activity == "migrate" and passed)
        if self.app.activity == "migrate" and passed:
            self.app.unlock(5)
            self.app.say("Migration complete and verified. Continue to Cutover "
                         "to point the application at the new database.", "ok")

    def _export(self):
        if not self.app.verify_report and not self.app.transfer_summary:
            messagebox.showinfo("Nothing to export",
                                "Run a transfer or verification first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON report", "*.json")],
            initialfile=f"pgbridge_{self.app.ms.database or 'migration'}_"
                        f"{datetime.datetime.now():%Y%m%d_%H%M}.json")
        if not path:
            return
        write_report(path, self.app.pg, self.app.ms, self.app.options,
                     self.app.issues, self.app.transfer_summary,
                     self.app.verify_report, self.app.tables)
        log_path = os.path.splitext(path)[0] + "_activity.log"
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(self.app.log.dump())
        self.app.say(f"Report written to {path}", "ok")


# ===========================================================================
class CutoverStage(Stage):
    """Point the application at the database that was just migrated.

    The migration is not finished when the rows land; it is finished when the
    app stops talking to PostgreSQL. Everything here writes to somebody's
    project, so nothing is guessed: the file is chosen, the change is previewed,
    and the original is backed up before a byte is written.
    """

    WHERE = {"In a .env file, read with python-decouple (recommended)": True,
             "Written directly into settings.py": False}

    def build(self):
        block = section(self, "Cutover", self.fonts,
                        "Prepare application configuration for SQL Server. Use the Django "
                        "workflow below or open a framework handoff. Rehearse before switching traffic.")
        block.pack(fill="both", expand=True)

        foot = ttk.Frame(block.body)
        foot.pack(side="bottom", fill="x", pady=(SP["md"], 0))

        # Which database the project should point at. A migration already knows
        # it; arriving here on its own, nothing does, and an empty DB_NAME in a
        # patched settings.py is worse than no patch at all.
        pick = ttk.Frame(block.body)
        pick.pack(fill="x", pady=(0, SP["md"]))
        self.database = Field(pick, "Database the app should use", self.fonts,
                              kind="combo", searchable=True, style_prefix="Root", width=26)
        self.database.widget.configure(state="normal")
        self.database.widget.bind("<<ComboboxSelected>>",
                                  lambda _e: self._set_database())
        # The trace pushes the choice into the shared config. Programmatic
        # writes — reset, or prefilling from a migration — must not fire it, or
        # clearing the box erases the very database being carried over.
        self._syncing = False
        self.database.var.trace_add("write", lambda *_: self._set_database())
        self.database.pack(side="left")
        Button(pick, "Framework handoff", self._framework_handoff,
               variant="target", font=self.fonts["small"]).pack(side="right", pady=(19, 0))
        holder = ttk.Frame(pick)
        holder.pack(side="left", padx=(SP["md"], 0), pady=(19, 0))
        Button(holder, "List databases", self._load_databases, variant="quiet",
               font=self.fonts["small"], height=28).pack(side="left")
        self.db_note = ttk.Label(pick, text="", style="Muted.TLabel")
        self.db_note.pack(side="left", padx=(SP["md"], 0), pady=(19, 0))

        form = ttk.Frame(block.body, style="Panel.TFrame", padding=SP["md"])
        form.pack(fill="x", pady=(0, SP["md"]))

        self.file_entries = []
        self.file_buttons = []
        self.picker_button_width = 210
        self.settings_path = self._picker(
            form, "Django settings.py", "Choose settings.py…", self._pick_settings)
        self.where = Field(form, "Where the credentials go", self.fonts,
                           kind="combo", style_prefix="Panel", width=52,
                           values=list(self.WHERE))
        self.where.set(next(iter(self.WHERE)))
        self.where.widget.bind("<<ComboboxSelected>>", lambda _e: self._refresh())
        self.where.pack(fill="x", padx=(0, self.picker_button_width + SP["sm"]),
                        pady=(0, SP["sm"]))

        self.env_path = self._picker(
            form, ".env file to create or update", "Choose .env…", self._pick_env)
        self.venv_path = self._picker(
            form, "The project's virtual environment",
            "Choose env folder…", self._pick_venv)

        checks = ttk.Frame(form, style="Panel.TFrame")
        checks.pack(fill="x", pady=(SP["sm"], 0))
        self.pkg_note = ttk.Label(checks, text="", style="PanelMuted.TLabel",
                                  wraplength=470, justify="left")
        self.pkg_note.pack(side="left", fill="x", expand=True)
        self.install_btn = Button(checks, "Install missing packages",
                                  self._install, variant="ghost", width=self.picker_button_width,
                                  font=self.fonts["small"], height=38,
                                  background=C["panel"])
        self.install_btn.pack(side="right")
        self.install_btn.set_enabled(False)

        prev = ttk.Frame(block.body)
        prev.pack(fill="both", expand=True)
        ttk.Label(prev, text="Preview", style="Muted.TLabel").pack(anchor="w")
        self.preview = tk.Text(prev, wrap="none", bd=0, background=C["panel"],
                               foreground=C["text"], font=self.fonts["data_sm"],
                               padx=10, pady=8, highlightthickness=0,
                               state="disabled")
        psb = ttk.Scrollbar(prev, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=psb.set)
        self.preview.pack(side="left", fill="both", expand=True, pady=(4, 0))
        psb.pack(side="right", fill="y")
        self.preview.tag_configure("old", foreground=C["faint"])
        self.preview.tag_configure("new", foreground=C["ok"])
        self.preview.tag_configure("head", foreground=C["source"],
                                   font=self.fonts["strong"])

        self.status = ttk.Label(foot, text="Choose a settings.py to begin.",
                                style="Muted.TLabel")
        self.status.pack(side="left")
        self.apply_btn = Button(foot, "Patch settings.py", self._apply,
                                variant="target", font=self.fonts["body"])
        self.apply_btn.pack(side="right")
        self.apply_btn.set_enabled(False)
        self.test_btn = Button(foot, "Test app connection", self._test_connection,
                               variant="ghost", font=self.fonts["body"])
        self.test_btn.pack(side="right", padx=(0, SP["sm"]))
        self.test_btn.set_enabled(False)
        self.packages: dict[str, bool] = {}

    def _framework_handoff(self):
        from .handoff import FRAMEWORKS, render_handoff
        window = tk.Toplevel(self)
        window.title("Application handoff · pgbridge")
        window.geometry("850x650")
        window.minsize(660, 450)
        window.configure(background=C["bg"])
        window.transient(self.app)
        panel = ttk.Frame(window, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="Prepare your application", style="Display.TLabel").pack(anchor="w")
        ttk.Label(panel, text="Reviewable templates · secrets supplied by your deployment environment",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 16))
        framework = Field(panel, "Application framework", self.fonts, kind="combo",
                          style_prefix="Root", values=list(FRAMEWORKS), width=42)
        framework.set(next(iter(FRAMEWORKS)))
        framework.pack(anchor="w")
        preview = tk.Text(panel, background=C["panel"], foreground=C["text"],
                          insertbackground=C["text"], font=self.fonts["data_sm"],
                          wrap="word", padx=16, pady=16, relief="flat")
        actions = ttk.Frame(panel)
        actions.pack(side="bottom", fill="x", pady=(14, 0))
        scrollbar = ttk.Scrollbar(panel, command=preview.yview)
        scrollbar.pack(side="right", fill="y", pady=(16, 0))
        preview.configure(yscrollcommand=scrollbar.set)
        preview.pack(fill="both", expand=True, pady=(16, 0))
        def content():
            return render_handoff(framework.get(), self.app.ms)
        def refresh(_event=None):
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.insert("1.0", content())
            preview.configure(state="disabled")
        def save():
            path = filedialog.asksaveasfilename(parent=window, title="Export application handoff",
                                               defaultextension=".md", initialfile="sqlserver-handoff.md")
            if path:
                try:
                    with open(path, "w", encoding="utf-8") as stream:
                        stream.write(content())
                except OSError as exc:
                    messagebox.showerror("Export failed", str(exc), parent=window)
                    return
                self.app.say("Exported framework handoff; application validation is still required.")
        Button(actions, "Export handoff", save, variant="primary", font=self.fonts["body"]).pack(side="right")
        ttk.Label(actions, text="Configuration only · does not switch traffic", style="Muted.TLabel").pack(side="left")
        framework.widget.bind("<<ComboboxSelected>>", refresh)
        refresh()

    def reset(self):
        self.settings_path.set("")
        self.env_path.set("")
        self.venv_path.set("")
        self._show_database("")
        self.where.set(next(iter(self.WHERE)))   # back to the recommended route
        self.packages = {}
        self.pkg_note.configure(text="")
        self.db_note.configure(text="")
        self.install_btn.set_enabled(False)
        self.apply_btn.set_enabled(False)
        self.test_btn.set_enabled(False)

    def _picker(self, parent, label: str, button: str, command):
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=(0, SP["sm"]))
        ttk.Label(row, text=label, style="PanelMuted.TLabel").pack(anchor="w")
        line = ttk.Frame(row, style="Panel.TFrame")
        line.pack(fill="x")
        var = tk.StringVar()
        entry = ttk.Entry(line, textvariable=var)
        entry.pack(side="left", fill="x", expand=True)
        browse = Button(line, button, command, variant="ghost", font=self.fonts["small"],
                        width=self.picker_button_width, height=38, background=C["panel"])
        browse.pack(side="left", padx=(SP["sm"], 0))
        self.file_entries.append(entry)
        self.file_buttons.append(browse)
        var.trace_add("write", lambda *_: self._refresh())
        return var

    # -- choosing ----------------------------------------------------------
    def _pick_settings(self):
        path = filedialog.askopenfilename(
            title="Select the Django settings module",
            filetypes=[("Python", "*.py"), ("All files", "*")])
        if not path:
            return
        self.settings_path.set(path)
        if not self.env_path.get():
            self.env_path.set(cutover.guess_env_path(path))

    def _pick_env(self):
        path = filedialog.asksaveasfilename(
            title="Select or name the .env file", defaultextension="",
            initialfile=".env",
            initialdir=os.path.dirname(self.env_path.get() or
                                       self.settings_path.get() or "."))
        if path:
            self.env_path.set(path)

    def _pick_venv(self):
        folder = filedialog.askdirectory(
            title="Select the project's virtual environment folder")
        if not folder:
            return
        self.venv_path.set(folder)
        self._check_packages()

    # -- environment -------------------------------------------------------
    def _needed(self) -> list[str]:
        needed = ["mssql-django"]
        if self.WHERE.get(self.where.get(), True):
            needed.insert(0, "python-decouple")
        return needed

    def _check_packages(self):
        folder = self.venv_path.get().strip()
        python = cutover.venv_python(folder)
        if not python:
            self.packages = {}
            self.pkg_note.configure(
                text="No Python found in that folder. Point at the environment "
                     "itself (the one holding bin/ or Scripts/).")
            self.install_btn.set_enabled(False)
            return
        needed = self._needed()

        def work():
            return cutover.describe_python(python), \
                cutover.check_packages(python, tuple(needed))

        def done(payload):
            version, found = payload
            self.packages = found
            missing = [n for n, ok in found.items() if not ok]
            have = [n for n, ok in found.items() if ok]
            parts = [f"Python {version}"]
            if have:
                parts.append("installed: " + ", ".join(have))
            if missing:
                parts.append("MISSING: " + ", ".join(missing))
            self.pkg_note.configure(text="  ·  ".join(parts))
            self.install_btn.set_enabled(bool(missing))
            self.app.say(f"{folder}: Python {version}, "
                         + (f"missing {', '.join(missing)}." if missing
                            else "everything needed is installed."),
                         "warn" if missing else "ok")
            self._refresh()

        self.app.run_async(work, on_done=done,
                           message="Checking the project environment")

    def _install(self):
        python = cutover.venv_python(self.venv_path.get().strip())
        missing = [n for n, ok in self.packages.items() if not ok]
        if not python or not missing:
            return
        if not messagebox.askyesno(
                "Install packages",
                f"Install {', '.join(missing)} into\n\n  {python}\n\n"
                "This changes that environment. Continue?"):
            return
        self.app.run_async(
            lambda: cutover.install_packages(python, missing),
            on_done=lambda r: (
                self.app.say(f"pip install {' '.join(missing)}: "
                             + ("done." if r[0] else f"failed. {r[1][-400:]}"),
                             "ok" if r[0] else "error"),
                self._check_packages()),
            message=f"Installing {', '.join(missing)}")

    # -- which database ----------------------------------------------------
    def _set_database(self):
        if self._syncing:
            return
        self.app.ms.database = self.database.get()
        self.app.refresh_endpoints()     # the header names the target too
        self._refresh()

    def _show_database(self, value: str):
        """Put a value in the box without treating it as the user's choice."""
        self._syncing = True
        try:
            self.database.set(value)
        finally:
            self._syncing = False

    def _load_databases(self):
        if self.app.conns is None:
            messagebox.showinfo("Not connected",
                                "Test the connection on Connect first.")
            return
        conns = self.app.conns
        self.app.run_async(
            lambda: conns.probe_target()["databases"],
            on_done=lambda names: (
                self.database.set_values(names),
                self.app.say(f"{len(names)} databases on {self.app.ms.server}.",
                             "ok")),
            message="Listing SQL Server databases")

    # -- preview and apply -------------------------------------------------
    def on_show(self):
        """Arriving from a migration, the database is already decided; arriving
        straight here, it has to be chosen."""
        current = self.app.ms.database
        if current and current not in self.database.widget.cget("values"):
            self.database.set_values([*self.database.widget.cget("values"), current])
        if current:
            self._show_database(current)
        came_from_migration = bool(self.app.transfer_summary or self.app.tables)
        self.db_note.configure(
            text=("carried over from this migration" if came_from_migration
                  and current else
                  "" if current else "choose the database the app should use"))
        self._refresh()

    def _refresh(self):
        path = self.settings_path.get().strip()
        use_env = self.WHERE.get(self.where.get(), True)
        ready = bool(path) and os.path.isfile(path)
        self.apply_btn.set_enabled(ready)
        self.test_btn.set_enabled(ready)
        if not ready:
            self.status.configure(text="Choose a settings.py to begin.")
            self._write_preview([("head", "Nothing selected yet.\n")])
            return

        try:
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
        except OSError as exc:
            self.status.configure(text=str(exc))
            return

        if not self.app.ms.database:
            self.status.configure(
                text="Choose the database the application should point at.")
            self.apply_btn.set_enabled(False)
            self._write_preview([("old", "No database chosen — the patched "
                                         "settings would have an empty NAME.\n")])
            return

        span = cutover.find_databases(source)
        lines: list[tuple[str, str]] = []
        if span is None:
            self.status.configure(
                text="No DATABASES assignment in this file — pick the module "
                     "that defines it.")
            self.apply_btn.set_enabled(False)
            self.test_btn.set_enabled(False)
            self._write_preview([("old", "This file has no top-level "
                                         "DATABASES setting.\n")])
            return
        if cutover.already_patched(source):
            self.status.configure(
                text="This file already carries a pgbridge block. Patching "
                     "again will comment out the current one too.")

        start, end = span
        old = "".join(source.splitlines(keepends=True)[start - 1:end])
        lines.append(("head", f"Will comment out lines {start}–{end}:\n\n"))
        lines += [("old", "# " + ln + "\n") for ln in old.splitlines()]
        lines.append(("head", "\nand write:\n\n"))
        lines += [("new", ln + "\n")
                  for ln in cutover.render_databases(
                      replace(self.app.ms, password="********" if self.app.ms.password else ""),
                      use_env, self.env_path.get().strip()).splitlines()]

        if use_env:
            env = self.env_path.get().strip()
            lines.append(("head", f"\nand set in {env or '(no .env chosen)'}:\n\n"))
            for key, value in cutover.env_values(self.app.ms).items():
                shown = "********" if key == "DB_PASSWORD" and value else value
                lines.append(("new", f"{key}={shown}\n"))
            if not cutover.has_decouple_import(source):
                lines.append(("head", "\nand add: "))
                lines.append(("new", "from decouple import config\n"))

        self._write_preview(lines)
        missing = [n for n, ok in self.packages.items() if not ok]
        self.status.configure(
            text=f"Ready to patch {os.path.basename(path)}"
                 + (f" — but {', '.join(missing)} is not installed in the "
                    "chosen environment" if missing else ""))

    def _write_preview(self, lines: list[tuple[str, str]]):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        for tag, text in lines:
            self.preview.insert("end", text, tag)
        self.preview.see("1.0")
        self.preview.configure(state="disabled")

    def _apply(self):
        if not self.app.ms.database:
            messagebox.showwarning(
                "No database chosen",
                "Choose the database the application should point at.")
            return
        path = self.settings_path.get().strip()
        use_env = self.WHERE.get(self.where.get(), True)
        env = self.env_path.get().strip()
        if use_env and not env:
            messagebox.showwarning("No .env chosen",
                                   "Choose where the .env file should go.")
            return
        missing = [n for n, ok in self.packages.items() if not ok]
        warning = ""
        if missing:
            warning = ("\n\n" + ", ".join(missing) + " is not installed in the "
                       "chosen environment, so the app will not start until it "
                       "is.")
        elif not self.venv_path.get().strip():
            warning = ("\n\nNo environment was checked, so nothing confirms "
                       "mssql-django is installed there.")
        if not messagebox.askyesno(
                "Patch settings",
                f"Rewrite DATABASES in\n\n  {path}\n\nto point at "
                f"{self.app.ms.database} on {self.app.ms.server}"
                + (f",\nwith the credentials in {env}." if use_env else ",\n"
                   "with the credentials written into the file.")
                + "\n\nThe original is backed up alongside it."
                + warning + "\n\nContinue?"):
            return

        try:
            result = cutover.patch_settings(path, self.app.ms, use_env, env_path=env if use_env else "")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not patch settings", str(exc))
            self.app.say(f"Patching {path} failed: {exc}", "error")
            return

        self.app.say(f"Patched {path}: commented out "
                     f"{result['commented_lines']} line(s), backup at "
                     f"{result['backup']}.", "ok")
        if result["added_import"]:
            self.app.say("Added 'from decouple import config' to the imports.",
                         "info")

        if use_env:
            try:
                report = cutover.update_env_file(
                    env, cutover.env_values(self.app.ms))
            except (OSError, ValueError) as exc:
                try:
                    with open(result["backup"], encoding="utf-8") as original:
                        cutover.atomic_write(path, original.read())
                except OSError as restore_error:
                    messagebox.showerror("Restore required",
                        f"Restore settings manually from {result['backup']}: {restore_error}")
                messagebox.showerror("Could not write .env", str(exc))
                self.app.say(f"Writing {env} failed: {exc}", "error")
                return
            self.app.say(
                f"{'Created' if report['created'] else 'Updated'} {env}: "
                f"{len(report['added'])} key(s) added, "
                f"{len(report['updated'])} updated. Permissions set to owner "
                "only — it holds the database password.", "ok")

        self.app.rail.set_state(5, "idle")
        self.app.sync_job()
        self._refresh()
        messagebox.showinfo(
            "Cutover written",
            f"{os.path.basename(path)} now points at {self.app.ms.database}.\n\n"
            f"Backup: {result['backup']}\n\n"
            "You can now click 'Test app connection' to verify that Django connects "
            "to SQL Server before restarting the application.")

    def _test_connection(self):
        path = self.settings_path.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showwarning(
                "No settings file",
                "Choose a valid Django settings.py file first.")
            return

        venv = self.venv_path.get().strip()
        env = self.env_path.get().strip()

        def work():
            return cutover.test_app_connection(
                settings_path=path,
                venv_folder=venv,
                env_path=env,
                timeout=25,
                expected_database=self.app.ms.database,
            )

        def done(result):
            ok, message, info = result
            if ok:
                self.app.rail.set_state(5, "done")
                db = info.get("database", "")
                ver = info.get("version", "")
                self.app.say(message, "ok")
                self.status.configure(text=f"✓ App connected to SQL Server: {db}")
                details = f"Database: {db}\nServer: {ver}" if ver else f"Database: {db}"
                messagebox.showinfo(
                    "App Connection Successful",
                    f"{message}\n\n{details}\n\nThe application is successfully communicating with SQL Server via Django.")
            else:
                self.app.say(message, "error")
                self.status.configure(text=f"✗ {message[:65]}...")
                messagebox.showerror(
                    "Connection Test Failed",
                    f"{message}\n\nCheck your database credentials, environment variables (.env), and ensure required packages (mssql-django, pyodbc) are installed.")

        self.app.run_async(work, on_done=done,
                           message="Testing Django application database connection")

def main():
    App().mainloop()
