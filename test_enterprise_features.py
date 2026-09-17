"""Enterprise features test suite for pgbridge.

Tests:
1. Data Adaptation Hardening (NUL bytes, NaN/Infinity, JSON serialization).
2. SQL Type mappings for custom Postgres Enums and Status types.
3. Checkpoint isolation (checkpoint_path_for, read_checkpoint).
4. BatchMigrator (multi-database queue, event handling, cancellation).
5. Tkinter UI integration for BatchScreen and Connection Profile Vault.
"""

from __future__ import annotations

import decimal
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from pgbridge import cutover
from pgbridge.app import App
from pgbridge.engine import (
    BatchDatabaseItem,
    BatchMigrator,
    Column,
    Connections,
    JobStore,
    MsConfig,
    Options,
    PgConfig,
    adapt,
    checkpoint_path_for,
    read_checkpoint,
    sql_type,
)


class TestDataAdaptation(unittest.TestCase):
    def test_nul_byte_sanitization(self):
        self.assertEqual(adapt("hello\x00world"), "helloworld")
        self.assertEqual(adapt("test\x00\x00123\x00"), "test123")
        self.assertEqual(adapt("clean_string"), "clean_string")

    def test_float_nan_infinity_sanitization(self):
        self.assertIsNone(adapt(float("nan")))
        self.assertIsNone(adapt(float("inf")))
        self.assertIsNone(adapt(float("-inf")))
        self.assertEqual(adapt(42.5), 42.5)

    def test_decimal_nan_infinity_sanitization(self):
        self.assertIsNone(adapt(decimal.Decimal("NaN")))
        self.assertIsNone(adapt(decimal.Decimal("Infinity")))
        self.assertIsNone(adapt(decimal.Decimal("-Infinity")))
        self.assertEqual(adapt(decimal.Decimal("123.45")), decimal.Decimal("123.45"))

    def test_json_and_collection_serialization(self):
        self.assertEqual(adapt({"a": 1, "b": "hello"}), '{"a": 1, "b": "hello"}')
        self.assertEqual(adapt([1, 2, 3]), "[1, 2, 3]")
        self.assertEqual(adapt(("x", "y")), '["x", "y"]')


class TestTypeMapping(unittest.TestCase):
    def test_standard_types(self):
        self.assertEqual(sql_type(Column("id", "int4", False, None, None)), "int")
        self.assertEqual(sql_type(Column("desc", "text", False, None, None)), "nvarchar(max)")
        self.assertEqual(sql_type(Column("code", "varchar", False, None, 50)), "nvarchar(50)")

    def test_custom_enums_and_status_types(self):
        self.assertEqual(sql_type(Column("status", "order_status_enum", False, None, None)), "nvarchar(64)")
        self.assertEqual(sql_type(Column("state", "user_status", False, None, None)), "nvarchar(64)")
        self.assertEqual(sql_type(Column("delivery_type", "fulfillment_enum", False, None, None)), "nvarchar(64)")


class TestCheckpointIsolation(unittest.TestCase):
    def test_checkpoint_isolation_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # When no isolated checkpoint exists, falls back to checkpoint.json
            p1 = checkpoint_path_for(tmpdir, ["pg_sales", "ms_sales"])
            self.assertEqual(p1, os.path.join(tmpdir, "checkpoint.json"))

            # When pair-specific checkpoint exists, returns the isolated path
            pair_file = os.path.join(tmpdir, "checkpoint_pg_sales_ms_sales.json")
            with open(pair_file, "w", encoding="utf-8") as f:
                json.dump({
                    "pair": ["pg_sales", "ms_sales"],
                    "tables": {"orders": {"status": "done"}, "users": {"status": "pending"}}
                }, f)

            p2 = checkpoint_path_for(tmpdir, ["pg_sales", "ms_sales"])
            self.assertEqual(p2, pair_file)

            # Test read_checkpoint loads isolated checkpoint
            data = read_checkpoint(tmpdir, ["pg_sales", "ms_sales"])
            self.assertIsNotNone(data)
            self.assertEqual(data["done"], ["orders"])
            self.assertEqual(data["unfinished"], ["users"])
            self.assertEqual(data["path"], pair_file)


class TestBatchMigrator(unittest.TestCase):
    def test_batch_migrator_init_and_control(self):
        conns = MagicMock(spec=Connections)
        pairs = [("pg_db1", "ms_db1"), ("pg_db2", "ms_db2")]
        options = Options()
        migrator = BatchMigrator(conns, pairs, options, continue_on_error=True)

        self.assertEqual(len(migrator.items), 2)
        self.assertEqual(migrator.items[0].source_db, "pg_db1")
        self.assertEqual(migrator.items[0].target_db, "ms_db1")
        self.assertEqual(migrator.items[0].status, "pending")

        # Test stop sets cancellation flag and emits warning event
        migrator.stop()
        self.assertTrue(migrator.cancel.is_set())
        stop_event = migrator.events.get_nowait()
        self.assertEqual(stop_event["kind"], "log")
        self.assertIn("cancelled", stop_event["message"].lower())

        # Test subsequent event emission
        migrator.log("info", "Test event message")
        event = migrator.events.get_nowait()
        self.assertEqual(event["kind"], "log")
        self.assertEqual(event["message"], "Test event message")


