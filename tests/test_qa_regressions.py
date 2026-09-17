"""QA regressions: execute real orchestration against deterministic driver doubles."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from pgbridge import engine as E


class EngineQA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.options = E.Options(output_dir=self.tmp.name, disable_constraints=False,
                                 reseed_identity=False, chunk_size=2)
        self.conns = E.Connections(E.PgConfig(dbname='src'), E.MsConfig(database='dst'))
        self.table = E.Table('items', source_rows=3, target_exists=True,
                            columns=[E.Column('id', 'integer', False, True),
                                     E.Column('label', 'text', True, False)])

    def transfer(self, tables=None):
        return E.Transport(self.conns, [self.table] if tables is None else tables, self.options)

    def drivers(self):
        source, target = MagicMock(), MagicMock()
        source.cursor.return_value.fetchmany.side_effect = [[(1,'one'),(2,'two')],[(3,'three')],[]]
        self.conns.source = Mock(return_value=source)
        self.conns.target = Mock(return_value=target)
        return source, target

    def final(self, transport):
        return [e for e in list(transport.events.queue) if e['kind'] == 'finished'][-1]

    def test_real_copy_path_clears_batches_commits_and_counts(self):
        source, target = self.drivers()
        transfer = self.transfer()
        transfer._run()
        self.assertEqual(transfer.results['items']['rows'], 3)
        self.assertEqual(self.final(transfer)['tables_ok'], 1)
        self.assertEqual(target.cursor.return_value.executemany.call_count, 2)
        self.assertTrue(any('DELETE FROM' in str(call) for call in target.cursor.return_value.execute.call_args_list))
        source.close.assert_called_once()
        target.close.assert_called_once()
        self.assertEqual(transfer.load_checkpoint()['tables']['items']['rows'],3)

    def test_failed_later_batch_preserves_committed_row_count(self):
        source, target = self.drivers()
        target.cursor.return_value.executemany.side_effect = [None, RuntimeError('bad value')]
        transfer = self.transfer()
        transfer._run()
        self.assertEqual(transfer.results['items']['status'], 'failed')
        self.assertEqual(transfer.results['items']['rows'], 2)
        self.assertEqual(self.final(transfer)['tables_ok'], 0)

    def test_dry_run_never_writes_or_overwrites_checkpoint(self):
        source, target = self.drivers()
        original=self.transfer()
        original.results={'items':{'status':'running','rows':1}}
        original.save_checkpoint()
        before=Path(original.checkpoint_path).read_bytes()
        self.options.dry_run=True
        self.options.resume=True
        transfer=self.transfer()
        transfer._run()
        self.assertEqual(Path(original.checkpoint_path).read_bytes(),before)
        target.cursor.return_value.executemany.assert_not_called()
        target.cursor.return_value.execute.assert_not_called()
        self.assertTrue(self.final(transfer)['dry_run'])

    def test_empty_selection_is_failure_before_connections(self):
        self.drivers()
        transfer=self.transfer([])
        transfer._run()
        self.assertIn('No tables',self.final(transfer)['error'])
        self.conns.target.assert_not_called()

    def test_legacy_checkpoint_never_blocks_fresh_transfer(self):
        self.drivers()
        legacy=Path(self.tmp.name)/'checkpoint.json'
        legacy.write_text(json.dumps({'pair':['src','dst'],'tables':{'items':{'status':'done','rows':3}}}))
        transfer=self.transfer()
        transfer._run()
        self.assertEqual(transfer.results['items']['rows'],3)
        self.assertTrue(legacy.exists())
        self.assertNotEqual(str(legacy),transfer.checkpoint_path)

    def test_resume_missing_checkpoint_is_explicit_failure(self):
        self.drivers()
        self.options.resume=True
        transfer=self.transfer()
        transfer._run()
        self.assertTrue(self.final(transfer)['error'])
        self.assertFalse(self.final(transfer)['cancelled'])
        self.conns.target.assert_not_called()

    def test_resume_completed_preserves_results_without_copy(self):
        self.drivers()
        first=self.transfer()
        first.results={'items':{'status':'done','rows':3}}
        first.save_checkpoint()
        self.options.resume=True
        second=self.transfer()
        second._run()
        self.assertEqual(self.final(second)['rows'],3)
        self.assertEqual(self.final(second)['tables_ok'],1)
        self.conns.target.assert_not_called()

    def test_interrupted_resume_clears_even_when_clear_disabled(self):
        source,target=self.drivers()
        first=self.transfer()
        first.results={'items':{'status':'running','rows':2}}
        first.save_checkpoint()
        self.options.resume=True
        self.options.clear_target=False
        second=self.transfer()
        second._run()
        self.assertIn('items',second.force_clear)
        self.assertTrue(any('DELETE FROM' in str(call) for call in target.cursor.return_value.execute.call_args_list))

    def test_checkpoint_paths_are_isolated_by_database_and_server(self):
        first=self.transfer().checkpoint_path
        self.conns.ms.server='other'
        self.assertNotEqual(first,self.transfer().checkpoint_path)
        self.conns.ms.server='localhost'
        self.conns.ms.database='other'
        self.assertNotEqual(first,self.transfer().checkpoint_path)

    def test_options_cannot_mutate_inflight(self):
        transfer=self.transfer()
        self.options.dry_run=True
        self.options.clear_target=False
        self.assertFalse(transfer.options.dry_run)
        self.assertTrue(transfer.options.clear_target)

    def test_invalid_numeric_options(self):
        for key,value in [('chunk_size',0),('workers',0),('workers',5),('max_retries',-1)]:
            with self.subTest(key=key,value=value):
                opts=E.Options(**{key:value})
                with self.assertRaises(ValueError):opts.validate()

    def test_constraint_failure_is_reported(self):
        conn=MagicMock()
        conn.cursor.return_value.execute.side_effect=RuntimeError('foreign key violation')
        with self.assertRaisesRegex(RuntimeError,'Constraint validation failed'):
            self.transfer()._set_constraints(conn,[self.table],True)

    def test_constraint_cleanup_error_prevents_success(self):
        self.options.disable_constraints=True
        self.drivers()
        transfer=self.transfer()
        transfer._set_constraints=Mock(side_effect=[None,RuntimeError('bad constraints')])
        transfer._run()
        self.assertIn('bad constraints',self.final(transfer)['error'])

    def test_reseed_runs_independently_of_disable_constraints(self):
        self.options.reseed_identity=True
        self.drivers()
        transfer=self.transfer()
        transfer._reseed=Mock()
        transfer._run()
        transfer._reseed.assert_called_once()

    def test_quarantine_means_partial_not_done(self):
        self.options.on_row_error='quarantine'
        source,target=self.drivers()
        target.cursor.return_value.executemany.side_effect=RuntimeError('invalid data')
        cursor=target.cursor.return_value
        def execute(sql,*args):
            if sql.startswith('INSERT') and args[0][0]==2:raise RuntimeError('rejected')
        cursor.execute.side_effect=execute
        transfer=self.transfer()
        transfer._run()
        self.assertEqual(transfer.results['items']['status'],'partial')
        self.assertEqual(transfer.results['items']['rows_rejected'],1)
        self.assertEqual(self.final(transfer)['tables_ok'],0)

    def test_no_tables_verified_cannot_pass(self):
        self.drivers()
        verifier=E.Verifier(self.conns,[],self.options)
        verifier._untrusted_keys=Mock(return_value=[])
        verifier._identity_seeds=Mock(return_value=[])
        report=verifier.run(deep=False)
        self.assertFalse(report['passed'])
        self.assertTrue(report['incomplete'])

    def test_missing_target_is_failure_and_connections_close(self):
        source,target=self.drivers()
        self.table.target_exists=False
        verifier=E.Verifier(self.conns,[self.table],self.options)
        verifier._counts=Mock(side_effect=RuntimeError('Invalid object name'))
        verifier._untrusted_keys=Mock(return_value=[])
        verifier._identity_seeds=Mock(return_value=[])
        report=verifier.run(deep=False)
        self.assertFalse(report['passed'])
        self.assertEqual(report['tables_failed'],1)
        source.close.assert_called_once()
        target.close.assert_called_once()

    def test_stale_target_metadata_does_not_skip_actual_count(self):
        self.drivers()
        self.table.target_exists=False
        verifier=E.Verifier(self.conns,[self.table],self.options)
        verifier._counts=Mock(return_value={'table':'items','status':'ok','source':3,'target':3})
        verifier._untrusted_keys=Mock(return_value=[])
        verifier._identity_seeds=Mock(return_value=[])
        self.assertTrue(verifier.run(deep=False)['passed'])
        verifier._counts.assert_called_once()

    def test_sample_matches_by_key_and_detects_case_changes(self):
        verifier=E.Verifier(self.conns,[self.table],self.options)
        with patch.object(E,'fetch',side_effect=[[(2,'CaseSensitive')],[(2,'casesensitive')]]) as fetch:
            report=verifier._sample(self.table,Mock(),Mock())
            self.assertEqual(report['status'],'fail')
            self.assertIn('WHERE [id] = ?',fetch.call_args.args[1])
            self.assertEqual(fetch.call_args.args[2],(2,))

    def test_unbounded_varchar_is_not_truncated_to_255(self):
        self.assertEqual(E.sql_type(E.Column('label','character varying',True,False)),'nvarchar(max)')

    def test_modern_identity_maps_to_sql_identity(self):
        col=E.Column('id','bigint',False,True,default='GENERATED IDENTITY')
        self.assertIn('IDENTITY(1,1)',E.generate_ddl('dbo',E.Table('t',columns=[col])))

    def test_batch_does_not_report_failed_transport_as_done(self):
        self.conns.ensure_target_database=Mock()
        self.options.mode='data'
        def run(transport):
            transport.emit('finished',rows=0,tables_ok=0,tables_total=1,cancelled=False,error='insert failed')
        with patch.object(E.Connections,'ensure_target_database'), \
             patch.object(E.Introspector,'discover',return_value=[self.table]), \
             patch.object(E.Preflight,'run',return_value=[]), patch.object(E.Transport,'_run',run):
            batch=E.BatchMigrator(self.conns,[('src','dst')],self.options)
            batch._run()
            self.assertEqual(batch.items[0].status,'failed')
            self.assertIn('insert failed',batch.items[0].error)

    def test_no_key_cannot_claim_full_sampling_pass(self):
        self.table.columns[0].is_pk=False
        report=E.Verifier(self.conns,[self.table],self.options)._sample(self.table,Mock(),Mock())
        self.assertEqual(report['status'],'fail')

    def test_timezone_profile_compares_utc(self):
        table=E.Table('t',columns=[E.Column('moment','timestamp with time zone',True,False)])
        specs=E.Verifier(self.conns,[table],self.options)._profile_specs(table)
        self.assertTrue(any("AT TIME ZONE 'UTC'" in src for _,src,_ in specs))

    def test_uuid_profile_normalizes_storage_format(self):
        table=E.Table('t',columns=[E.Column('id','uuid',False,True)])
        specs=E.Verifier(self.conns,[table],self.options)._profile_specs(table)
        self.assertTrue(any("REPLACE" in src and "REPLACE" in dst for _,src,dst in specs))

    def test_ambiguous_network_errors_are_not_retried(self):
        self.assertNotIn('08S01',E.RETRYABLE)
        self.assertNotIn('HYT00',E.RETRYABLE)

    def test_custom_env_is_explicit_in_generated_settings(self):
        from pgbridge.cutover import patch_settings
        settings=Path(self.tmp.name)/'settings.py'
        settings.write_text("DATABASES = {'default': {}}\n")
        env=str(Path(self.tmp.name)/'production.env')
        patch_settings(str(settings),E.MsConfig(database='app'),True,env_path=env)
        text=settings.read_text()
        self.assertIn(repr(env),text)
        self.assertIn('_pgbridge_config("DB_NAME")',text)
        import ast
        ast.parse(text)

    def test_probe_rejects_wrong_database_and_failed_exit(self):
        from pgbridge.cutover import test_app_connection
        settings=Path(self.tmp.name)/'settings.py'
        settings.write_text("DATABASES = {'default': {}}\n")
        for code,expected in ((0,'expected'),(1,'wrong')):
            proc=Mock(returncode=code,stdout='PGBRIDGE_OK|wrong|microsoft|SQL Server',stderr='')
            with patch('subprocess.run',return_value=proc):
                ok,_,_=test_app_connection(str(settings),expected_database=expected)
                self.assertFalse(ok)


if __name__=='__main__':unittest.main()
