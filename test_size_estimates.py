"""Size estimates must reflect transfer scope without pretending to be disk usage."""
import unittest
from unittest.mock import Mock, patch
from pgbridge.engine import (Column, Connections, PgConfig, MsConfig, Options, Table,
                             estimate_transfer_size, format_bytes)


class SizeTests(unittest.TestCase):
    def setUp(self):
        self.conns=Connections(PgConfig(schema='public'),MsConfig())
        self.source=Mock()
        self.conns.source=Mock(return_value=self.source)
        self.table=Table('items',source_rows=2000,columns=[Column('label','text',True,False),Column('blob','bytea',True,False)])

    def test_units(self):
        for value,expected in [(0,'0 B'),(999,'999 B'),(1000,'1.00 KB'),(1500000,'1.50 MB'),(2300000000,'2.30 GB'),(None,'Unavailable')]:
            self.assertEqual(format_bytes(value),expected)

    def test_sample_scales_rows_and_keeps_disk_size_separate(self):
        with patch('pgbridge.engine.fetch_one',side_effect=[(9000000,), (100,1000)]) as fetch:
            result=estimate_transfer_size(self.conns,[self.table],Options())
        self.assertEqual(result['estimated_bytes'],200000)
        self.assertEqual(result['database_bytes'],9000000)
        self.assertEqual(result['sampled_rows'],1000)
        sql=fetch.call_args.args[1]
        self.assertIn('"label"::text',sql)
        self.assertIn('OCTET_LENGTH("blob")',sql)
        self.assertEqual(fetch.call_args.args[2],(1000,))
        self.source.close.assert_called_once()

    def test_filtered_and_excluded_data(self):
        self.table.row_filter='"id" > 10'
        self.table.exclude_columns=['blob']
        with patch('pgbridge.engine.fetch_one',side_effect=[(9000,), (50,), (20,50)]) as fetch:
            result=estimate_transfer_size(self.conns,[self.table],Options())
        self.assertEqual(result['estimated_bytes'],1000)
        self.assertEqual(result['rows'],50)
        sql=fetch.call_args.args[1]
        self.assertIn('WHERE "id" > 10',sql)
        self.assertNotIn('"blob"',sql)

    def test_schema_only_and_skipped_tables_move_zero_data(self):
        for mode,plan,selected in [('schema','auto',True),('both','schema',True),('both','skip',True),('both','auto',False)]:
            self.table.plan,self.table.selected=plan,selected
            with patch('pgbridge.engine.fetch_one',return_value=(9000,)) as fetch:
                result=estimate_transfer_size(self.conns,[self.table],Options(mode=mode))
            self.assertEqual(result['estimated_bytes'],0)
            self.assertEqual(result['tables'],0)
            self.assertEqual(fetch.call_count,1)

    def test_failed_sample_does_not_show_partial_total(self):
        with patch('pgbridge.engine.fetch_one',side_effect=[(9000,),RuntimeError('query failed')]):
            result=estimate_transfer_size(self.conns,[self.table],Options())
        self.assertIsNone(result['estimated_bytes'])
        self.assertTrue(result['errors'])
        self.source.close.assert_called_once()

    def test_missing_disk_permission_does_not_hide_selected_size(self):
        with patch('pgbridge.engine.fetch_one',side_effect=[RuntimeError('permission'),(10,1000)]):
            result=estimate_transfer_size(self.conns,[self.table],Options())
        self.assertIsNone(result['database_bytes'])
        self.assertEqual(result['estimated_bytes'],20000)

    def test_empty_sample_with_stale_positive_count_is_unavailable(self):
        with patch('pgbridge.engine.fetch_one',side_effect=[(9000,), (0,0)]):
            result=estimate_transfer_size(self.conns,[self.table],Options())
        self.assertIsNone(result['estimated_bytes'])


if __name__=='__main__':unittest.main()
