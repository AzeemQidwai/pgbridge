"""Regression checks for connection escaping, recovery identity and handoffs."""
import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from pgbridge.engine import Connections, MsConfig, Options, PgConfig, Table, Transport
from pgbridge.cutover import patch_settings, update_env_file, validate_env_values
from pgbridge.handoff import FRAMEWORKS, render_handoff


class SafetyTests(unittest.TestCase):
    def test_odbc_values_are_braced_and_escaped(self):
        dsn = MsConfig(password='x};Encrypt=no;PWD={y', user='a;b').dsn()
        self.assertIn('PWD={x}};Encrypt=no;PWD={y}', dsn)
        self.assertIn('UID={a;b}', dsn)
        self.assertIn('TrustServerCertificate=no', dsn)

    def test_refuse_wrong_checkpoint_before_connecting(self):
        with tempfile.TemporaryDirectory() as folder:
            conns = Connections(PgConfig(dbname='app'), MsConfig(database='app'))
            options = Options(output_dir=folder, resume=True)
            first = Transport(conns, [Table('customer')], options)
            first.results = {'customer': {'status': 'done', 'rows': 10}}
            first.save_checkpoint()
            conns.pg.host = 'another-server'
            conns.target = Mock(side_effect=AssertionError('must not connect'))
            second = Transport(conns, [Table('customer')], options)
            second._run()
            conns.target.assert_not_called()
            events = list(second.events.queue)
            final = events[-1]
            self.assertEqual(final['kind'], 'finished')
            self.assertFalse(final['cancelled'])
            self.assertTrue(final['error'])
            self.assertEqual(final['tables_ok'], 0)
            self.assertEqual(first.load_checkpoint()['tables']['customer']['rows'], 10)

    def test_context_tracks_filters_and_contains_no_password(self):
        with tempfile.TemporaryDirectory() as folder:
            table = Table('customer')
            transfer = Transport(Connections(PgConfig(password='SECRET'), MsConfig(password='SECRET')),
                                 [table], Options(output_dir=folder))
            previous = transfer.resume_context
            table.row_filter = 'id > 5'
            self.assertEqual(previous, transfer.resume_context)  # active run is immutable
            changed = Transport(transfer.conns, [table], transfer.options)
            self.assertNotEqual(previous, changed.resume_context)
            self.assertNotIn('SECRET', str(transfer.resume_context))

    def test_handoffs_are_secret_free(self):
        ms = MsConfig(user='PRIVATE_USER', password='PRIVATE_SECRET', database='app')
        for framework in FRAMEWORKS:
            output = render_handoff(framework, ms)
            self.assertNotIn(ms.password, output)
            self.assertNotIn(ms.user, output)
            self.assertIn('application validation pending', output)
        ast.parse(FRAMEWORKS['SQLAlchemy / FastAPI / Flask'][2])
        with self.assertRaises(ValueError):
            render_handoff('unsupported', ms)

    def test_env_newline_injection_leaves_settings_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.py'
            original = "DATABASES = {'default': {}}\n"
            path.write_text(original)
            with self.assertRaises(ValueError):
                patch_settings(str(path), MsConfig(database='app', password='x\nEVIL=1'), True)
            self.assertEqual(path.read_text(), original)

    def test_env_preserves_other_keys_and_restricts_access(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('APP_MODE=production\nDB_NAME=old\n')
            update_env_file(str(path), {'DB_NAME': 'app', 'DB_PASSWORD': ' padded '})
            self.assertIn('APP_MODE=production', path.read_text())
            self.assertIn('DB_PASSWORD=" padded "', path.read_text())
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(folder).glob('.pgbridge-*')), [])

    def test_empty_database_cannot_be_patched(self):
        with self.assertRaises(ValueError):
            patch_settings('unused.py', MsConfig(database=''), False)


if __name__ == '__main__':
    unittest.main()
