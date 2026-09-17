"""UI workflow regressions; requires a display, uses no live databases."""
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from pgbridge import app as A, engine as E, cutover


class UIQA(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches=[patch.object(A.App,'_load_profile'),patch.object(A.App,'_announce_checkpoint'),
                      patch.object(A,'Options',side_effect=lambda:E.Options(output_dir=self.tmp.name)),
                      patch.object(A.messagebox,'showwarning'),patch.object(A.messagebox,'showerror'),
                      patch.object(A.messagebox,'showinfo')]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        try:self.app=A.App()
        except tk.TclError as exc:self.skipTest(str(exc))
        self.addCleanup(self.app.destroy)
        self.app.withdraw()
        self.connect=self.app.stages[0]

    def prepared(self):
        self.connect._commit()
        self.app.tables=[E.Table('t',source_rows=3,target_exists=True,
                         columns=[E.Column('id','integer',False,True)])]
        self.app.pg.dbname='s';self.app.ms.database='t'
        self.app.loaded_pair=('s','t')

    def test_switch_activity_starts_fresh_migration(self):
        self.app.set_activity('migrate')
        self.prepared()
        self.app.pg.dbname = self.app.ms.database = 'shop_app'
        conns = self.app.conns
        source_settings = vars(self.app.pg).copy()
        target_settings = vars(self.app.ms).copy()
        for index in (1, 4):
            self.app.stages[index].picker.sync()
        self.app.stages[5]._show_database('shop_app')
        self.app.transfer_summary = {'rows': 3}
        self.app.verify_report = {'passed': True}
        previous_job = self.app.job
        previous_job.status = 'passed'

        self.app.change_activity()
        self.app.set_activity('migrate')

        self.assertIs(self.app.conns, conns)
        source_settings['dbname'] = ''
        target_settings['database'] = ''
        self.assertEqual(vars(self.app.pg), source_settings)
        self.assertEqual(vars(self.app.ms), target_settings)
        for index in (1, 4):
            picker = self.app.stages[index].picker
            self.assertEqual(picker.src.get(), '')
            self.assertEqual(picker.tgt.get(), '')
        self.assertEqual(self.app.stages[5].database.get(), '')
        self.assertEqual(self.app.tables, [])
        self.assertIsNone(self.app.loaded_pair)
        self.assertIsNone(self.app.verify_report)
        self.assertEqual(self.app.transfer_summary, {})
        self.assertEqual(previous_job.source['database'], 'shop_app')
        self.assertEqual(previous_job.target['database'], 'shop_app')
        self.assertEqual(previous_job.transfer, {'rows': 3})
        self.assertEqual(previous_job.status, 'passed')

    def test_direct_activity_change_preserves_previous_job(self):
        self.app.set_activity('migrate')
        self.prepared()
        previous_job = self.app.job
        self.app.set_activity('verify')
        self.assertEqual(previous_job.source['database'], 's')
        self.assertEqual(previous_job.target['database'], 't')
        self.assertEqual(self.app.pg.dbname, '')
        self.assertEqual(self.app.ms.database, '')

    def test_stage_navigation_keeps_current_database(self):
        self.app.set_activity('migrate')
        self.prepared()
        self.app._goto(1)
        self.app._goto(0)
        self.app._goto(1)
        self.assertEqual(self.app.stages[1].picker.src.get(), 's')
        self.assertEqual(self.app.stages[1].picker.tgt.get(), 't')

    def workspace_editor(self):
        import json
        profile_path = Path(self.tmp.name) / 'profiles.json'
        profile_path.write_text(json.dumps({'profiles': {
            'Dev': {'pg': {'host': 'dev-pg', 'password': 'dev-secret'}},
            'UAT': {'pg': {'host': 'uat-pg', 'password': 'uat-secret'},
                    'ms': {'server': 'uat-sql', 'password': 'uat-sql-secret', 'trust_cert': True}}
        }}))
        profile_patch = patch.object(A, 'PROFILE_PATH', str(profile_path))
        profile_patch.start()
        self.addCleanup(profile_patch.stop)
        directory_patch = patch.object(A, 'PROFILE_DIR', self.tmp.name)
        directory_patch.start()
        self.addCleanup(directory_patch.stop)
        editor = self.app.test_connections_front()
        self.addCleanup(editor.master.destroy)
        return editor, profile_path

    def test_workspace_loads_selected_profile_before_testing(self):
        editor, _ = self.workspace_editor()
        original_host = self.app.pg.host
        editor.profile_var.set('UAT')
        editor._on_profile_selected()
        self.assertEqual(self.app.pg.host, original_host)
        self.assertEqual(editor.pg_fields['host'].get(), 'uat-pg')
        with patch.object(self.app, 'run_async') as run:
            editor._test()
        self.assertTrue(run.called)
        self.assertEqual(self.app.conns.pg.password, 'uat-secret')
        self.assertEqual(self.app.conns.ms.server, 'uat-sql')
        self.assertTrue(self.app.conns.ms.trust_cert)
        self.assertEqual(self.connect.pg_fields['host'].get(), 'uat-pg')

    def test_workspace_manual_entry_and_optional_password_save(self):
        import json
        editor, path = self.workspace_editor()
        editor._manual()
        self.assertEqual(editor.profile_var.get(), '')
        self.assertEqual(editor.pg_fields['password'].get(), '')
        self.assertEqual(editor.ms_fields['password'].get(), '')
        editor.pg_fields['host'].set('manual-pg')
        editor.pg_fields['password'].set('private')
        editor._save_named('Manual')
        saved = json.loads(path.read_text())['profiles']
        self.assertEqual(saved['Manual']['pg']['host'], 'manual-pg')
        self.assertEqual(saved['Manual']['pg']['password'], '')
        self.assertIn('UAT', saved)
        editor.remember.set(True)
        editor._save_named('Manual with password')
        saved = json.loads(path.read_text())['profiles']
        self.assertEqual(saved['Manual with password']['pg']['password'], 'private')

    def test_workspace_test_is_blocked_during_operation(self):
        with patch.object(self.app, 'operation_active', return_value=True):
            self.assertIsNone(self.app.test_connections_front())

    def test_workspace_results_stay_in_editor_and_test_both_servers(self):
        editor, _ = self.workspace_editor()
        with patch.object(self.app, 'run_async') as run:
            editor._test()
        self.assertTrue(editor._testing)
        self.assertFalse(editor.test_btn._enabled)
        editor._close()
        self.assertTrue(editor.winfo_exists())
        conns = self.app.conns
        with patch.object(conns, 'probe_source', side_effect=RuntimeError('Source unavailable')), \
             patch.object(conns, 'probe_target', return_value={'version': 'SQL Server QA', 'databases': ['uat']}) as target:
            outcomes = run.call_args.args[0]()
        target.assert_called_once()
        run.call_args.kwargs['on_done'](outcomes)
        self.assertFalse(editor._testing)
        self.assertTrue(editor.test_btn._enabled)
        self.assertIn('Failed', editor.results['PostgreSQL'].cget('text'))
        self.assertIn('Connected', editor.results['SQL Server'].cget('text'))
        A.messagebox.showinfo.assert_not_called()
        A.messagebox.showerror.assert_not_called()

    def test_workspace_success_renders_inline(self):
        editor, _ = self.workspace_editor()
        with patch.object(self.app, 'run_async') as run:
            editor._test()
        run.call_args.kwargs['on_done']({name: (True, {'version': 'QA server', 'databases': ['uat']})
                                       for name in editor.results})
        self.assertIn('authenticated successfully', editor.notes.cget('text'))
        self.assertTrue(self.connect.next_btn._enabled)
        A.messagebox.showinfo.assert_not_called()

    def test_empty_preflight_findings_allow_continue(self):
        self.prepared()
        stage=self.app.stages[2]
        stage._loaded([])
        self.assertTrue(stage.next_btn._enabled)
        stage.reset()
        stage._recheck()
        self.assertFalse(stage.next_btn._enabled)

    def test_password_whitespace_and_blank_profile(self):
        self.connect.pg_fields['password'].set(' padded ')
        self.connect.ms_fields['password'].set(' padded ')
        self.assertTrue(self.connect._commit())
        self.assertEqual(self.app.pg.password,' padded ')
        self.assertEqual(self.app.ms.password,' padded ')
        self.connect.apply_profile({'pg':{'password':''},'ms':{'password':''}})
        self.connect._commit()
        self.assertEqual(self.app.pg.password,'')
        self.assertEqual(self.app.ms.password,'')

    def test_port_range_validation(self):
        for value in ('-1','0','65536','abc'):
            self.connect.pg_fields['port'].set(value)
            self.assertFalse(self.connect._commit())

    def test_server_change_invalidates_schema_and_verification(self):
        self.prepared()
        self.app.verify_report={'passed':True}
        self.connect.ms_fields['server'].set('other-server')
        self.connect._commit()
        self.assertEqual(self.app.tables,[])
        self.assertIsNone(self.app.verify_report)
        self.assertIsNone(self.app.loaded_pair)

    def test_invalid_batch_size_is_not_silently_replaced(self):
        stage=self.app.stages[3]
        for value in ('0','abc','1000001'):
            stage.chunk.set(value)
            with self.assertRaises(ValueError):stage._collect_options()

    def test_old_checkpoint_is_not_automatically_selected(self):
        self.prepared()
        import json
        (Path(self.tmp.name)/'checkpoint.json').write_text(json.dumps({'tables':{'t':{'status':'done'}}}))
        stage=self.app.stages[3]
        stage.on_show()
        self.assertFalse(stage.flags['resume'].get())
        self.assertIn('Resume is off',stage.resume_note.cget('text'))

    def test_changed_selection_requires_preflight(self):
        self.prepared()
        self.app._preflight_signature=self.app.plan_signature()
        self.app.tables[0].selected=False
        stage=self.app.stages[3]
        stage._start()
        self.assertIsNone(self.app.transport)
        self.assertEqual(A.messagebox.showwarning.call_args.args[0],'Preflight required')

    def test_zero_table_verification_cannot_enable_cutover(self):
        stage=self.app.stages[4]
        stage._loaded({'tables':[],'tables_checked':0,'passed':True,'rows_verified':0,
                       'untrusted_foreign_keys':[],'identity_problems':[]})
        self.assertFalse(self.app.verify_report['passed'])
        self.assertFalse(stage.next_btn._enabled)

    def test_dry_run_does_not_auto_verify(self):
        self.app.options.dry_run=True
        stage=self.app.stages[3]
        self.app.stages[4].run=Mock()
        stage._finished({'tables_total':1,'tables_ok':1,'rows':3,'elapsed':1,'cancelled':False,'dry_run':True})
        self.app.stages[4].run.assert_not_called()
        self.assertFalse(stage.next_btn._enabled)

    def test_failed_transfer_does_not_auto_verify(self):
        stage=self.app.stages[3]
        self.app.stages[4].run=Mock()
        stage._finished({'tables_total':1,'tables_ok':0,'rows':0,'elapsed':1,'cancelled':False,'error':'checkpoint mismatch'})
        self.app.stages[4].run.assert_not_called()
        self.assertFalse(stage.next_btn._enabled)

    def test_active_operation_prevents_connection_changes(self):
        self.app._busy=True
        self.connect.ms_fields['server'].set('another')
        self.assertFalse(self.connect._commit())
        self.assertNotEqual(self.app.ms.server,'another')
        self.app._busy=False

    def test_transfer_created_table_metadata_returns_to_ui(self):
        self.prepared()
        self.app.tables[0].target_exists=False
        observed=E.Table('t',target_exists=True,target_has_identity=True)
        self.app.transport=Mock(tables=[observed],results={'t':{'status':'done','rows':3}})
        self.app.stages[4].run=Mock()
        self.app.stages[3]._finished({'tables_total':1,'tables_ok':1,'rows':3,'elapsed':1,'cancelled':False})
        self.assertTrue(self.app.tables[0].target_exists)
        self.assertTrue(self.app.tables[0].target_has_identity)
        self.assertEqual(self.app.tables[0].target_rows,3)

    def test_database_dropdown_search_is_case_insensitive_and_selects(self):
        picker=self.app.stages[1].picker
        picker.src.set_values(['sales_portal','AUDIT_PROD','audit_archive'])
        widget=picker.src.widget
        widget._open_search()
        widget._query.set('AuDiT')
        self.assertEqual(widget._matches.get(0,'end'),('AUDIT_PROD','audit_archive'))
        self.assertEqual(picker.src.get(),'')
        widget._choose_search()
        self.assertEqual(picker.src.get(),'AUDIT_PROD')
        self.assertEqual(self.app.pg.dbname,'AUDIT_PROD')
        self.assertEqual(len(widget.cget('values')),3)

    def test_cutover_search_does_not_change_database_and_handles_no_matches(self):
        stage=self.app.stages[5]
        stage.database.set_values(['alpha','beta'])
        stage.database.set('alpha')
        widget=stage.database.widget
        widget._open_search()
        widget._query.set('not-present')
        self.assertEqual(widget._matches.size(),0)
        widget._choose_search()
        self.assertEqual(self.app.ms.database,'alpha')
        widget._query.set('')
        self.assertEqual(widget._matches.size(),2)
        widget._close_search()
        self.assertEqual(stage.database.get(),'alpha')

    def test_database_sync_preserves_loaded_names_and_manual_target(self):
        picker=self.app.stages[4].picker
        picker.tgt.set_values(['existing_one','existing_two'])
        self.app.ms.database='new_target'
        picker.sync()
        self.assertEqual(picker.tgt.widget.cget('values'),('existing_one','existing_two','new_target'))
        self.assertEqual(picker.tgt.get(),'new_target')
        self.assertEqual(str(picker.tgt.widget.cget('state')),'normal')

    def test_all_database_pickers_use_searchable_dropdowns(self):
        from pgbridge.widgets import SearchableCombobox
        for stage in (self.app.stages[1],self.app.stages[4]):
            self.assertIsInstance(stage.picker.src.widget,SearchableCombobox)
            self.assertIsInstance(stage.picker.tgt.widget,SearchableCombobox)
        self.assertIsInstance(self.app.stages[5].database.widget,SearchableCombobox)

    def test_cutover_input_and_button_widths_align(self):
        stage=self.app.stages[5]
        self.app.deiconify()
        self.app.set_activity('cutover')
        self.app._goto(5)
        for size in ('1280x840','1120x720'):
            self.app.geometry(size)
            self.app.update()
            widths=[entry.winfo_width() for entry in stage.file_entries]
            widths.append(stage.where.widget.winfo_width())
            self.assertLessEqual(max(widths)-min(widths),2,widths)
            self.assertEqual(len({button.winfo_width() for button in stage.file_buttons}),1)
            self.assertTrue(all(button.variant=='ghost' for button in stage.file_buttons))

    def test_size_summary_is_invalidated_by_plan_changes(self):
        self.prepared()
        stage=self.app.stages[2]
        stage._show_size({'estimated_bytes':1500000,'database_bytes':9000000,'rows':3,'tables':1,'errors':[]},self.app.plan_signature())
        self.assertIn('1.50 MB',stage.size_label.cget('text'))
        self.app.tables[0].plan='schema'
        stage._recheck()
        self.assertIn('refresh size',stage.size_label.cget('text'))
        stage.reset()
        self.assertIsNone(stage.size_report)


if __name__=='__main__':unittest.main()