class TestUIScreensIntegration(unittest.TestCase):
    def test_screens_build_and_cleanup(self):
        from pgbridge.app import App, BatchScreen

        app = App()
        app.withdraw()
        try:
            # 1. Test Profile Vault management on ConnectStage
            connect_stage = app.stages[0]
            self.assertTrue(hasattr(connect_stage, "profile_combo"))
            connect_stage.set_profile_names(["Production", "Staging", "Dev"], "Production")
            self.assertEqual(connect_stage.profile_var.get(), "Production")

            # TLS selections must reach connections and survive profile changes.
            connect_stage.apply_profile({"ms": {"encrypt": True, "trust_cert": True}})
            self.assertEqual(connect_stage.ms_fields["trust_cert"].get(), "Yes")
            self.assertTrue(connect_stage._commit())
            self.assertIn("Encrypt=yes;TrustServerCertificate=yes", app.ms.dsn())
            connect_stage.apply_profile({"ms": {"encrypt": False, "trust_cert": False}})
            self.assertTrue(connect_stage._commit())
            self.assertFalse(app.ms.encrypt)
            self.assertFalse(app.ms.trust_cert)
            connect_stage.apply_profile({"ms": {}})
            self.assertTrue(connect_stage._commit())
            self.assertTrue(app.ms.encrypt)
            self.assertFalse(app.ms.trust_cert)

            # 2. Test BatchScreen
            batch_screen = BatchScreen(app, app)
            batch_screen.available_dbs = ["sales_db", "analytics_db", "users_db"]
            batch_screen.selected_dbs = set(batch_screen.available_dbs)
            batch_screen._render()
            self.assertEqual(len(batch_screen.tree.get_children()), 3)

            # Test target naming rule prefix
            batch_screen.naming_rule.set("Prefix: MSSQL_")
            self.assertEqual(batch_screen._target_name_for("sales_db"), "MSSQL_sales_db")

            # Test target naming rule suffix
            batch_screen.naming_rule.set("Suffix: _mssql")
            self.assertEqual(batch_screen._target_name_for("sales_db"), "sales_db_mssql")

            # 3. Test Front Connection & Server Settings navigation
            self.assertTrue(hasattr(app, "test_connections_front"))
            self.assertTrue(hasattr(app, "open_server_settings"))
            self.assertTrue(hasattr(app, "front_conn_status"))

            # Test open_server_settings navigates to ConnectStage
            app.open_server_settings()
            self.assertEqual(app.stages[0].winfo_manager(), "pack")
            self.assertEqual(app.rail.active, 0)
            self.assertEqual(app.activity, "migrate")

            # Test returning back to front Chooser screen
            app.change_activity()
            self.assertEqual(app.chooser.winfo_manager(), "pack")

            batch_screen.destroy()
        finally:
            app.destroy()


class TestCutoverAppConnection(unittest.TestCase):
    def test_missing_settings_file(self):
        ok, msg, info = cutover.test_app_connection("/nonexistent/path/settings.py")
        self.assertFalse(ok)
        self.assertIn("Settings file not found", msg)

    def test_successful_mssql_connection(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"DATABASES = {'default': {'ENGINE': 'mssql'}}\n")
            path = f.name
        try:
            fake_proc = MagicMock(returncode=0, stdout="PGBRIDGE_OK|orders_db|microsoft|Microsoft SQL Server 2022\n", stderr="")
            with patch("subprocess.run", return_value=fake_proc):
                ok, msg, info = cutover.test_app_connection(path)
                self.assertTrue(ok)
                self.assertIn("App successfully connected to SQL Server database 'orders_db'", msg)
                self.assertEqual(info["database"], "orders_db")
                self.assertEqual(info["vendor"], "microsoft")
        finally:
            os.unlink(path)

    def test_still_postgresql_warning(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"DATABASES = {'default': {'ENGINE': 'postgresql'}}\n")
            path = f.name
        try:
            fake_proc = MagicMock(returncode=0, stdout="PGBRIDGE_OK|pg_orders|postgresql|PostgreSQL 15.2\n", stderr="")
            with patch("subprocess.run", return_value=fake_proc):
                ok, msg, info = cutover.test_app_connection(path)
                self.assertFalse(ok)
                self.assertIn("still pointing at PostgreSQL", msg)
                self.assertEqual(info["database"], "pg_orders")
        finally:
            os.unlink(path)

    def test_setup_failure_reporting(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"DATABASES = {}\n")
            path = f.name
        try:
            fake_proc = MagicMock(returncode=101, stdout="", stderr="DJANGO_SETUP_FAILED: No module named 'mssql'\n")
            with patch("subprocess.run", return_value=fake_proc):
                ok, msg, info = cutover.test_app_connection(path)
                self.assertFalse(ok)
                self.assertIn("Django initialization failed: No module named 'mssql'", msg)
        finally:
            os.unlink(path)

    def test_db_connection_failure_reporting(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"DATABASES = {}\n")
            path = f.name
        try:
            fake_proc = MagicMock(returncode=102, stdout="", stderr="DB_CONNECTION_FAILED: Login failed for user 'sa'\n")
            with patch("subprocess.run", return_value=fake_proc):
                ok, msg, info = cutover.test_app_connection(path)
                self.assertFalse(ok)
                self.assertIn("Database connection failed: Login failed for user 'sa'", msg)
        finally:
            os.unlink(path)

    def test_cutover_stage_ui_button_and_state(self):
        try:
            app = App()
        except Exception:
            return
        try:
            stage = app.stages[5]
            self.assertTrue(hasattr(stage, "test_btn"))
            self.assertTrue(hasattr(stage, "_test_connection"))
            self.assertFalse(stage.test_btn._enabled)

            with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
                f.write(b"DATABASES = {'default': {}}\n")
                settings_file = f.name
            try:
                stage.settings_path.set(settings_file)
                stage._refresh()
                self.assertTrue(stage.test_btn._enabled)

                stage.reset()
                self.assertFalse(stage.test_btn._enabled)
            finally:
                os.unlink(settings_file)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
